# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Quantity correction (IDEV-3266): change a Delivery Note's quantity in ONE ERP transaction.

Called by erp-functions (Hasura actions ``amendDeliveryNote`` / ``planDeliveryNoteAmendment``)
for the QuantityCorrectionWorkflow. Codes pass through erp-functions untouched, so they are
fixed strings, never parsed from messages.

``get_amendment_plan(invoiced_item_id, idempotency_key, target_qty)`` — read-only. The amend
log for this episode, the live Delivery Note and whether it is already at target, and the ERP
feasibility checks: ``SO_CLOSED``, ``PERIOD_CLOSED``, ``INVOICED``, ``DUPLICATE_LIVE_DN``.

``amend_delivery_note(delivery_note, target_qty, idempotency_key, not_after)``:

    Draft      qty updated in place; deleted when the target is 0
    Submitted  cancel, insert the amendment, submit — one transaction; cancel only at 0

``target_qty`` is litres. Each line's qty is written in that line's UOM (litres / conversion
factor, some lines are Imperial Gallons). A reduction trims from the last line backwards and
drops emptied lines; an increase grows the last line within its Sales Order line's headroom and
spills onto the next Sales Order (fuelbuddy_crm.so_allocator, ported from erp-functions),
otherwise ``SO_HEADROOM``. The amendment keeps the original's posting date and time
(``set_posting_time`` on), line rates, taxes and price list; ``amended_from`` is the original
and ``custom_version`` is parent + 1 via dn_versioning.set_amended_version.

Idempotency: the episode key goes on the resulting live Delivery Note
(``custom_qc_idempotency_key``, unique, no_copy) and, in every branch, into a ``DN Amend Log``
row written in the same transaction. The method locks the input Delivery Note, then reads the
log with a locking read; a retry, or the plan, returns the logged result instead of
``NOT_LIVE_DN``.

Cut-off: ``not_after`` is the ticket's ``apply_cutoff_at``. The database clock is compared with
it inside the transaction, again just before the commit, so a slow or retried amend cannot land
after it (``DEADLINE``).

    NOT_LIVE_DN, SO_CLOSED, INVOICED, PERIOD_CLOSED, DEADLINE, SO_HEADROOM, ERP_VALIDATION  final
    LOCK_RETRY (timestamp mismatch, lock wait, deadlock)                                    retry

