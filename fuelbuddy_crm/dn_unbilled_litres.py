# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Unbilled litres per Delivery Note line, for the manual invoice rebuild (IDEV-3270).

The manual period invoice (auto_invoicing.rebuild_lines_from_dn_range) takes, from the Delivery
Notes in its DN window, what is not invoiced yet. ERPNext answers "not invoiced yet" in money: an
invoice raised against a Sales Order line is poured over that line's Delivery Notes oldest first BY
AMOUNT (erpnext delivery_note.update_billed_amount_based_on_so), and a DN counts as billed once its
amount is covered. An invoice that carries the deal discount, or a DN valued at an older price,
never covers the DN amount even when every litre is invoiced, so an amount-based rebuild offers
those litres again.

Owner decision (29 Sep): the rebuild counts litres, never amounts. This module runs ERPNext's
pour in litres:

- delivered litres of a DN line: its stock qty, less the litres submitted return DNs took back
  from it (read from the return lines, not the stored ``returned_qty``);
- litres on live invoices: the stock qty of Sales Invoice Items on invoices that are not cancelled
  (Draft or Submitted: a Draft is on its way to the customer) and are not credit notes. An item
  that names the DN line (``dn_detail``) counts for that line; one that bills the Sales Order line
  (``so_detail`` only, no stock update) goes into the line's pool;
- the pool is poured over the Sales Order line's DN lines oldest first (posting date, time, name),
  filling each line before the next, as ERPNext pours amounts. A DN line delivered against a Sales
  Invoice (``si_detail``) counts as billed in full and takes its litres from the pool, as in ERPNext;
- unbilled litres = delivered litres less the litres that reached the line.

So a rebuild never offers more litres than the Sales Order line delivered and has not invoiced.
Which DN a litre lands on follows the oldest-first pour, not the invoice's DN window: a DN posted
back into an already invoiced window shows up as unbilled litres on the line's latest DNs.

Left out on purpose, so nothing is ever invoiced twice: a Closed DN takes no part (it is not
offered and does not soak up invoiced litres), and a credit note gives no litres back.

Switch: ``invoice_rebuild_by_litres`` (0/1) in the site's own site_config.json; off when absent. The
same key in common_site_config.json would switch every site on the bench at once, so it is ignored
there and logged. Switch it on only after the IDEV-3268 one-time billing repair has run on the site:

    bench --site <site> set-config invoice_rebuild_by_litres 1
"""

import json
import os

import frappe
from frappe.utils import cint, flt, getdate

SWITCH = "invoice_rebuild_by_litres"
EPS = 1e-6


def litres_rebuild_enabled():
	"""The site switch, read on every call from this site's site_config.json (not frappe.conf)."""
	try:
		with open(os.path.join(frappe.local.site_path, "site_config.json")) as fh:
			conf = json.load(fh)
	except (OSError, ValueError, AttributeError, TypeError):
		conf = {}
	if not isinstance(conf, dict):
		conf = {}
	if SWITCH not in conf and (getattr(frappe, "conf", None) or {}).get(SWITCH) is not None:
		frappe.logger("auto_invoicing").warning(
			f"{SWITCH} is set outside this site's site_config.json (common_site_config.json?) and is ignored"
		)
	return bool(cint(conf.get(SWITCH)))


def unbilled_rows(so_details, from_date, to_date):
	"""The DN lines on ``so_details`` posted in the window that still have litres to invoice, oldest
	first: ``dn``, ``posting_date``, ``so_detail``, ``item_code`` and ``qty``, the unbilled litres in
	the DN line's own UOM (the Sales Order line's), which is what auto_invoicing._split_lines sums."""
	if not so_details:
		return []
	so_details = tuple(so_details)
	lines = _dn_lines(so_details, to_date)
	pools, direct = _invoiced(so_details)
	left = unbilled_litres(lines, pools, direct, _returned(so_details))
	start, end = getdate(from_date), getdate(to_date)
	return [
		frappe._dict(
			dn=ln.dn,
			posting_date=ln.posting_date,
			so_detail=ln.so_detail,
			item_code=ln.item_code,
			qty=flt(left[ln.name] / (flt(ln.conversion_factor) or 1), 6),
		)
		for ln in lines
		if left[ln.name] > EPS and start <= getdate(ln.posting_date) <= end
	]


