# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Invoices wait while a quantity correction is open on a delivery they would cover (IDEV-3266).

Owner decision (30 Sep): while a correction ticket is open on a delivery, the whole invoice for
that Sales Order waits. ERP cannot see the app's tickets, so the QuantityCorrectionWorkflow keeps
a ``QC Hold`` per invoiced item in ERP (fuelbuddy_crm.api.qc_hold writes them). This module reads
them.

``held_deliveries(so_lines, from_date, to_date)``
    The submitted, non-return Delivery Notes on those Sales Order lines posted inside the window
    (a blank bound is open) whose ``custom_invoiced_item_id`` has an Open hold that has not lapsed.
    The coverage rule is dn_invoice_link.window_invoices read from the invoice's side: exactly the
    Delivery Notes an invoice on those lines and window would make INVOICED for the
    quantity-correction amend.

``refuse_held_invoice``  Sales Invoice ``validate`` (a draft save, a submit)
    Refuses a live, non-return invoice that covers a held Delivery Note, with a table of the held
    Delivery Notes and their ticket episodes. Scheduler drafts are checked too; the job checks
    first (below), so this only catches a hold that landed in between.

``refuse_newly_held_after_submit``  Sales Invoice ``before_update_after_submit``
    The DN window can be edited after submit: refuses an edit that makes the invoice cover a held
    Delivery Note it did not cover before. Other edits of a submitted invoice pass.

``defer_sales_order``  fuelbuddy_crm.auto_invoicing
    The 12:00 job skips a Sales Order with a held Delivery Note in its window and leaves its
    "invoiced up to" date alone, so the next run takes the whole window again. One log line
    (``fuelbuddy_crm.qc_hold`` logger) per Sales Order per day.