ERP wallet / credit limit are out of scope (IDEV-3266): not live in ERP. If either is switched
on, revisit — both run as validate hooks on every Delivery Note save and could refuse an amend.
"""

import math
import re
from datetime import timezone
from zoneinfo import ZoneInfo

import frappe
from dateutil import parser as date_parser
from erpnext.accounts.doctype.accounting_period.accounting_period import (
	ClosedAccountingPeriod,
	validate_accounting_period_on_doc_save,
)
from frappe import _
from frappe.utils import flt, get_system_timezone, strip_html

from fuelbuddy_crm import so_allocator
from fuelbuddy_crm.dn_invoice_link import LINK_FIELD, QTY_FIELD, covering_invoices, window_invoices
from fuelbuddy_crm.dn_validation import so_headroom_shortfalls
from fuelbuddy_crm.dn_versioning import QC_IDEMPOTENCY_KEY_FIELD as KEY_FIELD

LOG_DOCTYPE = "DN Amend Log"

AMENDED = "AMENDED"
CANCELLED = "CANCELLED"
DRAFT_UPDATED = "DRAFT_UPDATED"
DRAFT_DELETED = "DRAFT_DELETED"

RETRYABLE = frozenset({"LOCK_RETRY"})
CLOSED_SO_STATUSES = ("Closed", "On Hold")

# A Delivery Note within this many litres of the target is "at target". Line qty is stored in
# the line's UOM, so an Imperial Gallon line cannot hit an arbitrary litre figure exactly.
LITRE_TOLERANCE = 0.01


class Refusal(Exception):
	"""A business refusal: returned to the caller as ``ok: false`` with its code."""

	def __init__(self, code, message):
		super().__init__(message)
		self.code = code
		self.message = message


# ---- amend ---------------------------------------------------------------------------------------
@frappe.whitelist(methods=["POST"])
def amend_delivery_note(delivery_note, target_qty, idempotency_key, not_after):
	"""Change one Delivery Note to ``target_qty`` litres. See the module docstring."""
	try:
		name = _required(delivery_note, "delivery_note")
		target = _parse_target_qty(target_qty)
		key = _required(idempotency_key, "idempotency_key")
		cutoff = _parse_not_after(not_after)
		if not frappe.has_permission("Delivery Note", "write"):
			raise Refusal("ERP_VALIDATION", _("Not permitted to amend Delivery Notes"))
	except Refusal as refusal:
		return _refused(refusal)

	# An episode ERP already applied answers from its log — after the cut-off too.
	logged = _read_log(key)
	if logged:
		return _logged(logged)
	if _db_now() >= cutoff:
		return _refused(_deadline(cutoff))

	try:
		# Start a fresh transaction, so the plain reads in _amend see what was committed before we
		# took the Delivery Note lock (say, by an amend of the same Delivery Note that held it),
		# not the snapshot the log read and permission check above opened.
		frappe.db.commit()
		result = _amend(name, target, key, cutoff)
		frappe.db.commit()
		return result
	except Exception as exc:
		frappe.db.rollback()
		if isinstance(exc, frappe.DuplicateEntryError | frappe.UniqueValidationError):
			# Only a committed log row for this episode makes a unique clash harmless: the
			# retry-safe answer is the logged result. Anything else is structural (another DN
			# already carries this key, a unique custom_invoiced_item_id, the amendment name) and
			# retrying cannot fix it.
			logged = _read_log(key)
			if logged:
				return _logged(logged)
		refusal = _as_refusal(exc)
		if refusal is None:
			raise  # infrastructure: surfaces as an HTTP error, which erp-functions retries
		return _refused(refusal)


def _amend(name, target, key, cutoff):
	# Lock the input DN first, then the log (a locking read also takes the gap lock when there
	# is no row yet), so two calls for one episode serialise and the second sees the first's log.
	frappe.db.sql("select name from `tabDelivery Note` where name = %s for update", name)
	logged = _read_log(key, for_update=True)
	if logged:
		return _logged(logged)

	state = frappe.db.get_value("Delivery Note", name, ["docstatus", "is_return"], as_dict=True)
	if not state or state.docstatus == 2 or state.is_return:
		raise Refusal("NOT_LIVE_DN", _("Delivery Note {0} is not a live delivery").format(name))
	_check_deadline(cutoff)

	doc = frappe.get_doc("Delivery Note", name)
	_check_sales_orders_open(doc)
	if doc.docstatus == 0:
		result = _amend_draft(doc, target, key)
	else:
		result = _amend_submitted(doc, target, key)

	frappe.get_doc(
		{
			"doctype": LOG_DOCTYPE,
			"idempotency_key": key,
			"delivery_note": name,
			"result": result["result"],
			"new_delivery_note": result["new_delivery_note"],
			"custom_version": result["custom_version"],
			"grand_total": result["grand_total"],
			"target_qty": target,
		}
	).insert(ignore_permissions=True)

	# Nothing commits after the cut-off, however long the amend itself took.
	_check_deadline(cutoff)
	return result


def _amend_draft(doc, target, key):
	_check_not_invoiced(doc)
	if _is_zero(target):
		frappe.delete_doc("Delivery Note", doc.name)
		return _ok(DRAFT_DELETED, _("Draft Delivery Note {0} deleted").format(doc.name))

	lines = _reshape(doc, target)
	by_row = {line["name"]: line for line in lines if line.get("name")}
	kept = []
	for row in doc.items:
		if row.name in by_row:
			row.qty = by_row[row.name]["qty"]
			kept.append(row)
	doc.set("items", kept)
	for line in lines:
		if not line.get("name"):
			doc.append("items", line)
	_renumber(doc)
	_refuse_on_headroom(doc)
	doc.set(KEY_FIELD, key)  # replaces a previous episode's key: the key marks the live DN
	doc.save()
	return _ok(
		DRAFT_UPDATED,
		_("Draft Delivery Note {0} updated to {1} L").format(doc.name, target),
		doc.name,
		_version(doc),
		doc.grand_total,
	)


def _amend_submitted(doc, target, key):
	lines = None if _is_zero(target) else _reshape(doc, target)
	original = {row.so_detail for row in doc.items if row.so_detail}
	added = {line["so_detail"] for line in lines or [] if line.get("so_detail")} - original
	_check_not_invoiced(doc, added)

	doc.cancel()
	if lines is None:
		return _ok(CANCELLED, _("Delivery Note {0} cancelled").format(doc.name))

	amendment = _build_amendment(doc, lines, key)
	# Checked on the reissue's FULL quantity, after the cancel freed the original's: other live
	# drafts may have taken the headroom since, so even a reduction can be refused.
	_refuse_on_headroom(amendment)
	amendment.insert()
	amendment.submit()
	return _ok(
		AMENDED,
		_("Delivery Note {0} amended to {1}").format(doc.name, amendment.name),
		amendment.name,
		_version(amendment),
		amendment.grand_total,
	)


def _build_amendment(original, lines, key):
	"""The amendment, the way ERPNext's own Amend builds it (a full copy, no_copy fields too,
	so SO links, rates, taxes, price list and posting date/time carry over), reshaped to
	``lines``."""
	amendment = frappe.copy_doc(original)
	amendment.amended_from = original.name
	amendment.set_posting_time = 1
	amendment.posting_date = original.posting_date
	amendment.posting_time = original.posting_time
	amendment.set(KEY_FIELD, key)
	amendment.flags.qc_idempotency_key = key  # drop_copied_idempotency_key keeps it
	amendment.set(LINK_FIELD, None)
	amendment.set(QTY_FIELD, 0)

	copies = {row.name: copy for row, copy in zip(original.items, amendment.items, strict=True)}
	amendment.set("items", [])
	for line in lines:
		if line.get("name"):
			row = copies[line["name"]]
			row.qty = line["qty"]
			amendment.append("items", row)
		else:
			amendment.append("items", line)
	_renumber(amendment)
	amendment.docstatus = 0
	for child in amendment.get_all_children():
		child.docstatus = 0
	return amendment


def _reshape(doc, target):
	"""The Delivery Note's lines at ``target`` litres (so_allocator), or a Refusal."""
	existing = [_line(row) for row in doc.items]
	current = sum(so_allocator.line_litres(line) for line in existing)
	candidates = _delta_candidates(doc) if target > current else []
	result = so_allocator.reconcile_delivery_note_lines(existing, target, candidates)
	if result.get("error"):
		raise Refusal("ERP_VALIDATION", _("Delivery Note {0}: {1}").format(doc.name, result["error"]))
	if result["leftover"] > 0:
		raise Refusal(
			"SO_HEADROOM",
			_("{0} L more than the Sales Orders valid on {1} have left for {2}").format(
				flt(result["leftover"], 3), doc.posting_date, doc.customer
			),
		)
	return result["lines"]


