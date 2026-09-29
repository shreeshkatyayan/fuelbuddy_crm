# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Delivery Note billing fields: one-off repair and nightly drift audit (IDEV-3268).

Why: the IDEV-3268 billing re-check writes only the rows whose values change. Stock ERPNext
rewrites every DN on the Sales Order line on every event and, in passing, repairs a per_billed /
status that was already wrong; the re-check does not, so a DN that is stale today (for instance
after fuelbuddy_dubai's drain skipped the recompute) would stay stale. ``repair`` clears that
backlog before the re-check is switched on; ``audit`` looks for new drift every night.

"Stock" means what ERPNext's own code would write now. Both checks are the billing re-check's
(fuelbuddy_crm.billing_recheck.api), so there is one copy of stock's arithmetic, pinned by its
upgrade guard:

- billed_amt: ``api.stock_would_change(so_detail)``, the SO-line walk
  (erpnext delivery_note.update_billed_amount_based_on_so) computed read-only.
- per_billed / status: ``api.header_drift(names)``, what update_billing_percentage + set_status
  would store from the DN's stored items (billing_recheck.stock_refresh).

When the running ERPNext / frappe code is not the pinned code (api.GuardError), or a status rule
cannot be evaluated from columns, the check cannot be exact: that phase is skipped, the report
says why, and the nightly audit alerts.

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

import frappe
from frappe.utils import cint

# The billing re-check's repair / audit interface (fuelbuddy_crm.billing_recheck.api), imported on
# first use (``_core``) so this module loads without it in the SQLite tests. What this module uses:
#
#   stock_would_change(so_detail, header=False) -> {"predicted", "reason", "rows", ...}
#       Read-only. ``rows``: [{"name", "parent", "stored", "stock"}] for the Delivery Note Items
#       whose billed_amt stock's walk would change ([] when the line matches stock); None with
#       ``predicted`` False for a line it hands to stock (a DN with two rows on it, si_detail rows).
#   recompute_line(so_detail, update_modified=True) -> [DN names]
#       Writes billed_amt so the line matches stock (stock's own walk on a line it hands to stock)
#       and returns the Delivery Notes to refresh. The caller holds the SO line lock.
#   header_drift(names) -> [{"name", "per_billed": (stored, stock), "status": (stored, stock)}]
#       Read-only: the DNs whose stored per_billed / status differ from stock's refresh.
#   GuardError: the running ERPNext / frappe code is not the pinned code (both reads raise it).
CORE_MODULE = "fuelbuddy_crm.billing_recheck.api"
INSTALL_MODULE = "fuelbuddy_crm.billing_recheck.install"

LOG_TITLE = "Billing drift"
SAMPLE = 20


def logger():
	return frappe.logger("billing_recheck", allow_site=True)


def _core():
	"""billing_recheck.api, with the billing re-check installed in this process first. Its upgrade
	guard also checks that the stock walk it falls back to is ERPNext's own function, which it only
	knows once install() ran; ``bench execute`` runs no before_request / before_job hook, so without
	this every check here would report the guard as tripped. Idempotent; the repair itself only
	calls stock code (update_billing_percentage) and api.recompute_line, which the patches leave alone."""
	importlib.import_module(INSTALL_MODULE).install()
	return importlib.import_module(CORE_MODULE)


class NotCheckable(Exception):
	"""The stock values cannot be computed exactly here (see the module docstring)."""


# ---- what stock would write --------------------------------------------------------------------
def dn_drift(names):
	"""Read-only. The DNs among ``names`` whose stored per_billed or status differs from what stock's
	refresh would write now: [{name, per_billed: (stored, stock), status: (stored, stock)}].
	Raises NotCheckable when that cannot be computed exactly."""
	if not names:
		return []
	core = _core()
	try:
		drift = core.header_drift(list(names))
	except (core.GuardError, ValueError) as exc:  # ValueError: a status rule not evaluable
		raise NotCheckable(f"{type(exc).__name__}: {exc}"[:300]) from exc
	return [frappe._dict(d) for d in drift]


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
	res = {"checked": 0, "drifted": 0, "rows": 0, "unknown": 0, "samples": [], "unknown_lines": []}
	for so_detail in _lines(from_date, to_date, so_details):
		try:
			found = core.stock_would_change(so_detail, header=False)
		except core.GuardError as exc:  # the same for every line: stop here
			frappe.db.rollback()
			res["skipped"] = f"upgrade guard: {exc}"[:300]
			return res
		frappe.db.rollback()  # end the read snapshot; stock_would_change writes nothing
		res["checked"] += 1
		if not found["predicted"]:  # a line stock's own walk handles; recompute_line would run it
			res["unknown"] += 1
			if len(res["unknown_lines"]) < sample:
				res["unknown_lines"].append({"so_detail": so_detail, "reason": found["reason"]})
			continue
		rows = found["rows"]
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
		try:
			drift = dn_drift(names)
		except NotCheckable as exc:  # the same for every page: stop here
			frappe.db.rollback()
			res["skipped"] = str(exc)
			return res
		frappe.db.rollback()  # end the read snapshot
		res["checked"] += len(names)
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
	(deferred, so outside any transaction) when anything differs or a check could not run."""
	report = audit()
	line = summary(report)
	logger().info(f"billing drift audit: {line}")
	if has_drift(report) or not_checked(report):
		frappe.log_error(
			title=f"{LOG_TITLE}: {line}"[:140],
			message=json.dumps(report, indent=1, default=str),
			defer_insert=True,
		)
	return report


def not_checked(report):
	"""The phases that were meant to run but could not check exactly (upgrade guard, status rules);
	``lines=0`` is a choice, not a failure."""
	return [
		key for key in ("lines", "dns") if (report.get(key) or {}).get("skipped") not in (None, "lines=0")
	]


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
	if "skipped" in dns:
		parts = [f"per_billed / status not checked ({dns['skipped']})"]
	else:
		parts = [f"{dns.get('drifted', 0)} of {dns.get('checked', 0)} DNs differ"]
	if dns.get("per_billed") or dns.get("status_only"):
		parts[-1] += f" ({dns.get('per_billed', 0)} per_billed, {dns.get('status_only', 0)} status only)"
	if "skipped" in lines:
		parts.append(f"billed_amt not checked ({lines['skipped']})")
	else:
		parts.append(f"{lines.get('drifted', 0)} of {lines.get('checked', 0)} SO lines differ")
	if lines.get("unknown"):
		parts.append(f"{lines['unknown']} lines left to stock's walk")
	for key, check in (report.get("links") or {}).items():
		if check.get("count"):
			parts.append(f"{check['count']} {key}")
	if "fixed" in dns:
		parts.append(f"fixed {dns['fixed']}, still different {dns['still_different']}")
	if report.get("failed"):
		parts.append(f"{report['failed']} failed")
	return "; ".join(parts)
