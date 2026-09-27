"""Stock's per-Delivery-Note billing refresh, computed for many DNs at once (IDEV-3268).

Shared by the invoice-side bulk refresh (bulk.py) and the repair / audit interface (api.py). What stock
does per DN, ERPNext v15.96.0 (the fingerprint pins each function):

1. StockController.update_billing_percentage picks target_ref_field from ``self.items`` in idx order:
   ``(amount - (returned_qty * rate))`` when sum(flt(returned_qty * rate)) < sum(flt(amount)), else
   ``amount``.
2. StatusUpdater._update_percent_field sets per_billed with the SQL expression in PER_BILLED (for one
   literal DN name; here correlated on ``dn.name``).
3. StatusUpdater.set_status evaluates status_map['Delivery Note'] in reverse on ``self.as_dict()`` of a
   freshly loaded DN; the first ``eval:`` condition that holds gives the status.
"""

import re

import frappe
from frappe.model import float_like_fields
from frappe.utils import cint, cstr, flt, getdate, nowdate

DN = "Delivery Note"
CHUNK = 1000
REF_AMOUNT = "amount"
REF_NET_OF_RETURNS = "(amount - (returned_qty * rate))"
# _update_percent_field's expression, target_dt 'Delivery Note Item', target_field billed_amt,
# target_parent_dt 'Delivery Note', correlated on the outer `dn` instead of a literal name
PER_BILLED = """round(
	ifnull((select
		ifnull(sum(case when abs({ref}) > abs(billed_amt) then abs(billed_amt) else abs({ref}) end), 0)
		/ sum(abs({ref})) * 100
	from `tabDelivery Note Item` where parent=dn.name and parenttype='Delivery Note'
	having sum(abs({ref})) > 0), 0), 6)"""
# the field types whose as_dict() value this module reproduces (BaseDocument.get_valid_dict)
STATUS_FIELD_TYPES = frozenset({"Check", "Int", "Float", "Currency", "Percent", "Select", "Data"})


def chunks(seq, size=CHUNK):
	seq = list(seq)
	for i in range(0, len(seq), size):
		yield seq[i : i + size]


def ref_fields(names):
	"""{DN name: target_ref_field} as update_billing_percentage picks it, the same float sums in the same
	order (items as get_doc loads them: parentfield 'items', idx order)."""
	totals = {}
	for chunk in chunks(names):
		for parent, amount, returned_qty, rate in frappe.db.sql(
			"""select parent, amount, returned_qty, rate from `tabDelivery Note Item`
			where parent in %(names)s and parenttype = 'Delivery Note' and parentfield = 'items'
			order by parent, idx""",
			{"names": chunk},
		):
			total = totals.setdefault(parent, [0, 0])
			total[0] += flt(amount)
			total[1] += flt(returned_qty * rate)
	out = {}
	for name in names:
		total_amount, total_returned = totals.get(name, (0, 0))
		out[name] = REF_NET_OF_RETURNS if total_returned < total_amount else REF_AMOUNT
	return out


def by_ref(names):
	"""{target_ref_field: [DN names]}."""
	groups = {}
	for name, ref in ref_fields(names).items():
		groups.setdefault(ref, []).append(name)
	return groups


def stock_per_billed(names):
	"""{DN name: the per_billed stock's refresh would store now, from the stored items}, as a float."""
	out = {}
	for ref, group in by_ref(names).items():
		for chunk in chunks(group):
			out.update(
				frappe.db.sql(
					f"select dn.name, {PER_BILLED.format(ref=ref)} from `tabDelivery Note` dn "
					"where dn.name in %(names)s",
					{"names": chunk},
				)
			)
	return {name: flt(value) for name, value in out.items()}


# ---- status ----------------------------------------------------------------------------------------------
def status_map():
	import erpnext.controllers.status_updater as su_mod

	return su_mod.status_map[DN]


def status_fields():
	"""The Delivery Note fields status_map's conditions read, plus status. Raises ValueError for anything
	this module cannot evaluate from column values exactly as set_status would."""
	meta = frappe.get_meta(DN)
	columns = set(frappe.db.get_table_columns(DN))
	fields = {"status"}
	for _label, cond in status_map():
		if not cond:
			continue
		if not cond.startswith("eval:"):
			raise ValueError(f"method condition {cond!r}")
		refs = re.findall(r"\bself\.([A-Za-z_]\w*)", cond)
		if len(refs) != len(re.findall(r"\bself\b", cond)) or not set(refs) <= columns:
			raise ValueError(f"condition {cond!r}")
		fields.update(refs)
	for field in fields:
		df = meta.get_field(field)
		if df and df.fieldtype not in STATUS_FIELD_TYPES:
			raise ValueError(f"field {field} of type {df.fieldtype}")
	return sorted(fields)


def as_dict_value(meta, fieldname, value):
	"""BaseDocument.get_valid_dict's normalisation for STATUS_FIELD_TYPES (what self.as_dict() holds)."""
	df = meta.get_field(fieldname)
	if not df:
		return value
	if df.fieldtype == "Check":
		return 1 if cint(value) else 0
	if df.fieldtype == "Int" and not isinstance(value, int):
		return cint(value)
	if df.fieldtype in float_like_fields and not isinstance(value, float):
		return flt(value)
	if getattr(df, "unique", False) and cstr(value).strip() == "":
		return None
	return value


def status_of(values):
	"""StatusUpdater.set_status's loop for one DN's field values."""
	context = {
		"self": frappe._dict(values),
		"getdate": getdate,
		"nowdate": nowdate,
		"get_value": frappe.db.get_value,
	}
	sl = status_map()[:]
	sl.reverse()
	for label, cond in sl:
		if not cond:
			return label
		if frappe.safe_eval(cond[5:], None, context):
			return label
	return values["status"]


def statuses(names, per_billed=None):
	"""{DN name: (stored status, the status set_status would give it now)}. ``per_billed`` overrides the
	stored per_billed ({name: value}); memoised per distinct input."""
	fields = status_fields()
	meta = frappe.get_meta(DN)
	cols = ", ".join(f"`{f}`" for f in fields)
	memo, out = {}, {}
	for chunk in chunks(names):
		for row in frappe.db.sql(
			f"select name, {cols} from `tabDelivery Note` where name in %(names)s",
			{"names": chunk},
			as_dict=True,
		):
			if per_billed is not None and row.name in per_billed:
				row["per_billed"] = per_billed[row.name]
			values = tuple(as_dict_value(meta, f, row[f]) for f in fields)
			if values not in memo:
				memo[values] = status_of(dict(zip(fields, values, strict=True)))
			out[row.name] = (row.status, memo[values])
	return out