def _line(row):
	line = {
		key: row.get(key)
		for key in (
			"name",
			"item_code",
			"item_name",
			"qty",
			"uom",
			"conversion_factor",
			"rate",
			"against_sales_order",
			"so_detail",
			"warehouse",
			"cost_center",
		)
	}
	for optional in ("custom_customer_asset", "divisions"):
		if row.get(optional) is not None:
			line[optional] = row.get(optional)
	return line


def _delta_candidates(doc):
	"""Sales Orders that can absorb an increase, FIFO, starting at the last line's own order
	(as erp-functions updateDeliveryNote does). Closed / On Hold orders cannot take a delivery."""
	query = so_allocator.build_sales_order_query(doc.customer, doc.customer_address, doc.posting_date)
	orders = frappe.get_all(
		"Sales Order",
		filters=query["filters"],
		fields=[*query["fields"], "status"],
		order_by=query["order_by"],
		limit_page_length=query["limit"],
	)
	candidates = []
	for so in orders:
		if so.status in CLOSED_SO_STATUSES:
			continue
		items = frappe.get_all(
			"Sales Order Item",
			filters={"parent": so.name},
			fields=[
				"name",
				"item_code",
				"item_name",
				"uom",
				"conversion_factor",
				"rate",
				"price_list_rate",
				"discount_percentage",
				"discount_amount",
				"qty",
				"delivered_qty",
				"returned_qty",
				"custom_delivery_note_qty_in_draft",
			],
			order_by="idx asc",
			limit_page_length=1,
		)
		if items:
			candidates.append({"so": {"name": so.name}, "soItem": items[0]})
	last_so = doc.items[-1].against_sales_order if doc.items else None
	start = next((i for i, c in enumerate(candidates) if c["so"]["name"] == last_so), None)
	return candidates[start:] if start is not None else candidates


