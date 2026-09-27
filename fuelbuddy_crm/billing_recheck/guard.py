"""Runtime upgrade guard for the billing re-check (IDEV-3268).

The re-check is only proven equal to stock for the exact ERPNext and frappe code pinned in
fingerprint_pins.json. ``ok()`` is True only when all of this holds, else the callers run stock code:

- erpnext and frappe versions are pinned, and every pinned segment of the files this process loaded has
  the pinned fingerprint (billing_recheck.fingerprint; the same check the CI script runs);
- update_billed_amount_based_on_so is called exactly once in delivery_note.py and once in
  sales_invoice.py (the whole-package count runs in CI; a new caller elsewhere means a new, unpinned
  erpnext version);
- the delivery_note / sales_invoice modules this process imported are the files that were hashed, and
  the stock walk the re-check falls back to is that module's own function;
- PyMySQL is the pinned version (it formats the floats the 2**23 write-skip rule relies on);
- on this site, frappe resolves Delivery Note and Sales Invoice to ERPNext's own classes (no
  override_doctype_class / extend_doctype_class), and every method the re-check relies on is defined by
  the class that was hashed.

The code part is computed once per process on first use (about 0.1 s) and hashes the files right
after the modules were imported; the class part once per site. A mismatch is logged once per process
and raises one deferred Error Log per hour.
"""

import inspect
import os

import frappe

from fuelbuddy_crm.billing_recheck import fingerprint as fp
from fuelbuddy_crm.billing_recheck import observe

_CODE = {}
_CLASSES = {}


def ok():
	return not mismatches()


def mismatches():
	out = list(code_result()["mismatch"]) + list(class_result())
	if out:
		observe.warn_once(
			"guard:" + ",".join(out),
			"billing re-check DISABLED, stock ERPNext code runs: the running code differs from what it was "
			f"proven against ({', '.join(out)})",
		)
	return out


def code_result():
	if "result" in _CODE:
		return _CODE["result"]
	from fuelbuddy_crm.billing_recheck import install

	result = _check_code()
	if install.stock_walk() is not None:  # not before install() ran: that answer would stick
		_CODE["result"] = result
	return result


def class_result():
	site = getattr(frappe.local, "site", None)
	if site not in _CLASSES:
		_CLASSES[site] = _check_classes()
	return _CLASSES[site]


def reset():
	"""Forget the cached results (tests)."""
	_CODE.clear()
	_CLASSES.clear()


def modules():
	import erpnext.accounts.doctype.sales_invoice.sales_invoice as si_mod
	import erpnext.controllers.status_updater as su_mod
	import erpnext.controllers.stock_controller as sc_mod
	import erpnext.stock.doctype.delivery_note.delivery_note as dn_mod

	return dn_mod, si_mod, sc_mod, su_mod


def _check_code():
	mismatch, versions = [], {}
	try:
		import erpnext
		import pymysql

		pins = fp.load_pins()
		roots = {}
		for app, package in (("erpnext", erpnext), ("frappe", frappe)):
			root = os.path.dirname(os.path.dirname(os.path.abspath(package.__file__)))
			roots[app] = root
			versions[app] = package.__version__
			trees = fp.parse_modules(root, app)
			mismatch += fp.compare(app, package.__version__, fp.fingerprints(trees, app), pins)
			if app == "erpnext":
				for key in ("delivery_note", "sales_invoice"):
					if (n := fp.count_calls(trees[key])) != 1:
						mismatch.append(f"erpnext:{key} calls {fp.WALK} {n} times, expected 1")

		dn_mod, si_mod, _sc, _su = modules()
		for mod, key in ((dn_mod, "delivery_note"), (si_mod, "sales_invoice")):
			hashed = os.path.join(roots["erpnext"], fp.ERPNEXT_MODULES[key])
			if not os.path.samefile(inspect.getsourcefile(mod), hashed):
				mismatch.append(f"erpnext:{key} loaded from {inspect.getsourcefile(mod)}, hashed {hashed}")

		from fuelbuddy_crm.billing_recheck import install

		stock = install.stock_walk()
		if stock is None or not (
			stock.__module__ == dn_mod.__name__
			and stock.__qualname__ == fp.WALK
			and os.path.samefile(stock.__code__.co_filename, inspect.getsourcefile(dn_mod))
		):
			mismatch.append(f"stock walk is not ERPNext's own function: {stock!r}")

		# VERSION_STRING, not __version__: PyMySQL sets __version__ to a MySQLdb-compatible "1.4.6"
		versions["pymysql"] = getattr(pymysql, "VERSION_STRING", None)
		if versions["pymysql"] not in pins["runtime"]["pymysql"]:
			mismatch.append(f"pymysql {versions['pymysql']} not pinned")
	except Exception as exc:  # cannot prove equality -> stock
		mismatch.append(f"error:{exc!r}")
	return {"mismatch": mismatch, "versions": versions}


# (attribute, owner class name) the re-check relies on; "absent" must not be defined anywhere in the MRO
_DN_OWNERS = (
	("update_billing_status", "DeliveryNote"),
	("on_submit", "DeliveryNote"),
	("on_cancel", "DeliveryNote"),
	("update_prevdoc_status", "StatusUpdater"),
	("update_billing_percentage", "StockController"),
	("_update_percent_field", "StatusUpdater"),
	("_update_modified", "StatusUpdater"),
	("set_status", "StatusUpdater"),
	("get_status", None),
)
_SI_OWNERS = (
	("update_billing_status_in_dn", "SalesInvoice"),
	("on_submit", "SalesInvoice"),
	("on_cancel", "SalesInvoice"),
	("update_prevdoc_status", "StatusUpdater"),
)


def _check_classes():
	out = []
	try:
		dn_mod, si_mod, sc_mod, su_mod = modules()
		expected = {
			"DeliveryNote": dn_mod.DeliveryNote,
			"SalesInvoice": si_mod.SalesInvoice,
			"StockController": sc_mod.StockController,
			"StatusUpdater": su_mod.StatusUpdater,
		}
		for doctype, cls, owners in (
			("Delivery Note", dn_mod.DeliveryNote, _DN_OWNERS),
			("Sales Invoice", si_mod.SalesInvoice, _SI_OWNERS),
		):
			resolved = frappe.get_controller(doctype)
			if resolved is not cls:
				out.append(f"controller:{doctype} is {resolved.__module__}.{resolved.__qualname__}")
			out += owner_mismatches(resolved, owners, expected)
	except Exception as exc:
		out.append(f"error:{exc!r}")
	return out


def owner_mismatches(cls, owners, expected):
	"""Each attribute must be defined first (in MRO order) by its expected class, or nowhere for None."""
	out = []
	for attr, owner in owners:
		defining = next((c for c in cls.__mro__ if attr in c.__dict__), None)
		if owner is None:
			if defining is not None:
				out.append(f"mro:{cls.__qualname__}.{attr} defined by {defining.__qualname__}")
		elif defining is not expected[owner]:
			where = defining.__qualname__ if defining else "nothing"
			out.append(f"mro:{cls.__qualname__}.{attr} defined by {where}, expected {owner}")
	return out
