# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Delivery Note billing fields: one-off repair and nightly drift audit (IDEV-3268).

Why: the IDEV-3268 billing re-check writes only the rows whose values change. Stock ERPNext
rewrites every DN on the Sales Order line on every event and, in passing, repairs a per_billed /
status that was already wrong; the re-check does not, so a DN that is stale today (for instance
after fuelbuddy_dubai's drain skipped the recompute) would stay stale. ``repair`` clears that
backlog before the re-check is switched on; ``audit`` looks for new drift every night.

"Stock" means what ERPNext's own code would write now:

- billed_amt: the SO-line walk (erpnext delivery_note.update_billed_amount_based_on_so), asked of
  the core module through the interface below. Without it, this check is skipped and says so.
- per_billed: StockController.update_billing_percentage -> StatusUpdater._update_percent_field
  (erpnext v15.96.0 stock_controller.py:1042-1063, status_updater.py:475-505):
  round(sum(min(|ref|, |billed_amt|)) / sum(|ref|) * 100, 6) over the DN's items, 0 when
  sum(|ref|) is 0; ref is ``amount``, or ``amount - returned_qty * rate`` when the DN's returned
  value is below its amount. ``_PCT`` is stock's SQL expression (its ``having sum(abs(ref)) > 0``
  written as a CASE, as it runs per DN in a GROUP BY), so MariaDB does the same DECIMAL
  arithmetic; the choice of ref is Python float sums over the items in idx order, as in stock.
- status: StatusUpdater.set_status (status_updater.py:184-228) over status_map["Delivery Note"]
  (:84-93), read at run time, walked in reverse, the first condition that holds
  (frappe.safe_eval) on the DN with that per_billed. A condition that is a method, or reads a
  field that is not a column, makes the status "unknown" (not compared) rather than guessed.

``repair(dry_run=True)`` reports what differs. With dry_run off it takes each drifted SO line's
lock and brings its billed_amt to stock (core ``recompute_line``), then refreshes the returned DNs
and every DN whose per_billed / status differs with stock's own code,
``frappe.get_doc("Delivery Note", name).update_billing_percentage(update_modified=True)``:
per_billed, modified, then set_status (a Label comment when the status really changes, db_set
status, notify_update), which is what the next stock walk would do to that DN. It commits every
chunk; a DN that fails is retried alone and reported while the rest of its chunk lands. Run it
off-peak: each refreshed DN costs about four writes, one Label comment per status change.

``audit()`` is read-only: it rolls back after every page, so it never holds a snapshot for long
and never writes; its one Error Log goes through defer_insert. Scheduled daily on the long queue
(hooks.py scheduler_events "daily_long": nightly_drift_audit).

    bench --site <site> execute fuelbuddy_crm.billing_repair.repair
    bench --site <site> execute fuelbuddy_crm.billing_repair.repair --kwargs "{'dry_run': 0}"
    bench --site <site> execute fuelbuddy_crm.billing_repair.audit
