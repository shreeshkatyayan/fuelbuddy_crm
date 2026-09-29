"""The Delivery Note billing re-check, writing only what changes (IDEV-3268).

Stock, ERPNext v15.96.0 delivery_note.py:772 ``update_billed_amount_based_on_so(so_detail)``: for ONE Sales
Order line it reads every submitted, non-return Delivery Note Item on the line in (posting_date,
posting_time, dn.name) order, spreads the amount invoiced directly against the Sales Order over them
FIFO, ``frappe.db.set_value``s billed_amt on EVERY row and returns EVERY parent. Both callers
(DeliveryNote.update_billing_status, SalesInvoice.update_billing_status_in_dn) then refresh every returned
Delivery Note (get_doc, update_billing_percentage, set_status, a Label comment on a status change). On a
30k-row line that holds the Sales Order line lock for minutes.

With ``billing_recheck_walk`` on and the guard holding, this replacement takes one path per call, each
counted (billing_recheck.observe):

- ``fast``: no row has si_detail, every stored billed_amt is exactly 0, nothing is invoiced against the
  Sales Order line (stock's own query) and no submitted Sales Invoice Item bills any row through
  dn_detail. Stock would rewrite 0 over every 0 and refresh DNs whose values cannot move: return [].
- ``fallback_si_detail``: a row has si_detail (delivered against an invoice). Stock runs.
- ``fallback_multi_item``: a Delivery Note has two rows on the line; stock's order between them is
  undefined, so its split cannot be reproduced. Stock runs.
- ``fifo``: stock's loop runs in memory on stock's own inputs (fifo.stock_values: the same queries,
  floats as frappe returns them, the same arithmetic in the same order), stock's write
  (frappe.db.set_value) runs for only the rows whose stored value it would change (fifo.needs_write),
  and only their parents are returned, so the callers run stock's refresh for exactly those DNs.

Returns: a return Delivery Note changes returned_qty on the rows it returns (update_prevdoc_status runs
before the walk), so the returned DN's per_billed can move while no billed_amt does. Stock refreshes it
only because it refreshes every sibling. wrap_update_billing_status refreshes it with stock code when
stock would have: it is a sibling on a line this walk handled with a reduced result (so only when the
return's items carry so_detail, as in stock), and it was not refreshed already.

Deliberate differences from stock, listed rather than hidden: Delivery Notes whose values do not change
are not touched (no modified bump, no set_status / on_change / notify_update), and per_billed / status
that were already wrong before the event are not repaired here; the repair job and the drift audit do
that (billing_recheck.api). Under a concurrent commit on the same line this reads the same snapshot stock
would; stock then rewrites every row from it, this writes only the rows whose value differs.
"""

import functools
import time
from types import SimpleNamespace

import frappe
from frappe.query_builder.functions import Sum
from frappe.utils import cint, flt

from fuelbuddy_crm.billing_recheck import config, fifo, guard, install, observe

DN = "Delivery Note"
DNI = "Delivery Note Item"
REDUCED = ("fast", "fifo")
_FRAMES = "billing_recheck_frames"  # frappe.local: one frame per running update_billing_status


def update_billed_amount_based_on_so(so_detail, update_modified=True):
	"""Drop-in for ERPNext's function of the same name (install() rebinds both module names to this).
	Returns the Delivery Notes whose billed_amt it changed; stock's full list on a stock path."""
	stock = install.stock_walk()
	if not config.walk_enabled():
		return _record(so_detail, stock(so_detail, update_modified), reduced=False)
	if mismatch := guard.mismatches():
		observe.count("guard_disabled", so_detail, mismatch=",".join(mismatch))
		return _record(so_detail, stock(so_detail, update_modified), reduced=False)
	t0 = time.perf_counter()
	path, parents, info = _recheck(so_detail, update_modified, stock)
	observe.count(path, so_detail, ms=round((time.perf_counter() - t0) * 1000, 1), **info)
	return _record(so_detail, parents, reduced=path in REDUCED)


update_billed_amount_based_on_so._billing_recheck = True


def _recheck(so_detail, update_modified, stock):
	line = line_stats(so_detail)
	info = {"rows": line.rows}
	if line.si_detail:
		return "fallback_si_detail", stock(so_detail, update_modified), info
	billed_against_so = billed_against_so_of(so_detail)
	if not line.nonzero and not billed_against_so and not has_direct_billing(so_detail):
		return "fast", [], info
	if line.parents != line.rows:
		return "fallback_multi_item", stock(so_detail, update_modified), info
	changed = changes(so_detail, billed_against_so)
	for row, value in changed:
		frappe.db.set_value(DNI, row.name, "billed_amt", value, update_modified=update_modified)
	parents = sorted({row.parent for row, _value in changed})
	return "fifo", parents, {**info, "changed_rows": len(changed), "changed_dns": len(parents)}


# ---- stock's inputs ------------------------------------------------------------------------------------
def line_stats(so_detail):
	"""Counts over the rows stock walks: rows, distinct parents, rows with si_detail (Python-truthy, so
	a blank-looking value counts), rows whose stored billed_amt is not exactly 0."""
	rows, parents, si_detail, nonzero = frappe.db.sql(
		"""select count(*), count(distinct dni.parent),
			coalesce(sum(char_length(ifnull(dni.si_detail, '')) > 0), 0),
			coalesce(sum(dni.billed_amt is null or dni.billed_amt != 0), 0)
		from `tabDelivery Note Item` dni, `tabDelivery Note` dn
		where dn.name = dni.parent and dni.so_detail = %s and dn.docstatus = 1 and dn.is_return = 0""",
		so_detail,
	)[0]
	return SimpleNamespace(
		rows=cint(rows), parents=cint(parents), si_detail=cint(si_detail), nonzero=cint(nonzero)
	)


