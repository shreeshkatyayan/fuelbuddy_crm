"""What the repair job and the drift audit use (IDEV-3268).

The walk replacement writes only what changes, so it never repairs per_billed / status that were
already wrong; these functions find and repair such rows with stock's arithmetic and stock's code. None
of them commits: the caller decides the chunk size and commits between chunks.

- ``stock_would_change(so_detail)``: read-only; what stock's re-check of one Sales Order line would
  change right now (billed_amt rows), and which Delivery Notes on the line hold a per_billed / status
  different from what stock's refresh would store from their stored items.
- ``recompute_line(so_detail)``: writes the billed_amt stock's re-check would write (changed rows only;
  stock's own walk on a line it cannot reproduce) and returns the Delivery Notes it changed.
- ``header_drift(names)``: read-only; per_billed / status drift of the given Delivery Notes.
- ``refresh_dns(names)``: stock's refresh (update_billing_percentage) of the given Delivery Notes.
- ``line_delivery_notes(so_detail)``: the submitted, non-return Delivery Notes on a line (stock's
  siblings).

Order for a repair: recompute_line, then header_drift over the line's DNs (billed_amt first, since
per_billed is computed from it), then refresh_dns in chunks. The read-only functions raise GuardError
when the running ERPNext / frappe code is not the pinned code (their copies of stock's arithmetic would
not be proven); recompute_line and refresh_dns then fall back to stock code.
"""

import frappe
from frappe.utils import flt

from fuelbuddy_crm.billing_recheck import guard, install, walk
from fuelbuddy_crm.billing_recheck import stock_refresh as sr

DN = "Delivery Note"


class GuardError(Exception):
	pass


def _require_guard():
	if mismatch := guard.mismatches():
		raise GuardError(", ".join(mismatch))


def _stock_walk():
	stock = install.stock_walk()
	if stock is None:  # never installed in this process: the module still holds ERPNext's own
		dn_mod, _si, _sc, _su = guard.modules()
		stock = dn_mod.update_billed_amount_based_on_so
	return stock


def line_delivery_notes(so_detail):
	return frappe.db.sql_list(
		"""select distinct dni.parent from `tabDelivery Note Item` dni, `tabDelivery Note` dn
		where dn.name = dni.parent and dni.so_detail = %s and dn.docstatus = 1 and dn.is_return = 0
		order by dni.parent""",
		so_detail,
	)


def stock_would_change(so_detail):
	"""{"so_detail", "predicted", "reason", "rows", "header"}.

	``rows``: [{"name", "parent", "stored", "stock"}] for the Delivery Note Items whose billed_amt stock
	would change (stored value differs from stock's; stock's own rewrite of an equal value at or above
	2**23 is not drift). None when ``predicted`` is False: a Delivery Note with two rows on the line
	(stock's order between them is undefined) or a row with si_detail (not modelled); recompute_line
	then runs stock's walk. ``header``: header_drift of the line's Delivery Notes, from their stored
	items (so recompute rows first when ``rows`` is not empty)."""
	_require_guard()
	line = walk.line_stats(so_detail)
	out = {"so_detail": so_detail, "predicted": True, "reason": None, "rows": []}
	if line.si_detail:
		out.update(predicted=False, reason="si_detail", rows=None)
	elif line.parents != line.rows:
		out.update(predicted=False, reason="multi_item", rows=None)
	else:
		for row, value in walk.changes(so_detail):
			if row.billed_amt is None or value != row.billed_amt:
				out["rows"].append(
					{"name": row.name, "parent": row.parent, "stored": row.billed_amt, "stock": value}
				)
	out["header"] = header_drift(line_delivery_notes(so_detail))
	return out


def recompute_line(so_detail, update_modified=True):
	"""Write what stock's re-check of the line would write; returns the Delivery Notes it changed (all of
	the line's when stock's own walk ran). Their per_billed / status are NOT refreshed here: pass them,
	with header_drift's names, to refresh_dns."""
	line = walk.line_stats(so_detail)
	if guard.mismatches() or line.si_detail or line.parents != line.rows:
		return sorted(set(_stock_walk()(so_detail, update_modified)))
	changed = walk.changes(so_detail)
	for row, value in changed:
		frappe.db.set_value(
			"Delivery Note Item", row.name, "billed_amt", value, update_modified=update_modified
		)
	return sorted({row.parent for row, _value in changed})


def header_drift(names):
	"""[{"name", "per_billed": (stored, stock), "status": (stored, stock)}] for the Delivery Notes whose
	stored per_billed or status differs from what stock's refresh would store now. Status is compared
	as set_status would compute it after the per_billed fix."""
	_require_guard()
	names = list(names)
	stock_pb = sr.stock_per_billed(names)
	stored_pb = {}
	for chunk in sr.chunks(names):
		stored_pb.update(
			frappe.db.sql("select name, per_billed from `tabDelivery Note` where name in %(n)s", {"n": chunk})
		)
	statuses = sr.statuses(names, per_billed=stock_pb)
	out = []
	for name in names:
		if name not in statuses:
			continue  # no such Delivery Note
		pb = (flt(stored_pb.get(name)), stock_pb.get(name, 0.0))
		status = statuses[name]
		if pb[0] != pb[1] or status[0] != status[1]:
			out.append({"name": name, "per_billed": pb, "status": status})
	return out


def refresh_dns(names, update_modified=True):
	"""Stock's refresh of each Delivery Note: update_billing_percentage (per_billed, and with
	update_modified set_status, a Label comment on a change, notify_update)."""
	for name in names:
		frappe.get_doc(DN, name).update_billing_percentage(update_modified=update_modified)