def unbilled_litres(lines, pools, direct, returned):
	"""The pour. ``lines``: DN lines oldest first, any mix of Sales Order lines; ``pools``:
	{so_detail: litres invoiced against the Sales Order line}; ``direct``: {DN line: litres invoiced
	against that DN line}; ``returned``: {DN line: litres returned}. Returns {DN line: unbilled
	litres}, never below zero."""
	pools = dict(pools)
	out = {}
	for ln in lines:
		delivered = max(litres(ln) - max(flt(returned.get(ln.name)), 0), 0)
		pool = flt(pools.get(ln.so_detail))
		if ln.get("si_detail"):
			billed = delivered
			pool -= delivered
		else:
			billed = flt(direct.get(ln.name))
		if pool > EPS and billed < delivered:
			take = min(delivered - billed, pool)
			billed += take
			pool -= take
		pools[ln.so_detail] = pool
		out[ln.name] = max(delivered - billed, 0)
	return out


def litres(row):
	"""A row's quantity in the stock UOM (litres), as dn_invoice_link reads it."""
	return flt(row.get("stock_qty")) or flt(row.get("qty")) * (flt(row.get("conversion_factor")) or 1)


# ---- reads -------------------------------------------------------------------------------------
def _dn_lines(so_details, to_date):
	"""Submitted, non-return, not Closed DN lines on the Sales Order lines, oldest first. Lines after
	the window cannot change the pour for the lines in it (they come later), so they are not read."""
	return frappe.db.sql(
		"""
		select dni.name, dni.parent as dn, dn.posting_date, dni.so_detail, dni.item_code,
			dni.qty, dni.stock_qty, dni.conversion_factor, dni.si_detail
		from `tabDelivery Note Item` dni
		join `tabDelivery Note` dn on dn.name = dni.parent
		where dn.docstatus = 1
			and ifnull(dn.is_return, 0) = 0
			and dn.status != 'Closed'
			and dn.posting_date <= %(to_date)s
			and dni.so_detail in %(so_details)s
		order by dn.posting_date, dn.posting_time, dn.name, dni.idx
		""",
		{"to_date": to_date, "so_details": so_details},
		as_dict=True,
	)


def _invoiced(so_details):
	"""Litres on live invoices (Draft or Submitted, not credit notes): ({so_detail: pooled litres},
	{DN line: litres billed against it}). The pool leaves out invoices that update stock themselves,
	as ERPNext's pour does."""
	pools = {}
	for row in frappe.db.sql(
		"""
		select sii.so_detail, sii.qty, sii.stock_qty, sii.conversion_factor
		from `tabSales Invoice Item` sii
		join `tabSales Invoice` si on si.name = sii.parent
		where si.docstatus < 2
			and ifnull(si.is_return, 0) = 0
			and ifnull(si.update_stock, 0) = 0
			and ifnull(sii.dn_detail, '') = ''
			and sii.so_detail in %(so_details)s
		""",
		{"so_details": so_details},
		as_dict=True,
	):
		pools[row.so_detail] = pools.get(row.so_detail, 0) + litres(row)

	direct = {}
	for row in frappe.db.sql(
		"""
		select sii.dn_detail, sii.qty, sii.stock_qty, sii.conversion_factor
		from `tabSales Invoice Item` sii
		join `tabSales Invoice` si on si.name = sii.parent
		join `tabDelivery Note Item` dni on dni.name = sii.dn_detail
		where si.docstatus < 2
			and ifnull(si.is_return, 0) = 0
			and dni.so_detail in %(so_details)s
		""",
		{"so_details": so_details},
		as_dict=True,
	):
		direct[row.dn_detail] = direct.get(row.dn_detail, 0) + litres(row)
	return pools, direct


def _returned(so_details):
	"""{DN line: litres taken back by submitted return DNs}."""
	out = {}
	for row in frappe.db.sql(
		"""
		select r.dn_detail, r.qty, r.stock_qty, r.conversion_factor
		from `tabDelivery Note Item` r
		join `tabDelivery Note` rdn on rdn.name = r.parent
		join `tabDelivery Note Item` o on o.name = r.dn_detail
		where rdn.docstatus = 1
			and rdn.is_return = 1
			and o.so_detail in %(so_details)s
		""",
		{"so_details": so_details},
		as_dict=True,
	):
		out[row.dn_detail] = out.get(row.dn_detail, 0) - litres(row)  # return lines are negative
	return out
