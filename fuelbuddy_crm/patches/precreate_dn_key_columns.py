# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""The Delivery Note key columns, before any Custom Field needs them (IDEV-3269, IDEV-3266).

In pre_model_sync, so it runs before every post_model_sync patch whatever their order -- in
particular before add_dn_live_invoiced_item_key and IDEV-3266's add_dn_qc_idempotency_key. Their
create_custom_fields then find the columns and unique indexes in place, and Frappe's sync changes
nothing in the table: no rebuild, and no moment when a Delivery Note save fails with "Unknown
column". See fuelbuddy_crm.dn_key_columns. Where the step already ran (by hand before the migrate,
docs/dn-key-columns.md), this only reads."""

from fuelbuddy_crm import dn_key_columns


def execute():
	"""Adds the Delivery Note key columns online (capped lock wait, retried), or confirms they are in place."""
	dn_key_columns.ensure(dn_key_columns.FrappeDB())
