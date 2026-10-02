# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Delivery Note.custom_qc_idempotency_key (IDEV-3266 quantity correction).

The episode key of the quantity correction that produced this Delivery Note. It goes only on
the resulting LIVE Delivery Note (the amendment, or the draft updated in place), is uniquely
indexed so one episode can never land on two Delivery Notes, and is no_copy so a duplicate
does not inherit it (fuelbuddy_crm.dn_versioning.drop_copied_idempotency_key also clears
it on a UI amendment, which copies no_copy fields). Set by
fuelbuddy_crm.api.qty_correction.amend_delivery_note only."""

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

FIELD = "custom_qc_idempotency_key"

CUSTOM_FIELDS = {
	"Delivery Note": [
		{
			"fieldname": FIELD,
			"fieldtype": "Data",
			"label": "Quantity Correction Key",
			"insert_after": "custom_invoiced_item_id",
			"unique": 1,
			"no_copy": 1,
			"read_only": 1,
			"allow_on_submit": 0,
			"description": "Set by the quantity-correction workflow (IDEV-3266): the episode that "
			"produced this Delivery Note. See DN Amend Log.",
		},
	],
}


def execute():
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
