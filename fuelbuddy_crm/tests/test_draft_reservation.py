# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""dn_validation.sync_draft_reservation on a live ERPNext site (IDEV-3266).

``Sales Order Item.custom_delivery_note_qty_in_draft`` must equal what the live draft Delivery
Notes hold on that line, for every line a save touches: the lines the DN points at now and the
lines it pointed at before the save (a dropped or re-pointed line must give its qty back).

Needs erpnext + fuelbuddy_crm and a company (setup wizard done). Production-only schema is stood
in by qc_fixtures.ensure_prod_schema. Every test gets its own customer.

    bench --site <site> run-tests --app fuelbuddy_crm --module fuelbuddy_crm.tests.test_draft_reservation
"""

import time
import uuid

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from fuelbuddy_crm.api import qty_correction
from fuelbuddy_crm.tests import qc_fixtures as fx

FUTURE = "2099-01-01T05:00:00.000Z"

# erp-functions src/erp/updateDeliveryNote.js toCleanLine: the fields it PUTs for each line (no
# child-row name, so Frappe rebuilds the table), less its two production-only custom fields.
CLEAN_LINE_FIELDS = (
	"item_code",
	"item_name",
	"qty",
	"uom",
	"conversion_factor",
	"rate",
	"price_list_rate",
	"base_price_list_rate",
	"discount_percentage",
	"discount_amount",
	"base_rate",
	"against_sales_order",
	"so_detail",
)


class TestDraftReservation(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_prod_schema()
		fx.ensure_masters()

	def setUp(self):
		self.party = fx.new_customer(self._testMethodName[5:30])
		self.so_a = fx.make_sales_order(self.party, 600)
		self.so_b = fx.make_sales_order(self.party, 5000)
		# A two-line draft, the way a punch spills past a full order, and a second draft on the
		# second line that no save below touches: its hold must survive every one of them.
		self.dn = fx.make_delivery_note(self.party, [(self.so_a, 600), (self.so_b, 400)])
		self.other = fx.make_delivery_note(self.party, [(self.so_b, 250)])
		self.assertReserved((self.so_a, 600), (self.so_b, 650))

	# ---- helpers ------------------------------------------------------------------------------
	def held_by_drafts(self, so_detail):
		return flt(
			frappe.db.sql(
				"""select coalesce(sum(dni.qty), 0) from `tabDelivery Note Item` dni
				join `tabDelivery Note` dn on dn.name = dni.parent
				where dni.so_detail = %s and dn.docstatus = 0""",
				so_detail,
			)[0][0]
		)

	def assertReserved(self, *expected):
		"""``expected``: (sales_order, qty) pairs. Each line reserves that qty, and that qty is what
		the live drafts hold on it."""
		for so, qty in expected:
			line = so.items[0].name
			reserved = flt(frappe.db.get_value("Sales Order Item", line, "custom_delivery_note_qty_in_draft"))
			self.assertEqual((reserved, self.held_by_drafts(line)), (qty, qty), so.name)

	def lines(self, dn):
		return [(row.so_detail, row.qty) for row in frappe.get_doc("Delivery Note", dn.name).items]

	# ---- tests --------------------------------------------------------------------------------
	def test_line_dropped_by_an_amend_is_released(self):
		r = qty_correction.amend_delivery_note(
			delivery_note=self.dn.name,
			target_qty=500,
			idempotency_key=f"{uuid.uuid4()}-{int(time.time() * 1000)}",
			not_after=FUTURE,
		)

		self.assertTrue(r["ok"], r)
		self.assertEqual(r["result"], "DRAFT_UPDATED")
		self.assertEqual(self.lines(self.dn), [(self.so_a.items[0].name, 500)])  # trimmed from the end
		self.assertReserved((self.so_a, 500), (self.so_b, 250))

	def test_line_left_out_of_a_replaced_items_table_is_released(self):
		"""erp-functions' legacy updateDeliveryNote PUTs the whole items table; Frappe's REST
		update is doc.update(body) + doc.save(), which deletes every row not in the body."""
		first = {field: self.dn.items[0].get(field) for field in CLEAN_LINE_FIELDS}
		first["qty"] = 500
		first["warehouse"] = fx.warehouse()  # a fresh test site has no default for ERPNext to fill in
		doc = frappe.get_doc("Delivery Note", self.dn.name)
		doc.update({"items": [first]})
		doc.save()

		self.assertEqual(self.lines(self.dn), [(self.so_a.items[0].name, 500)])
		self.assertReserved((self.so_a, 500), (self.so_b, 250))

	def test_line_re_pointed_to_another_sales_order_line_is_released(self):
		doc = frappe.get_doc("Delivery Note", self.dn.name)
		row = doc.items[0]
		row.against_sales_order = self.so_b.name
		row.so_detail = self.so_b.items[0].name  # the same child row, moved to the other order's line
		doc.save()

		line_b = self.so_b.items[0].name
		self.assertEqual(self.lines(self.dn), [(line_b, 600), (line_b, 400)])
		self.assertReserved((self.so_a, 0), (self.so_b, 1250))

	def test_deleted_draft_releases_every_line(self):
		frappe.delete_doc("Delivery Note", self.dn.name)

		self.assertReserved((self.so_a, 0), (self.so_b, 250))

	def test_save_that_changes_nothing_keeps_every_line(self):
		frappe.get_doc("Delivery Note", self.dn.name).save()

		self.assertEqual(
			self.lines(self.dn), [(self.so_a.items[0].name, 600), (self.so_b.items[0].name, 400)]
		)
		self.assertReserved((self.so_a, 600), (self.so_b, 650))
