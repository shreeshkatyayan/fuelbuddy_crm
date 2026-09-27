# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Stamp each Delivery Note with the Sales Invoice that billed it.

Fields: Delivery Note.custom_sales_invoice (Link) / custom_sales_invoice_qty (Float, litres),
created by patches.add_dn_sales_invoice_link. Hooks in hooks.py doc_events (Sales Invoice on_update /
on_update_after_submit / on_cancel / on_trash; Delivery Note on_update / on_cancel / on_trash).

Invoices here bill a Sales Order line for a DN date window (``custom_dn_from_date`` /
``custom_dn_to_date``), not individual DNs. On every Sales Invoice save this module rebuilds
the invoice's link: for each invoice row with a ``so_detail``, walk the DNs on that SO line
inside the window oldest-first and take from each until the row's stock qty is used up. Each
DN taken gets ``custom_sales_invoice`` = this invoice and ``custom_sales_invoice_qty`` = the
litres taken from it (the whole DN except, possibly, the last one).

A Delivery Note links to ONE invoice. A DN already stamped by another invoice is not a
candidate, so a DN whose litres straddle two invoices links to the first one with the qty it
took; the second invoice shows the remainder as a shortfall. Quantities are in the stock UOM
(litres) on both sides, so IG-priced SO lines work. Only SUBMITTED, non-return DNs take part:
that is the set fuelbuddy_crm.auto_invoicing sums the invoice quantities from. Split invoices
carry the DN's department / billing location on the invoice; those are matched when present.

Writes (IDEV-3268): each run computes the invoice's whole allocation from scratch, then writes
only the difference from what is stored (``_write_links``, the one write path): DNs it no longer
takes are cleared, new or changed takes are stamped, the rest are not touched. The end state is
the one clearing every DN the invoice holds and re-stamping every take gives, without locking and
rewriting the whole window on every invoice save and every DN event inside it.

