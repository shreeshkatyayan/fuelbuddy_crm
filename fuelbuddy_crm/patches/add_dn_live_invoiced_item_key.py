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

The table comes first. fuelbuddy_crm.dn_key_columns.ensure adds the column and its unique index
online, under a capped lock wait, and checks them; precreate_dn_key_columns already ran it at the
start of the migrate, so here it normally only reads. Then the Custom Field is created, and Frappe's
sync of the table finds nothing to change. So the field is never committed without its column -- the
state in which every Delivery Note save fails with "Unknown column" and a second migrate does not
repair it -- and a run that stops anywhere is simply run again.

custom_invoiced_item_id is a production Custom Field made through the UI (in no app's fixtures).
Every create looks Delivery Notes up by it; production turned its Index on (search_index) on
2026-08-05. Frappe drops an index whose field says search_index = 0 the next time it syncs the
table, so ensure() sets the flag where the field is not unique, and builds
`custom_invoiced_item_id_index` online where it is missing."""

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from fuelbuddy_crm import dn_key_columns
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
	"""The live key's column and indexes, online (a read-only no-op once in place), then its Custom Field."""
	db = dn_key_columns.FrappeDB()
	dn_key_columns.ensure(db)
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
	# Frappe's sync had nothing to change; if it changed the columns after all, stop here, loudly.
	if problems := dn_key_columns.unready(dn_key_columns.read_state(db)):
		raise dn_key_columns.KeyColumnsError(
			"After creating the Custom Field, the Delivery Note key columns are not as Frappe builds "
			f"them ({dn_key_columns.DOCS}):\n- " + "\n- ".join(problems)
		)