Not covered: the check reads without a lock (the owner ruled out a Sales Order line lock), so an
invoice that read its deliveries just before a hold landed can still be saved. The amend's own
INVOICED check then refuses the correction loudly.
"""

from datetime import timezone
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import escape_html, get_datetime, get_link_to_form, get_system_timezone, nowdate

QC_HOLD_DOCTYPE = "QC Hold"
LOGGER = "fuelbuddy_crm.qc_hold"
_DEFERRED_KEY = "fuelbuddy_crm:qc_hold:deferred:{0}:{1}"
_DEFERRED_KEY_TTL = 2 * 24 * 3600


class InvoiceHeldError(frappe.ValidationError):
	"""An invoice would cover a Delivery Note whose quantity is being corrected. ``held``: the rows
	held_deliveries returned."""

	def __init__(self, *args, held=None):
		super().__init__(*args)
		self.held = held or []


# ---- the coverage read -----------------------------------------------------------------------------
def held_deliveries(so_lines, from_date=None, to_date=None):
	"""Held Delivery Notes an invoice on ``so_lines`` (Sales Order Item names) with this DN window
	would cover, oldest first: ``delivery_note, posting_date, invoiced_item_id, episode_key,
	opened_at, expires_at``. See the module docstring."""
	lines = sorted({line for line in so_lines or () if line})
	if not lines or not _any_hold_open():
		return []
	return frappe.db.sql(
		f"""select dn.name as delivery_note, dn.posting_date, h.invoiced_item_id, h.episode_key,
			h.opened_at, h.expires_at
		from `tab{QC_HOLD_DOCTYPE}` h
		join `tabDelivery Note` dn on dn.custom_invoiced_item_id = h.invoiced_item_id
		where h.status = 'Open' and h.expires_at > utc_timestamp()
			and dn.docstatus = 1 and dn.is_return = 0
			and (%(from_date)s is null or dn.posting_date >= %(from_date)s)
			and (%(to_date)s is null or dn.posting_date <= %(to_date)s)
			and exists (select 1 from `tabDelivery Note Item` dni
				where dni.parent = dn.name and dni.so_detail in %(lines)s)
		order by dn.posting_date, dn.name""",
		# A blank bound is open, as in window_invoices: '' must not reach the date comparison.
		{"lines": lines, "from_date": from_date or None, "to_date": to_date or None},
		as_dict=True,
	)


def _any_hold_open():
	"""The cheap common case: no hold anywhere, so no Delivery Note is read at all."""
	return bool(
		frappe.db.sql(
			f"""select 1 from `tab{QC_HOLD_DOCTYPE}`
			where status = 'Open' and expires_at > utc_timestamp() limit 1"""
		)
	)


def so_line_names(sales_order):
	"""Every line of a Sales Order: what its scheduler invoices can bill."""
	return frappe.get_all("Sales Order Item", filters={"parent": sales_order}, pluck="name")


# ---- Sales Invoice hooks ---------------------------------------------------------------------------
def refuse_held_invoice(doc, method=None):
	"""Sales Invoice ``validate``: see the module docstring."""
	if _not_billing(doc):
		return
	held = held_deliveries(_so_lines(doc), doc.get("custom_dn_from_date"), doc.get("custom_dn_to_date"))
	if held:
		_refuse(held)


def refuse_newly_held_after_submit(doc, method=None):
	"""Sales Invoice ``before_update_after_submit``: see the module docstring."""
	if _not_billing(doc):
		return
	held = held_deliveries(_so_lines(doc), doc.get("custom_dn_from_date"), doc.get("custom_dn_to_date"))
	before = doc.get_doc_before_save() if held else None
	if before:
		covered = {
			row.delivery_note
			for row in held_deliveries(
				_so_lines(before), before.get("custom_dn_from_date"), before.get("custom_dn_to_date")
			)
		}
		held = [row for row in held if row.delivery_note not in covered]
	if held:
		_refuse(held)


def _not_billing(doc):
	"""Invoices that make no Delivery Note INVOICED: credit notes and cancelled invoices
	(window_invoices counts only docstatus < 2, is_return = 0)."""
	return bool(
		frappe.flags.in_install or frappe.flags.in_migrate or doc.get("is_return") or doc.docstatus == 2
	)


def _so_lines(doc):
	return [row.get("so_detail") for row in doc.get("items") or [] if row.get("so_detail")]


def _refuse(held):
	frappe.throw(
		held_table(held),
		exc=InvoiceHeldError(held=held),
		title=_("Deliveries under quantity correction"),
	)


def held_table(held):
	"""The refusal: one row per held Delivery Note, with its ticket episode and when the hold
	lapses on its own (site time)."""
	zone = get_system_timezone()
	head = "".join(
		f"<th>{escape_html(label)}</th>"
		for label in (
			_("Delivery Note"),
			_("Posting Date"),
			_("Correction ticket episode"),
			_("Hold lapses at ({0})").format(zone),
		)
	)
	body = "".join(
		"<tr>"
		f"<td>{get_link_to_form('Delivery Note', row.delivery_note)}</td>"
		f"<td>{escape_html(str(row.posting_date))}</td>"
		f"<td>{escape_html(str(row.episode_key or ''))}</td>"
		f"<td>{escape_html(_site_time(row.expires_at, zone))}</td>"
		"</tr>"
		for row in held
	)
	intro = _(
		"This invoice would bill Delivery Notes whose quantity is being corrected. It can be saved "
		"once each correction has ended, or once its hold lapses."
	)
	return (
		f"<p>{escape_html(intro)}</p>"
		f'<table class="table table-bordered table-condensed"><thead><tr>{head}</tr></thead>'
		f"<tbody>{body}</tbody></table>"
	)


def _site_time(utc_value, zone):
	moment = get_datetime(utc_value)
	if not moment:
		return ""
	return moment.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M")


# ---- the 12:00 job -------------------------------------------------------------------------------
def defer_sales_order(sales_order, from_date=None, to_date=None, held=None):
	"""fuelbuddy_crm.auto_invoicing: this Sales Order waits. Logs one line per Sales Order per day;
	the caller leaves the "invoiced up to" date alone."""
	key = _DEFERRED_KEY.format(sales_order, nowdate())
	try:
		if frappe.cache.get_value(key):
			return
		frappe.cache.set_value(key, 1, expires_in_sec=_DEFERRED_KEY_TTL)
	except Exception:
		pass  # without the cache the line is logged once per run, and the job runs once a day
	deliveries = ", ".join(f"{row.delivery_note} (episode {row.episode_key})" for row in held or ()) or "-"
	frappe.logger(LOGGER, allow_site=True).info(
		f"auto-invoicing deferred Sales Order {sales_order}, window {from_date or '-'} to {to_date or '-'}: "
		f"Delivery Notes under quantity correction: {deliveries}. Invoiced-up-to date left unchanged."
	)