Lock order (IDEV-3268): allocating and clearing first lock the invoice's Sales Order line rows
(``lock_so_lines``: SELECT ... FOR UPDATE, one statement, name order), then write DN rows. A DN
submit / cancel / delete holds its SO line before these hooks run (dn_validation.
sync_draft_reservation writes it in on_update / on_trash; ERPNext's update_prevdoc_status in
on_cancel), so invoice saves and DN events on one line queue behind each other in one order: SO
line, then DN rows. Reads stay plain (a locking read of DN rows would wait on a DN a concurrent
cancel holds, while that cancel waits for the SO line we hold). Under REPEATABLE READ those reads
can come from a snapshot taken before the lock was granted (the invoice's validate already read);
an allocation computed from such a snapshot is corrected by the invoice's next allocation, since
every run compares with what is stored. ERPNext's own billing walk reads the same way.
"""

import frappe
from frappe import _
from frappe.utils import flt

LINK_FIELD = "custom_sales_invoice"
QTY_FIELD = "custom_sales_invoice_qty"
EPS = 1e-6
# frappe reads the DECIMAL(21,9) qty column as a float. Below 2**23 that float, written back,
# stores the same decimal (lab, IDEV-3268: 0 of 254,816 values below it changed, 41,622 of 53,829
# above it did), so float equality means "writing would store what is there". At or above it the
# value is always written.
EXACT_ROUND_TRIP = 2**23


# ---- Sales Invoice hooks ---------------------------------------------------------------------
def allocate_sales_invoice(doc, method=None, exclude_dn=None):
	"""Bring this invoice's DN links to a fresh allocation (idempotent); writes only what changes."""
	if frappe.flags.in_install or frappe.flags.in_migrate:
		return
	lock_so_lines(doc)
	takes = {}  # dn name -> litres taken by this invoice; none for a cancelled invoice or a return
	if doc.docstatus != 2 and not doc.get("is_return"):
		for item in doc.items:
			if not item.so_detail:
				continue
			short = _allocate_row(doc, item, takes, exclude_dn)
			if short > EPS:
				frappe.msgprint(
					_(
						"Row {0}: {1} L of {2} L not covered by Delivery Notes on {3} between {4} and {5}"
					).format(
						item.idx,
						round(short, 3),
						round(_litres(item), 3),
						item.sales_order,
						doc.custom_dn_from_date or "-",
						doc.custom_dn_to_date or "-",
					),
					title=_("Delivery Note link"),
					indicator="orange",
				)
	_write_links(doc.name, takes)


def clear_sales_invoice(doc, method=None):
	lock_so_lines(doc)
	_write_links(doc.name, {})


def lock_so_lines(doc, method=None):
	"""Lock the Sales Order line rows this invoice bills: one statement, rows in name order, so
	two invoices never take the same lines in a different order.

	Also the first Sales Invoice after_insert hook: update_so_last_invoiced, next in that list,
	writes the Sales Order header, and a DN submit locks its SO line before the header
	(update_prevdoc_status), so an invoice insert takes the line before the header too.

	Not for a credit note: it takes no DNs, so it has no links to write, and stock ERPNext does
	not lock the SO line for one either; it should not queue behind the line's DN events."""
	if doc.get("is_return"):
		return
	names = sorted({item.so_detail for item in doc.items if item.get("so_detail")})
	if names:
		frappe.db.sql(
			"select name from `tabSales Order Item` where name in %(names)s order by name for update",
			{"names": tuple(names)},
		)


def _litres(row):
	return flt(row.stock_qty) or flt(row.qty) * flt(row.conversion_factor or 1)


def _candidates(doc, item, exclude_dn):
	"""Submitted non-return DNs on the SO line inside the window that are free or already ours,
	one row per DN with its litres on that SO line, oldest first."""
	conds = [
		"dni.so_detail = %(so_detail)s", "dn.docstatus = 1", "dn.is_return = 0",
		f"(ifnull(dn.`{LINK_FIELD}`, '') in ('', %(si)s))",
	]
	params = {"so_detail": item.so_detail, "si": doc.name}
	if doc.get("custom_dn_from_date"):
		conds.append("dn.posting_date >= %(from_date)s")
		params["from_date"] = doc.custom_dn_from_date
	if doc.get("custom_dn_to_date"):
		conds.append("dn.posting_date <= %(to_date)s")
		params["to_date"] = doc.custom_dn_to_date
	# Split invoices: auto_invoicing copies the group's department / billing location onto the SI.
	if doc.get("custom_department"):
		conds.append("dn.custom_department = %(department)s")
		params["department"] = doc.custom_department
	if doc.get("custom_location"):
		conds.append("dn.custom_billing_location = %(location)s")
		params["location"] = doc.custom_location
	if exclude_dn:
		conds.append("dn.name != %(exclude_dn)s")
		params["exclude_dn"] = exclude_dn
	return frappe.db.sql(
		f"""select dn.name as dn, sum(dni.stock_qty) as stock_qty
		from `tabDelivery Note Item` dni
		join `tabDelivery Note` dn on dn.name = dni.parent
		where {" and ".join(conds)}
		group by dn.name
		order by min(dn.posting_date), min(dn.posting_time), dn.name""",
		params,
		as_dict=True,
	)


def _allocate_row(doc, item, takes, exclude_dn):
	"""Record the DNs one invoice row takes; return the litres left uncovered."""
	left = _litres(item)
	for c in _candidates(doc, item, exclude_dn):
		if left <= EPS:
			break
		available = flt(c.stock_qty) - takes.get(c.dn, 0)  # a DN can serve two rows of the SAME invoice
		if available <= EPS:
			continue
		take = min(available, left)
		takes[c.dn] = takes.get(c.dn, 0) + take
		left -= take
	return max(left, 0)


def _write_links(si_name, takes):
	"""The one write path. ``takes`` is the invoice's whole allocation (dn -> litres); against
	what is stored, clear the DNs it no longer takes and stamp new or changed takes."""
	held = dict(
		frappe.db.sql(
			f"select name, `{QTY_FIELD}` from `tabDelivery Note` where `{LINK_FIELD}` = %s", si_name
		)
	)
	drop = [dn for dn in held if dn not in takes]
	if drop:
		_clear(None, dn_names=drop)
	_stamp(si_name, {dn: qty for dn, qty in takes.items() if dn not in held or not _same(held[dn], qty)})


def _same(stored, qty):
	return stored is not None and flt(stored) == flt(qty) and abs(flt(qty)) < EXACT_ROUND_TRIP


def _stamp(si_name, takes):
	if not takes:
		return
	# One UPDATE per 1000 DNs (a month of real invoices is ~100k DNs; per-row set_value ran at ~60/s).
	items = list(takes.items())
	for i in range(0, len(items), 1000):
		chunk = items[i : i + 1000]
		case = " ".join("when %s then %s" for _ in chunk)
		params = [si_name]
		for dn, qty in chunk:
			params += [dn, flt(qty)]
		params += [[dn for dn, _ in chunk]]
		frappe.db.sql(
			f"""update `tabDelivery Note` set `{LINK_FIELD}` = %s, `{QTY_FIELD}` = case name {case} end
			where name in %s""",
			params,
		)
	if len(takes) <= 500:
		for dn in takes:
			frappe.clear_document_cache("Delivery Note", dn)


def _clear(si_name, dn_names=None):
	if dn_names:
		names = list(dn_names)
	else:
		names = frappe.db.sql_list(f"select name from `tabDelivery Note` where `{LINK_FIELD}` = %s", si_name)
	if not names:
		return
	frappe.db.sql(
		f"""update `tabDelivery Note` set `{LINK_FIELD}` = NULL, `{QTY_FIELD}` = 0 where name in %(names)s""",
		{"names": names},
	)
	if len(names) <= 500:
		for dn in names:
			frappe.clear_document_cache("Delivery Note", dn)


# ---- Delivery Note hooks ---------------------------------------------------------------------
def on_delivery_note_update(doc, method=None):
	if doc.docstatus != 1:
		return  # drafts never take part
	before = doc.get_doc_before_save()
	if (
		before
		and before.docstatus == 1
		and str(before.posting_date) == str(doc.posting_date)
		and abs(sum(_litres(i) for i in before.items) - sum(_litres(i) for i in doc.items)) < EPS
	):
		return  # cosmetic save of a submitted DN: nothing a link depends on changed
	reallocate_for_delivery_note(doc)


def on_delivery_note_cancel(doc, method=None):
	reallocate_for_delivery_note(doc, exclude_self=True)


def on_delivery_note_trash(doc, method=None):
	reallocate_for_delivery_note(doc, exclude_self=True)


def reallocate_for_delivery_note(doc, exclude_self=False):
	"""Re-run the invoice that took this DN and every invoice whose window now covers it."""
	if frappe.flags.in_install or frappe.flags.in_migrate:
		return
	# The back-dated drain (fuelbuddy_dubai.api.dn_drain) submits tens of thousands of DNs with
	# ERPNext's own billing recompute switched off; stay off with it and let the post-drain step
	# call backfill() for the affected months.
	if getattr(frappe.flags, "fb_skip_billing_status", False):
		return
	so_details = [i.so_detail for i in doc.items if i.so_detail]
	if not so_details:
		return
	invoices = set()
	own = frappe.db.get_value("Delivery Note", doc.name, LINK_FIELD)
	if own:
		invoices.add(own)
	if not invoices and doc.docstatus != 1:
		return  # an unsubmitted DN no invoice ever took cannot affect any link
	if exclude_self:
		_clear(None, dn_names=[doc.name])
	invoices.update(
		frappe.db.sql_list(
			"""select distinct si.name from `tabSales Invoice` si
			join `tabSales Invoice Item` sii on sii.parent = si.name
			where sii.so_detail in %(so)s and si.docstatus < 2 and si.is_return = 0
			and (si.custom_dn_from_date is null or si.custom_dn_from_date <= %(d)s)
			and (si.custom_dn_to_date is null or si.custom_dn_to_date >= %(d)s)""",
			{"so": so_details, "d": doc.posting_date},
		)
	)
	if not invoices:
		return
	ordered = frappe.get_all(
		"Sales Invoice", filters={"name": ["in", list(invoices)]}, order_by="posting_date asc, creation asc", pluck="name"
	)
	for name in ordered:
		allocate_sales_invoice(frappe.get_doc("Sales Invoice", name), exclude_dn=doc.name if exclude_self else None)


# ---- one-off ---------------------------------------------------------------------------------
def backfill(from_date="2026-06-01"):
	"""bench --site <site> execute fuelbuddy_crm.dn_invoice_link.backfill --kwargs "{'from_date': '2026-06-01'}" """
	names = frappe.get_all(
		"Sales Invoice",
		filters={"docstatus": 1, "is_return": 0, "posting_date": [">=", from_date]},
		order_by="posting_date asc, creation asc",
		pluck="name",
	)
	frappe.flags.mute_messages = True
	for i, name in enumerate(names, 1):
		allocate_sales_invoice(frappe.get_doc("Sales Invoice", name))
		frappe.db.commit()
		if i % 100 == 0:
			print(f"{i}/{len(names)}")
	frappe.flags.mute_messages = False
	return len(names)


# ---- audit (read-only; fuelbuddy_crm.billing_repair.audit / nightly_drift_audit) --------------
OVER_LINK_TOLERANCE = 0.001  # litres; stored litres are DECIMAL(21,9), a few frontier rows round


def audit_links(sample=20):
	"""Read-only checks of what this module promises, each {"count", "sample"} (up to ``sample``
	names). Anything counted is drift an invoice re-run (or a missed DN event) left behind:

	- linked_not_submitted: a DN carries a link but is not a submitted non-return DN (a cancel
	  that skipped the link hooks, e.g. under the dubai fb_skip_billing_status flag);
	- linked_to_dead_invoice: the link names a missing, cancelled or return invoice;
	- over_linked: an invoice's DNs carry more litres than its rows with an SO line bill."""
	checks = {
		"linked_not_submitted": f"""select name from `tabDelivery Note`
			where ifnull(`{LINK_FIELD}`, '') != '' and (docstatus != 1 or is_return = 1) order by name""",
		"linked_to_dead_invoice": f"""select dn.name from `tabDelivery Note` dn
			left join `tabSales Invoice` si on si.name = dn.`{LINK_FIELD}`
			where ifnull(dn.`{LINK_FIELD}`, '') != ''
			and (si.name is null or si.docstatus = 2 or si.is_return = 1)
			order by dn.name""",
		"over_linked": f"""select l.si from (
				select `{LINK_FIELD}` as si, sum(`{QTY_FIELD}`) as linked from `tabDelivery Note`
				where ifnull(`{LINK_FIELD}`, '') != '' group by `{LINK_FIELD}`) l
			join (
				select parent as si, sum(case when ifnull(stock_qty, 0) != 0 then stock_qty
					else qty * (case when ifnull(conversion_factor, 0) != 0 then conversion_factor else 1 end)
					end) as billed
				from `tabSales Invoice Item` where ifnull(so_detail, '') != '' group by parent) b on b.si = l.si
			where l.linked > b.billed + {OVER_LINK_TOLERANCE} order by l.si""",
	}
	out = {}
	for key, query in checks.items():
		names = frappe.db.sql_list(query)
		out[key] = {"count": len(names), "sample": names[:sample]}
	return out
