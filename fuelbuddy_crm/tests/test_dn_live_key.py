# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""One live Delivery Note per invoiced item, held by the database (IDEV-3269).

dn_validation.set_live_invoiced_item_key keeps Delivery Note.custom_live_invoiced_item_id equal to
custom_invoiced_item_id while the DN is live (docstatus < 2, not a return) and NULL otherwise;
patches/add_dn_live_invoiced_item_key makes it unique. These tests check the key through a DN's
life (draft, submit, cancel, amend, return, delete) and that MariaDB refuses a second live DN in
the case production hit: enforce_single_active_dn's validate-time read passes it.

Needs erpnext + fuelbuddy_crm and a company (setup wizard done). The production-only fields the
code reads (custom_invoiced_item_id, custom_version) are created where missing, and the patch is
run where its field is missing (a fresh install marks patches done without running them). Each
test uses its own invoiced-item id. The concurrency test commits one Delivery Note and deletes it
afterwards.

    bench --site <site> run-tests --app fuelbuddy_crm --module fuelbuddy_crm.tests.test_dn_live_key
"""

import unittest
from unittest import mock

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.tests.utils import FrappeTestCase

from fuelbuddy_crm.dn_validation import LIVE_KEY_FIELD, STAMP_FIELD
from fuelbuddy_crm.patches import add_dn_live_invoiced_item_key

ITEM = "IDEV-3269 Test Service"  # non-stock: DNs submit and cancel without stock or a warehouse
FUEL_GROUP = "Fuel"  # Customer.custom_transaction_type (mandatory through crm fixtures)

# Stand-ins for production fields, as in production (the 2026-06-03 dump; the stamp is no longer
# unique there). Created only where missing.
_PROD_FIELDS = {
	"Delivery Note": [
		{"fieldname": STAMP_FIELD, "fieldtype": "Data", "label": "Invoiced Item ID"},
		{
			"fieldname": "custom_version",
			"fieldtype": "Data",
			"label": "Version",
			"default": "1",
			"no_copy": 1,
			"insert_after": STAMP_FIELD,
		},
	],
}


def _company():
	name = frappe.db.get_single_value("Global Defaults", "default_company")
	if not name:
		raise unittest.SkipTest("needs an ERPNext company (complete the setup wizard)")
	return name


def _ensure_schema():
	missing = {
		dt: [f for f in fields if not frappe.get_meta(dt).has_field(f["fieldname"])]
		for dt, fields in _PROD_FIELDS.items()
	}
	missing = {dt: fields for dt, fields in missing.items() if fields}
	if missing:
		create_custom_fields(missing, ignore_validate=True)
	if not frappe.get_meta("Delivery Note").has_field(LIVE_KEY_FIELD):
		add_dn_live_invoiced_item_key.execute()
	frappe.db.commit()
	frappe.clear_cache(doctype="Delivery Note")


def _ensure_masters():
	if not frappe.db.exists("Item Group", FUEL_GROUP):
		frappe.get_doc(
			{"doctype": "Item Group", "item_group_name": FUEL_GROUP, "parent_item_group": "All Item Groups"}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Item", ITEM):
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": ITEM,
				"item_name": ITEM,
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		).insert(ignore_permissions=True)
	frappe.db.commit()


class TestDeliveryNoteLiveKey(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.company = _company()
		_ensure_schema()
		_ensure_masters()
		series = (frappe.get_meta("Customer").get_options("naming_series") or "").split("\n")[0] or None
		cls.customer = (
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": f"IDEV-3269 {frappe.generate_hash(length=5)}",
					"naming_series": series,
					"customer_group": "All Customer Groups",
					"territory": "All Territories",
					"custom_transaction_type": FUEL_GROUP,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)
		frappe.db.commit()

	def setUp(self):
		self.iid = frappe.generate_hash(length=32)

	# ---- helpers ------------------------------------------------------------------------------
	def new_dn(self, iid=None):
		return frappe.get_doc(
			{
				"doctype": "Delivery Note",
				"company": self.company,
				"customer": self.customer,
				"posting_date": "2026-09-25",
				"posting_time": "11:10:34",
				"set_posting_time": 1,
				STAMP_FIELD: iid or self.iid,
				"items": [
					{"item_code": ITEM, "qty": 100, "uom": "Nos", "conversion_factor": 1, "rate": 3.51}
				],
			}
		)

	def key(self, name):
		return frappe.db.get_value("Delivery Note", name, LIVE_KEY_FIELD)

	# ---- the key through a DN's life ----------------------------------------------------------
	def test_draft_and_submitted_dn_hold_the_key(self):
		dn = self.new_dn().insert(ignore_permissions=True)
		self.assertEqual(self.key(dn.name), self.iid)

		dn.submit()
		self.assertEqual(self.key(dn.name), self.iid)

	def test_cancel_frees_the_key_and_the_amendment_takes_it(self):
		original = self.new_dn().insert(ignore_permissions=True)
		original.submit()

		# The way fuelbuddy_crm's quantity-correction amend and ERPNext's Amend do it: cancel,
		# then a full copy (no_copy fields too) pointing at the original, in one transaction.
		original.cancel()
		self.assertIsNone(self.key(original.name))
		amendment = frappe.copy_doc(original)
		amendment.amended_from = original.name
		amendment.docstatus = 0
		for child in amendment.get_all_children():
			child.docstatus = 0
		amendment.insert(ignore_permissions=True)

		self.assertEqual(self.key(amendment.name), self.iid)

	def test_a_return_is_never_keyed_and_leaves_the_dn_its_key(self):
		from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_return

		dn = self.new_dn().insert(ignore_permissions=True)
		dn.submit()
		ret = make_sales_return(dn.name)
		self.assertEqual(ret.get(STAMP_FIELD), self.iid)  # the stamp is copied onto the return
		ret.insert(ignore_permissions=True)
		ret.submit()

		self.assertIsNone(self.key(ret.name))
		self.assertEqual(self.key(dn.name), self.iid)

	def test_a_deleted_draft_frees_the_key(self):
		first = self.new_dn().insert(ignore_permissions=True)
		frappe.delete_doc("Delivery Note", first.name)

		again = self.new_dn().insert(ignore_permissions=True)
		self.assertEqual(self.key(again.name), self.iid)

	def test_a_dn_without_a_stamp_is_not_keyed(self):
		if frappe.get_meta("Delivery Note").get_field(STAMP_FIELD).reqd:
			self.skipTest("custom_invoiced_item_id is mandatory on this site (as in production)")
		dn = self.new_dn()
		dn.set(STAMP_FIELD, None)
		dn.insert(ignore_permissions=True)
		self.assertIsNone(self.key(dn.name))

	# ---- the database refuses the second live DN ----------------------------------------------
	def test_second_live_dn_is_refused_even_when_the_validate_check_misses_it(self):
		self.new_dn().insert(ignore_permissions=True)

		with mock.patch("fuelbuddy_crm.dn_validation.enforce_single_active_dn", return_value=None):
			with self.assertRaises(frappe.UniqueValidationError):
				self.new_dn().insert(ignore_permissions=True)

	def test_concurrent_insert_is_refused_although_its_validate_read_cannot_see_the_first(self):
		"""The production race: the second insert's transaction opened its snapshot before the
		first insert committed, so enforce_single_active_dn (a plain read) passes it."""
		with self.secondary_connection():
			frappe.db.sql("select name from `tabDelivery Note` limit 1")  # opens the snapshot

		first = self.new_dn().insert(ignore_permissions=True)
		frappe.db.commit()
		self.addCleanup(self._delete_committed, first.name)

		with self.secondary_connection():
			if frappe.db.get_value("Delivery Note", first.name):
				self.skipTest("the DB is not REPEATABLE READ (MariaDB's default); the race needs it")
			with self.assertRaises(frappe.UniqueValidationError):
				self.new_dn().insert(ignore_permissions=True)

	def _delete_committed(self, name):
		with self.primary_connection():
			frappe.db.rollback()
			frappe.delete_doc("Delivery Note", name, force=True, ignore_permissions=True)
			frappe.db.commit()

	# ---- the patch ----------------------------------------------------------------------------
	def test_patch_leaves_the_key_unique_and_the_stamp_indexed(self):
		add_dn_live_invoiced_item_key.execute()  # runs again: no-op

		table = "tabDelivery Note"
		self.assertTrue(frappe.db.get_column_index(table, LIVE_KEY_FIELD, unique=True))
		self.assertTrue(
			frappe.db.get_column_index(table, STAMP_FIELD, unique=False)
			or frappe.db.get_column_index(table, STAMP_FIELD, unique=True)
		)