"""

import importlib
import json
import re

import frappe
from frappe.utils import cint, flt, getdate, nowdate

# Interface the IDEV-3268 core branch provides; imported defensively (``_core``), so this module
# works, minus the billed_amt check, while it is absent.
#
#   stock_would_change(so_detail: str) -> list[tuple[str, str, float]] | None
#       Read-only (plain reads, no writes, no locks): the Delivery Note Item rows on this SO line
#       whose stored billed_amt differs from what ERPNext's walk would write now, as
#       (dn_item, delivery_note, stock_billed_amt). [] when the line matches stock; None when it
#       cannot tell exactly (upgrade guard tripped, or a line shape it hands to stock).
#   recompute_line(so_detail: str, update_modified: bool = True) -> list[str]
#       The contract of stock update_billed_amount_based_on_so: writes billed_amt so the line
#       matches stock and returns the Delivery Notes the caller must refresh with
#       update_billing_percentage. The caller holds the SO line lock.
CORE_MODULE = "fuelbuddy_crm.billing_recheck.walk"

LOG_TITLE = "Billing drift"
SAMPLE = 20

REF_AMOUNT = "amount"
REF_NET = "(amount - (returned_qty * rate))"
# stock's per_billed: round(ifnull((select <this> ... having sum(abs(ref)) > 0), 0), 6)
_PCT = (
	"round(ifnull(case when sum(abs({ref})) > 0 then "
	"ifnull(sum(case when abs({ref}) > abs(billed_amt) then abs(billed_amt) else abs({ref}) end), 0)"
	" / sum(abs({ref})) * 100 end, 0), 6)"
)


def logger():
	return frappe.logger("billing_recheck", allow_site=True)


# ---- what stock would write --------------------------------------------------------------------
def _dn_status_rules():
	from erpnext.controllers.status_updater import status_map

	return status_map["Delivery Note"]


def stock_status(doc, rules):
	"""The status StatusUpdater.set_status picks for ``doc`` (a frappe._dict of DN fields), or None
	when a rule cannot be evaluated from those fields."""
	context = {"self": doc, "getdate": getdate, "nowdate": nowdate, "get_value": frappe.db.get_value}
	for status, condition in reversed(rules):
		if not condition:
			return status
		if not condition.startswith("eval:"):
			return None  # a controller method: not evaluable from stored fields
		if frappe.safe_eval(condition[5:], None, context):
			return status
	return None


def _status_fields(rules):
	"""The DN columns the status rules read, or None when one is not a column."""
	fields = {"status", "docstatus"}
	for _status, condition in rules:
		fields.update(re.findall(r"self\.(\w+)", condition or ""))
	if not all(frappe.db.has_column("Delivery Note", f) for f in fields):
		return None
	return sorted(fields)


def dn_drift(names):
	"""Read-only. The submitted DNs among ``names`` whose stored per_billed or status differs from
	what stock's refresh would write now: [{name, per_billed: (stored, stock), status: (stored,
	stock)}]; a status of None on the stock side means "not evaluable" and is not compared."""
	return [
		d
		for d in stock_billing(names)
		if d.per_billed[0] != d.per_billed[1] or (d.status[1] is not None and d.status[0] != d.status[1])
	]


def stock_billing(names):
	"""Read-only. For each submitted DN among ``names``: {name, per_billed: (stored, stock),
	status: (stored, stock)}, stock being what update_billing_percentage + set_status would write."""
	if not names:
		return []
	names = list(names)
	rules = _dn_status_rules()
	fields = _status_fields(rules)
	cols = ", ".join(f"`{f}`" for f in sorted({*(fields or ()), "per_billed", "status"}))
	parents = frappe.db.sql(
		f"select name, {cols} from `tabDelivery Note` where name in %(names)s and docstatus = 1 order by name",
		{"names": names},
		as_dict=True,
	)
	pct = {
		r.parent: r
		for r in frappe.db.sql(
			f"""select parent, {_PCT.format(ref=REF_AMOUNT)} as by_amount, {_PCT.format(ref=REF_NET)} as by_net
			from `tabDelivery Note Item` where parent in %(names)s and parenttype = 'Delivery Note'
			group by parent""",
			{"names": names},
			as_dict=True,
		)
	}
	totals = {}
	for r in frappe.db.sql(
		"""select parent, amount, returned_qty, rate from `tabDelivery Note Item`
		where parent in %(names)s and parenttype = 'Delivery Note' order by parent, idx""",
		{"names": names},
		as_dict=True,
	):
		t = totals.setdefault(r.parent, [0, 0])  # stock: total_amount, total_returned
		t[0] += flt(r.amount)
		t[1] += flt(flt(r.returned_qty) * flt(r.rate))

	out = []
	for p in parents:
		total_amount, total_returned = totals.get(p.name, (0, 0))
		row = pct.get(p.name)
		per_billed = flt((row.by_net if total_returned < total_amount else row.by_amount) if row else 0)
		status = None
		if fields is not None:
			status = stock_status(frappe._dict({f: p.get(f) for f in fields}, per_billed=per_billed), rules)
		out.append(
			frappe._dict(name=p.name, per_billed=(flt(p.per_billed), per_billed), status=(p.status, status))
		)
	return out


# ---- scope -------------------------------------------------------------------------------------
def _scope(from_date, to_date, so_details, alias="dn"):
	conds, params = [], {}
	if from_date:
		conds.append(f"{alias}.posting_date >= %(from_date)s")
		params["from_date"] = from_date
	if to_date:
		conds.append(f"{alias}.posting_date <= %(to_date)s")
		params["to_date"] = to_date
	if so_details:
		conds.append(
			f"""exists (select 1 from `tabDelivery Note Item` s
			where s.parent = {alias}.name and s.so_detail in %(so_details)s)"""
		)
		params["so_details"] = tuple(so_details)
	return "".join(f" and {c}" for c in conds), params


def _dn_pages(from_date, to_date, so_details, page_size):
	"""Submitted DN names in scope, in name order, ``page_size`` at a time (keyset, no OFFSET)."""
	where, params = _scope(from_date, to_date, so_details)
	after = ""
	while True:
		names = frappe.db.sql_list(
			f"""select dn.name from `tabDelivery Note` dn
			where dn.docstatus = 1 and dn.name > %(after)s{where} order by dn.name limit %(n)s""",
			{**params, "after": after, "n": cint(page_size)},
		)
		if not names:
			return
		yield names
		after = names[-1]


def _lines(from_date, to_date, so_details):
	if so_details:
		return sorted(set(so_details))
	where, params = _scope(from_date, to_date, None)
	return frappe.db.sql_list(
		f"""select distinct dni.so_detail from `tabDelivery Note Item` dni
		join `tabDelivery Note` dn on dn.name = dni.parent
		where dn.docstatus = 1 and dn.is_return = 0 and ifnull(dni.so_detail, '') != ''{where}
		order by dni.so_detail""",
		params,
	)


def _core():
	"""The core branch's interface module (CORE_MODULE), or None while it is not installed."""
	try:
		module = importlib.import_module(CORE_MODULE)
	except ImportError:
		return None
	if not all(callable(getattr(module, fn, None)) for fn in ("stock_would_change", "recompute_line")):
		return None
	return module


