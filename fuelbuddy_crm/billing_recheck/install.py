"""Installing the billing re-check into ERPNext (IDEV-3268).

``install()`` patches this process, idempotently, and is called from crm's before_request and before_job
hooks, so every web request and every background job has it before any document code runs:

1. DeliveryNote.update_billing_status is wrapped (walk.wrap_update_billing_status: the return add-on).
   Done first, so a reduced walk can never run without it.
2. SalesInvoice.update_billing_status_in_dn is wrapped (bulk.wrap_update_billing_status_in_dn).
3. The name update_billed_amount_based_on_so is rebound, in delivery_note (DeliveryNote.update_billing_status
   looks it up there) and in sales_invoice (which imported the name), to walk.update_billed_amount_based_on_so.

The patches are inert until a switch in site_config.json is set (billing_recheck.config): each one then
calls the stock code it replaced. A process that never ran a request or job hook (bench console,
bench execute, patches) keeps stock code; if a switch is on there, the Delivery Note / Sales Invoice
submit and cancel hook ``count_not_installed`` counts it as ``not_installed``.
"""

import frappe

from fuelbuddy_crm.billing_recheck import fingerprint as fp
from fuelbuddy_crm.billing_recheck import observe

MARK = "_billing_recheck"
_STATE = {"walk": None, "patched": set()}


def stock_walk():
	"""ERPNext's own update_billed_amount_based_on_so, as it was before install() rebound the name."""
	return _STATE["walk"]


def install():
	from fuelbuddy_crm.billing_recheck import bulk, guard, walk

	dn_mod, si_mod, _sc, _su = guard.modules()
	current = dn_mod.update_billed_amount_based_on_so
	if not getattr(current, MARK, False):
		_STATE["walk"] = current  # first install, or the module was reloaded
	# Wrap each class once. Another app may later wrap our wrapper (fuelbuddy_dubai wraps
	# update_billing_status when it is imported); wrapping that again would run the add-on twice.
	for cls, attr, wrap in (
		(dn_mod.DeliveryNote, "update_billing_status", walk.wrap_update_billing_status),
		(si_mod.SalesInvoice, "update_billing_status_in_dn", bulk.wrap_update_billing_status_in_dn),
	):
		if (cls, attr) not in _STATE["patched"]:
			setattr(cls, attr, wrap(getattr(cls, attr)))
			_STATE["patched"].add((cls, attr))
	dn_mod.update_billed_amount_based_on_so = walk.update_billed_amount_based_on_so
	si_mod.update_billed_amount_based_on_so = walk.update_billed_amount_based_on_so


def installed():
	"""True when every patch is in place in this process."""
	try:
		from fuelbuddy_crm.billing_recheck import guard

		dn_mod, si_mod, _sc, _su = guard.modules()
		return bool(
			_STATE["walk"]
			and getattr(dn_mod.update_billed_amount_based_on_so, MARK, False)
			and getattr(si_mod.update_billed_amount_based_on_so, MARK, False)
			and (dn_mod.DeliveryNote, "update_billing_status") in _STATE["patched"]
			and (si_mod.SalesInvoice, "update_billing_status_in_dn") in _STATE["patched"]
		)
	except Exception:
		return False


# ---- hooks --------------------------------------------------------------------------------------------
def before_request():
	_install_quietly()


def before_job():
	_install_quietly()


def _install_quietly():
	"""A failed install must not break requests or jobs: stock code keeps running, and the event hook
	below counts ``not_installed`` whenever a switch is on."""
	try:
		install()
	except Exception as exc:
		observe.warn_once(f"install:{exc!r}", f"billing re-check install failed, stock code runs: {exc!r}")


def count_not_installed(doc, method=None):
	"""Delivery Note / Sales Invoice on_submit and on_cancel, after the controller ran the billing code:
	counts a switched-on event that ran stock code because this process was not installed."""
	if installed():
		return
	from fuelbuddy_crm.billing_recheck import config

	try:
		switched_on = config.walk_enabled() or config.bulk_over()
	except Exception:
		switched_on = False
	if switched_on:
		observe.count("not_installed", doctype=doc.doctype, name=doc.name, method=method)
		_install_quietly()


@frappe.whitelist()
def status():
	"""System Manager health check: switches, install state, guard result, bulk gaps and path counters."""
	frappe.only_for("System Manager")
	from fuelbuddy_crm.billing_recheck import bulk, config, guard

	driver = guard.driver_result()
	return {
		"switches": config.snapshot(),
		"installed": installed(),
		"guard_ok": guard.ok(),
		"guard_mismatch": guard.mismatches(),
		"versions": guard.code_result().get("versions"),
		"db_driver": {"driver": driver.get("driver"), "version": driver.get("version")},
		"bulk_gaps": bulk.safe_gap_reasons(),
		"counters": observe.counters(),
		"paths": list(observe.PATHS),
		"pinned": {app: sorted(v) for app, v in fp.load_pins().items() if app in fp.APPS},
	}