# ---- plan ----------------------------------------------------------------------------------------
@frappe.whitelist()
def get_amendment_plan(invoiced_item_id, idempotency_key, target_qty):
	"""Read-only: what amend_delivery_note would find for this invoiced item. See the module
	docstring."""
	try:
		iid = _required(invoiced_item_id, "invoiced_item_id")
		key = _required(idempotency_key, "idempotency_key")
		target = _parse_target_qty(target_qty)
		if not frappe.has_permission("Delivery Note", "read"):
			raise Refusal("ERP_VALIDATION", _("Not permitted to read Delivery Notes"))
	except Refusal as refusal:
		return _plan(refusal)

	logged = _read_log(key)
	amend_log = (
		{
			"result": logged.result,
			"new_delivery_note": logged.new_delivery_note or None,
			"custom_version": logged.custom_version if logged.new_delivery_note else None,
		}
		if logged
		else None
	)
	live = frappe.get_all(
		"Delivery Note",
		filters={"custom_invoiced_item_id": iid, "docstatus": ["<", 2], "is_return": 0},
		fields=["name"],
		order_by="creation asc",
		pluck="name",
	)
	if len(live) > 1 and not logged:
		return _plan(
			Refusal(
				"DUPLICATE_LIVE_DN",
				_("{0} live Delivery Notes for invoiced item {1}: {2}").format(
					len(live), iid, ", ".join(live)
				),
			)
		)
	if len(live) != 1:
		return _plan(amend_log=amend_log)

	doc = frappe.get_doc("Delivery Note", live[0])
	litres = sum(so_allocator.line_litres(row) for row in doc.items)
	facts = {
		"amend_log": amend_log,
		"live_delivery_note": doc.name,
		"live_dn_qty_litres": litres,
		"dn_at_target": abs(litres - target) <= LITRE_TOLERANCE,
		"docstatus": doc.docstatus,
	}
	if logged or facts["dn_at_target"]:
		return _plan(**facts)  # nothing for ERP to do, so nothing to check
	try:
		_check_sales_orders_open(doc)
		# Deleting a draft posts nothing, so ERPNext allows it in a closed period; every other
		# branch saves or cancels a Delivery Note on the original's posting date.
		if not (doc.docstatus == 0 and _is_zero(target)):
			_check_period_open(doc)
		_check_not_invoiced(doc)
	except Refusal as refusal:
		return _plan(refusal, **facts)
	return _plan(**facts)


def _plan(
	refusal=None,
	amend_log=None,
	live_delivery_note=None,
	live_dn_qty_litres=None,
	dn_at_target=False,
	docstatus=None,
):
	if refusal:
		frappe.clear_messages()
	return {
		"ok": refusal is None,
		"code": refusal.code if refusal else None,
		"message": refusal.message if refusal else None,
		"retryable": bool(refusal and refusal.code in RETRYABLE),
		"amend_log": amend_log,
		"live_delivery_note": live_delivery_note,
		"live_dn_qty_litres": live_dn_qty_litres,
		"dn_at_target": dn_at_target,
		"docstatus": docstatus,
	}


# ---- checks --------------------------------------------------------------------------------------
def _check_sales_orders_open(doc):
	orders = {row.against_sales_order for row in doc.items if row.against_sales_order}
	if not orders:
		return
	closed = frappe.get_all(
		"Sales Order",
		filters={"name": ["in", list(orders)], "status": ["in", CLOSED_SO_STATUSES]},
		fields=["name", "status"],
	)
	if closed:
		raise Refusal(
			"SO_CLOSED",
			", ".join(_("Sales Order {0} is {1}").format(so.name, so.status) for so in closed),
		)


def _check_period_open(doc):
	"""ERPNext's own closed-accounting-period test for saving this Delivery Note."""
	try:
		validate_accounting_period_on_doc_save(doc)
	except ClosedAccountingPeriod as exc:
		raise Refusal("PERIOD_CLOSED", _message(exc))


def _check_not_invoiced(doc, added_so_details=()):
	"""The INVOICED test is dn_invoice_link's own (covering_invoices), plus — for SO lines the
	reissue spills onto — any invoice whose window already covers the posting date there."""
	invoices = covering_invoices(doc)
	if added_so_details:
		invoices |= set(window_invoices(added_so_details, doc.posting_date))
	if invoices:
		raise Refusal(
			"INVOICED",
			_("Delivery Note {0} is covered by Sales Invoice {1}").format(
				doc.name, ", ".join(sorted(invoices))
			),
		)


def _refuse_on_headroom(doc):
	shortfalls = so_headroom_shortfalls(doc)
	if shortfalls:
		raise Refusal(
			"SO_HEADROOM",
			"; ".join(
				_("Sales Order {0} has {1} {2} left, the Delivery Note needs {3}").format(
					s.so_item.parent, flt(s.available, 3), s.so_item.uom, flt(s.increase, 3)
				)
				for s in shortfalls
			),
		)


