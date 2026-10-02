# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class QCHold(Document):
	"""The ERP-side "correction pending" mark for one invoiced item (IDEV-3266).

	Written only by fuelbuddy_crm.api.qc_hold (open_hold / close_hold), which the
	QuantityCorrectionWorkflow calls through erp-functions when a ticket's run starts and when
	it ends. Read by fuelbuddy_crm.invoice_hold.held_deliveries: an Open, unexpired hold stops
	any invoice that would cover the held Delivery Note."""
