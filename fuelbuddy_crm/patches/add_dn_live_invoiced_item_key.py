# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Delivery Note.custom_live_invoiced_item_id, and an index on custom_invoiced_item_id (IDEV-3269).

custom_live_invoiced_item_id is uniquely indexed and holds custom_invoiced_item_id only while the
Delivery Note is live (docstatus < 2, not a return); fuelbuddy_crm.dn_validation.set_live_invoiced_item_key
keeps it so. MariaDB has no partial unique index, and a unique custom_invoiced_item_id itself would
reject the cancelled originals of versioned amendments and the returns that copy the stamp. So the
database, not a validate-time read, refuses a second live Delivery Note for one invoiced_item -- the
check two concurrent inserts slip past. Unique NULLs do not clash, so Delivery Notes made before
this patch (NULL here) need no backfill and existing duplicates do not block the index.

custom_invoiced_item_id is a production Custom Field made through the UI (in no app's fixtures).
Every create looks Delivery Notes up by it; production turned its Index on (search_index) on
2026-08-05. Frappe drops an index whose field says search_index = 0 the next time it syncs the
table -- which the create_custom_fields below does -- so the flag is set first, and the sync then
adds `custom_invoiced_item_id_index` where it is missing. A field that is unique already has an
index and is left alone."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from fuelbuddy_crm.dn_validation import LIVE_KEY_FIELD, STAMP_FIELD

CUSTOM_FIELDS = {
	"Delivery Note": [
		{
			"fieldname": LIVE_KEY_FIELD,
			"fieldtype": "Data",
			"label": "Live Invoiced Item ID",
			"insert_after": STAMP_FIELD,
			"unique": 1,
			"no_copy": 1,
			"read_only": 1,
			"hidden": 1,
			"print_hide": 1,
			"description": "Invoiced Item ID while this Delivery Note is live (not cancelled, not a "
			"return); empty otherwise. Unique: one live Delivery Note per invoiced item (IDEV-3269).",
		},
	],
}


def execute():
	stamp = frappe.db.get_value(
		"Custom Field",
		{"dt": "Delivery Note", "fieldname": STAMP_FIELD},
		["name", "search_index", "unique"],
		as_dict=True,
	)
	if stamp and not stamp.search_index and not stamp.unique:
		frappe.db.set_value("Custom Field", stamp.name, "search_index", 1)
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