def _check_deadline(cutoff):
	if _db_now() >= cutoff:
		raise _deadline(cutoff)


def _deadline(cutoff):
	return Refusal("DEADLINE", _("Apply cut-off {0} UTC has passed").format(cutoff))


def _db_now():
	"""The database clock (UTC), the one arbiter of the cut-off."""
	return frappe.db.sql("select utc_timestamp(6)")[0][0]


# ---- helpers -------------------------------------------------------------------------------------
def _read_log(key, for_update=False):
	rows = frappe.db.sql(
		f"""select result, new_delivery_note, custom_version, grand_total
		from `tab{LOG_DOCTYPE}` where idempotency_key = %s {"for update" if for_update else ""}""",
		key,
		as_dict=True,
	)
	return rows[0] if rows else None


def _logged(log):
	new_dn = log.new_delivery_note or None
	return _ok(
		log.result,
		_("Already applied by this episode"),
		new_dn,
		log.custom_version if new_dn else None,
		log.grand_total,
	)


def _ok(result, message, new_delivery_note=None, custom_version=None, grand_total=0.0):
	return {
		"ok": True,
		"code": None,
		"message": message,
		"retryable": False,
		"result": result,
		"new_delivery_note": new_delivery_note,
		"custom_version": custom_version,
		"grand_total": flt(grand_total),
	}


def _refused(refusal):
	frappe.clear_messages()
	return {
		"ok": False,
		"code": refusal.code,
		"message": refusal.message,
		"retryable": refusal.code in RETRYABLE,
		"result": None,
		"new_delivery_note": None,
		"custom_version": None,
		"grand_total": None,
	}


def _as_refusal(exc):
	"""Map an exception raised inside the amend to its code; None = not a business outcome."""
	if isinstance(exc, Refusal):
		return exc
	if isinstance(exc, ClosedAccountingPeriod):
		return Refusal("PERIOD_CLOSED", _message(exc))
	if isinstance(exc, frappe.TimestampMismatchError | frappe.QueryDeadlockError | frappe.QueryTimeoutError):
		return Refusal("LOCK_RETRY", _message(exc))
	if isinstance(exc, frappe.DuplicateEntryError | frappe.UniqueValidationError):
		# amend_delivery_note already answered a clash with this episode's own committed log.
		# The DN lock and the log gap lock serialise amends of one Delivery Note and one episode,
		# so what is left is a structural clash: not retryable.
		return Refusal("ERP_VALIDATION", _message(exc))
	if _is_lock_error(exc):
		return Refusal("LOCK_RETRY", _message(exc))
	if isinstance(exc, frappe.ValidationError | frappe.PermissionError):
		return Refusal("ERP_VALIDATION", _message(exc))
	return None


def _is_lock_error(exc):
	try:
		return bool(frappe.db.is_deadlocked(exc) or frappe.db.is_timedout(exc))
	except Exception:
		return False


def _message(exc):
	return strip_html(str(exc) or exc.__class__.__name__)[:500]


def _required(value, field):
	text = (str(value) if value is not None else "").strip()
	if not text:
		raise Refusal("ERP_VALIDATION", _("{0} is required").format(field))
	return text


def _parse_target_qty(value):
	try:
		target = float(value)
	except (TypeError, ValueError):
		raise Refusal("ERP_VALIDATION", _("target_qty must be a number of litres, got {0!r}").format(value))
	if math.isnan(target) or math.isinf(target) or target < 0:
		raise Refusal("ERP_VALIDATION", _("target_qty must be >= 0 litres, got {0}").format(value))
	return target


def _parse_not_after(value):
	"""ISO timestamp -> naive UTC. Without an offset it is read in the site's time zone."""
	text = _required(value, "not_after")
	try:
		moment = date_parser.isoparse(text)
	except (ValueError, OverflowError):
		raise Refusal("ERP_VALIDATION", _("not_after must be an ISO timestamp, got {0!r}").format(text))
	if moment.tzinfo is None:
		moment = moment.replace(tzinfo=ZoneInfo(get_system_timezone()))
	return moment.astimezone(timezone.utc).replace(tzinfo=None)


def _is_zero(litres):
	return litres < so_allocator.EPSILON


def _version(doc):
	"""custom_version as an int, parsed leniently like dn_versioning ("7abc" -> 7)."""
	match = re.match(r"\d+", str(doc.get("custom_version") or ""))
	return int(match.group()) if match else None


def _renumber(doc):
	for idx, row in enumerate(doc.items, start=1):
		row.idx = idx
