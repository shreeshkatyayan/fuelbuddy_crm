"""Switches for the billing re-check (IDEV-3268), read on every call from the site's own site_config.json.

- ``billing_recheck_walk`` (0/1): the Delivery Note billing re-check replacement (fast path, changed-rows
  FIFO). Off when absent.
- ``billing_recheck_bulk_over`` (int N): refresh an invoice's Delivery Notes set-based when there are more
  than N of them. Off when absent or 0.
- ``billing_recheck_alert_rows`` (int): a slow path (a fallback to stock, guard off, not installed) on a
  Sales Order line with at least this many Delivery Note rows writes an Error Log. Default 1000.

Only site_config.json counts. The same key in common_site_config.json would switch every site on the
bench at once, so it is ignored and logged instead.
"""

import json
import os

import frappe
from frappe.utils import cint

WALK = "billing_recheck_walk"
BULK_OVER = "billing_recheck_bulk_over"
ALERT_ROWS = "billing_recheck_alert_rows"
KEYS = (WALK, BULK_OVER, ALERT_ROWS)
DEFAULT_ALERT_ROWS = 1000


def site_value(key):
	"""``key`` from this site's site_config.json (the file, not the merged frappe.conf), or None."""
	try:
		with open(os.path.join(frappe.local.site_path, "site_config.json")) as fh:
			conf = json.load(fh)
	except (OSError, ValueError, AttributeError, TypeError):
		conf = {}
	if key not in conf and frappe.conf.get(key) is not None:
		from fuelbuddy_crm.billing_recheck import observe

		observe.warn_once(
			f"conf:{key}",
			f"{key} is set outside this site's site_config.json (common_site_config.json?) and is ignored",
		)
	return conf.get(key)


def walk_enabled():
	return bool(cint(site_value(WALK)))


def bulk_over():
	return max(cint(site_value(BULK_OVER)), 0)


def alert_rows():
	value = site_value(ALERT_ROWS)
	return DEFAULT_ALERT_ROWS if value is None else cint(value)


def snapshot():
	return {key: site_value(key) for key in KEYS}