# ---- repair ------------------------------------------------------------------------------------
def repair(
	dry_run=True,
	from_date=None,
	to_date=None,
	so_details=None,
	lines=True,
	chunk_size=500,
	page_size=2000,
	sample=SAMPLE,
):
	"""Find DNs whose billing fields differ from stock's; with dry_run off, fix them with stock's code.

	Scope: submitted DNs, optionally by posting date and / or SO lines (``so_details``). ``lines``
	off skips the billed_amt (SO line) phase. Returns a report; nothing is written in a dry run."""
	dry_run = bool(cint(dry_run))
	chunk_size, page_size, sample = max(cint(chunk_size), 1), max(cint(page_size), 1), cint(sample)
	if isinstance(so_details, str):
		so_details = [so_details]
	refreshed, failures = set(), []
	report = frappe._dict(dry_run=dry_run, from_date=from_date, to_date=to_date, so_details=so_details)
	report.lines = (
		_line_phase(dry_run, from_date, to_date, so_details, chunk_size, sample, refreshed, failures)
		if cint(lines)
		else {"skipped": "lines=0"}
	)
	report.dns = _dn_phase(
		dry_run, from_date, to_date, so_details, chunk_size, page_size, sample, refreshed, failures
	)
	report.failed = len(failures)
	report.failures = failures[:sample]
	frappe.db.rollback()  # nothing of ours is pending: every write above is already committed
	logger().info(f"billing repair{' (dry run)' if dry_run else ''}: {summary(report)}")
	return report


def _line_phase(dry_run, from_date, to_date, so_details, chunk_size, sample, refreshed, failures):
	core = _core()
	if core is None:
		return {"skipped": f"{CORE_MODULE}.stock_would_change / recompute_line not available"}
	res = {"checked": 0, "drifted": 0, "rows": 0, "unknown": 0, "samples": [], "unknown_lines": []}
	for so_detail in _lines(from_date, to_date, so_details):
		res["checked"] += 1
		rows = core.stock_would_change(so_detail)
		frappe.db.rollback()  # end the read snapshot; stock_would_change writes nothing
		if rows is None:
			res["unknown"] += 1
			if len(res["unknown_lines"]) < sample:
				res["unknown_lines"].append(so_detail)
			continue
		if not rows:
			continue
		res["drifted"] += 1
		res["rows"] += len(rows)
		if len(res["samples"]) < sample:
			res["samples"].append({"so_detail": so_detail, "rows": len(rows), "first": list(rows[:3])})
		if dry_run:
			continue
		try:
			# lock first, so every read recompute_line makes is taken after the lock is granted
			frappe.db.sql("select name from `tabSales Order Item` where name = %s for update", so_detail)
			parents = core.recompute_line(so_detail, update_modified=True)
			frappe.db.commit()
		except Exception as exc:
			frappe.db.rollback()
			failures.append({"so_detail": so_detail, "error": str(exc)[:300]})
			continue
		_refresh(parents, chunk_size, refreshed, failures)
	return res


