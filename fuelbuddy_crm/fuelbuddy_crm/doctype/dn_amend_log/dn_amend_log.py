# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class DNAmendLog(Document):
	"""One row per quantity-correction episode that changed a Delivery Note (IDEV-3266).

	Written only by fuelbuddy_crm.api.qty_correction.amend_delivery_note, in the same
	transaction as the change, so a row exists if and only if the change committed. A retry
	or the workflow's plan step reads it back by idempotency key instead of re-amending."""