def billed_against_so_of(so_detail):
	"""Stock's first query, verbatim, read the way stock reads it."""
	si = frappe.qb.DocType("Sales Invoice").as_("si")
	si_item = frappe.qb.DocType("Sales Invoice Item").as_("si_item")
	sum_amount = Sum(si_item.amount).as_("amount")

	billed_against_so = (
		frappe.qb.from_(si_item)
		.join(si)
		.on(si.name == si_item.parent)
		.select(sum_amount)
		.where(
			(si_item.so_detail == so_detail)
			& ((si_item.dn_detail.isnull()) | (si_item.dn_detail == ""))
			& (si_item.docstatus == 1)
			& (si.update_stock == 0)
		)
		.run()
	)
	return (billed_against_so and billed_against_so[0][0]) or 0


def has_direct_billing(so_detail):
	"""Any submitted Sales Invoice Item billing a row of the line through dn_detail (any row, stricter
	than stock needs: a sum of 0 would also do)."""
	return bool(
		frappe.db.sql(
			"""select count(*) from `tabSales Invoice Item` sii
			where sii.docstatus = 1 and sii.dn_detail in (
				select dni.name from `tabDelivery Note Item` dni where dni.so_detail = %s)""",
			so_detail,
		)[0][0]
	)


def stock_rows(so_detail):
	"""Stock's dn_details query (same tables, filters and order) plus the stored billed_amt."""
	dn = frappe.qb.DocType("Delivery Note").as_("dn")
	dn_item = frappe.qb.DocType("Delivery Note Item").as_("dn_item")
	return (
		frappe.qb.from_(dn)
		.from_(dn_item)
		.select(dn_item.name, dn_item.amount, dn_item.si_detail, dn_item.parent, dn_item.billed_amt)
		.where(
			(dn.name == dn_item.parent)
			& (dn_item.so_detail == so_detail)
			& (dn.docstatus == 1)
			& (dn.is_return == 0)
		)
		.orderby(dn.posting_date, dn.posting_time, dn.name)
		.run(as_dict=True)
	)


def direct_sums(so_detail):
	"""{row name: sum(amount) of its submitted Sales Invoice Items}: stock runs ``select sum(amount) from
	tabSales Invoice Item where dn_detail=%s and docstatus=1`` per row; one join over the same rows, keyed
	by the Delivery Note Item's own name and matched with the same collation, gives the same DECIMAL sums."""
	return dict(
		frappe.db.sql(
			"""select dni.name, sum(sii.amount)
			from `tabDelivery Note Item` dni
			join `tabSales Invoice Item` sii on sii.dn_detail = dni.name and sii.docstatus = 1
			where dni.so_detail = %s
			group by dni.name""",
			so_detail,
		)
	)


def changes(so_detail, billed_against_so=None):
	"""[(row, value)] for the rows whose stored billed_amt stock's walk would change, with the value."""
	if billed_against_so is None:
		billed_against_so = billed_against_so_of(so_detail)
	rows = stock_rows(so_detail)
	values = fifo.stock_values(billed_against_so, rows, direct_sums(so_detail), flt)
	return fifo.changed_rows(rows, values)


# ---- returns --------------------------------------------------------------------------------------------
def _record(so_detail, parents, reduced):
	"""Note, for the update_billing_status running now (if any), what this walk returned."""
	frames = getattr(frappe.local, _FRAMES, None)
	if frames:
		frames[-1]["returned"].update(parents)
		if reduced:
			frames[-1]["reduced"].add(so_detail)
	return parents


def wrap_update_billing_status(original):
	@functools.wraps(original)
	def update_billing_status(self, update_modified=True):
		frames = getattr(frappe.local, _FRAMES, None)
		if frames is None:
			frames = []
			setattr(frappe.local, _FRAMES, frames)
		frame = {"returned": set(), "reduced": set()}
		frames.append(frame)
		try:
			out = original(self, update_modified)
		finally:
			frames.pop()
		if frame["reduced"] and self.get("is_return"):
			refresh_returned(self, frame, update_modified)
		return out

	update_billing_status._billing_recheck = True
	return update_billing_status


def refresh_returned(doc, frame, update_modified=True):
	"""Stock's refresh (update_billing_percentage) for the DNs this return returns that stock's full
	sibling list would have covered and the reduced one did not."""
	dn_details = tuple(d.dn_detail for d in doc.get("items") if d.get("dn_detail"))
	returned = {doc.get("return_against")}
	if dn_details:
		returned.update(
			frappe.db.sql_list(
				"select distinct parent from `tabDelivery Note Item` where name in %(names)s",
				{"names": dn_details},
			)
		)
	returned = {n for n in returned if n} - frame["returned"] - {doc.name}
	if not returned:
		return []
	siblings = frappe.db.sql_list(
		"""select distinct dni.parent from `tabDelivery Note Item` dni, `tabDelivery Note` dn
		where dn.name = dni.parent and dni.so_detail in %(lines)s and dn.docstatus = 1 and dn.is_return = 0
			and dni.parent in %(names)s""",
		{"lines": tuple(sorted(frame["reduced"])), "names": tuple(sorted(returned))},
	)
	todo = sorted(set(siblings) - frame["returned"] - {doc.name})
	for name in todo:
		frappe.get_doc(DN, name).update_billing_percentage(update_modified=update_modified)
	if todo:
		observe.log(f"return_refresh {doc.name}: {','.join(todo)}")
	return todo