def _dn_phase(dry_run, from_date, to_date, so_details, chunk_size, page_size, sample, refreshed, failures):
	res = {"checked": 0, "drifted": 0, "per_billed": 0, "status_only": 0, "samples": []}
	if not dry_run:
		res.update(fixed=0, still_different=0, still_samples=[])
	for names in _dn_pages(from_date, to_date, so_details, page_size):
		res["checked"] += len(names)
		drift = dn_drift(names)
		frappe.db.rollback()  # end the read snapshot
		res["drifted"] += len(drift)
		for d in drift:
			res["per_billed" if d.per_billed[0] != d.per_billed[1] else "status_only"] += 1
			if len(res["samples"]) < sample:
				res["samples"].append(d)
		if dry_run or not drift:
			continue
		_refresh([d.name for d in drift], chunk_size, refreshed, failures, force=True)
		after = dn_drift([d.name for d in drift])
		frappe.db.rollback()
		res["fixed"] += len(drift) - len(after)
		res["still_different"] += len(after)
		res["still_samples"].extend(after[: max(sample - len(res["still_samples"]), 0)])
	return res


def _refresh(names, chunk_size, refreshed, failures, force=False):
	"""Stock's refresh of each DN, a commit per chunk; a chunk that fails is redone one DN at a time
	so only the failing DN is left out (and reported)."""
	todo = [n for n in sorted(set(names)) if force or n not in refreshed]
	for i in range(0, len(todo), chunk_size):
		chunk = todo[i : i + chunk_size]
		try:
			for name in chunk:
				_refresh_one(name)
			frappe.db.commit()
			refreshed.update(chunk)
		except Exception:
			frappe.db.rollback()
			for name in chunk:
				try:
					_refresh_one(name)
					frappe.db.commit()
					refreshed.add(name)
				except Exception as exc:
					frappe.db.rollback()
					failures.append({"delivery_note": name, "error": str(exc)[:300]})
		logger().info(f"billing repair: refreshed {min(i + chunk_size, len(todo))}/{len(todo)} DNs")


def _refresh_one(name):
	frappe.get_doc("Delivery Note", name).update_billing_percentage(update_modified=True)


# ---- audit -------------------------------------------------------------------------------------
def audit(from_date=None, to_date=None, lines=True, page_size=2000, sample=SAMPLE):
	"""Read-only: what repair() would change, plus the DN -> Sales Invoice link invariants."""
	report = repair(
		dry_run=True, from_date=from_date, to_date=to_date, lines=lines, page_size=page_size, sample=sample
	)
	report.links = _link_audit(cint(sample))
	frappe.db.rollback()
	return report


def _link_audit(sample):
	from fuelbuddy_crm.dn_invoice_link import audit_links

	return audit_links(sample)


def nightly_drift_audit():
	"""scheduler_events daily_long: run audit(), log a one-line summary, and write one Error Log
	(deferred, so outside any transaction) when anything differs."""
	report = audit()
	line = summary(report)
	logger().info(f"billing drift audit: {line}")
	if has_drift(report):
		frappe.log_error(
			title=f"{LOG_TITLE}: {line}"[:140],
			message=json.dumps(report, indent=1, default=str),
			defer_insert=True,
		)
	return report


def has_drift(report):
	lines = report.get("lines") or {}
	links = report.get("links") or {}
	return bool(
		(report.get("dns") or {}).get("drifted")
		or lines.get("drifted")
		or any(check.get("count") for check in links.values())
	)


def summary(report):
	dns, lines = report.get("dns") or {}, report.get("lines") or {}
	parts = [f"{dns.get('drifted', 0)} of {dns.get('checked', 0)} DNs differ"]
	if dns.get("per_billed") or dns.get("status_only"):
		parts[-1] += f" ({dns.get('per_billed', 0)} per_billed, {dns.get('status_only', 0)} status only)"
	if "skipped" in lines:
		parts.append("billed_amt not checked")
	else:
		parts.append(f"{lines.get('drifted', 0)} of {lines.get('checked', 0)} SO lines differ")
		if lines.get("unknown"):
			parts.append(f"{lines['unknown']} lines not checkable")
	for key, check in (report.get("links") or {}).items():
		if check.get("count"):
			parts.append(f"{check['count']} {key}")
	if "fixed" in dns:
		parts.append(f"fixed {dns['fixed']}, still different {dns['still_different']}")
	if report.get("failed"):
		parts.append(f"{report['failed']} failed")
	return "; ".join(parts)
