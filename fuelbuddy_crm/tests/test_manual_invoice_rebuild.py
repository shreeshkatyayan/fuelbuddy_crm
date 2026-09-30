# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""auto_invoicing.rebuild_lines_from_dn_range on a live ERPNext site (IDEV-3270).

A manual period invoice rebuilds its SO lines from the Delivery Notes in its DN window, taking only
what is not billed yet. ERPNext marks a DN billed for part of its value ``Partially Billed`` (the
``Partly Billed`` the filter used to name is the Purchase Receipt status); the rebuild must take that
DN's unbilled remainder, and nothing once the DN is fully billed.

With the site switch ``invoice_rebuild_by_litres`` on (patched on here, whatever the site says),
"not billed yet" is counted in litres: a DN whose litres are all invoiced at another rate is not
offered, and a part-invoiced DN offers exactly its remaining litres. The same cases run without a
site in test_rebuild_litres_pure.

Needs erpnext + fuelbuddy_crm and a company (setup wizard done). The production-only schema the
Delivery Note and Sales Invoice hooks read is stood in by ``ensure_prod_schema``, only where missing,
so on a production copy it changes nothing. Kept self-contained so this fix does not wait on the
IDEV-3266 test helpers (tests/qc_fixtures.py); fold into them once both are on main. Every test gets
its own customer.

    bench --site <site> run-tests --app fuelbuddy_crm --module fuelbuddy_crm.tests.test_manual_invoice_rebuild
"""

from unittest.mock import patch

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from fuelbuddy_crm.dn_invoice_link import LINK_FIELD

ITEM = "FB/FL/00001"  # the production fuel item (stock UOM Litre)
RATE = 3.51
STOCK_DATE = "2026-01-01"
FUEL_GROUP = "Fuel"  # the transaction type on fuel documents
DN_FROM, DN_TO = "2026-08-01", "2026-08-31"  # the manual invoice's DN window
LITRES_SWITCH = "fuelbuddy_crm.auto_invoicing.litres_rebuild_enabled"

# Child DocTypes that crm fixtures reference as Table fields; installed by other apps in
# production. Loading a Customer / Sales Order fails without them.
_STUB_CHILD_DOCTYPES = ("POC", "Slab Discount", "Opportunity HSE")

_PROD_FIELDS = {
	"Delivery Note": [
		# production: Data; dn_validation documents it as deliberately NOT unique
		{"fieldname": "custom_invoiced_item_id", "fieldtype": "Data", "label": "Invoiced Item ID"},
		# dn_versioning: "custom_version is no_copy=1 with default '1'"
		{
			"fieldname": "custom_version",
			"fieldtype": "Data",
			"label": "Version",
			"default": "1",
			"no_copy": 1,
			"insert_after": "custom_invoiced_item_id",
		},
	],
	"Sales Order Item": [
		{
			"fieldname": "custom_delivery_note_qty_in_draft",
			"fieldtype": "Float",
			"label": "Delivery Note Qty in Draft",
			"allow_on_submit": 1,
			"insert_after": "picked_qty",
		},
	],
	"Sales Invoice": [
		{
			"fieldname": "custom_dn_from_date",
			"fieldtype": "Date",
			"label": "DN From Date",
			"allow_on_submit": 1,
		},
		{"fieldname": "custom_dn_to_date", "fieldtype": "Date", "label": "DN To Date", "allow_on_submit": 1},
	],
}


# ---- site set-up (idempotent; commits, since it runs DDL) --------------------------------------
def ensure_prod_schema():
	for doctype in _STUB_CHILD_DOCTYPES:
		if not frappe.db.exists("DocType", doctype):
			frappe.get_doc(
				{
					"doctype": "DocType",
					"name": doctype,
					"module": "Fuelbuddy CRM",
					"custom": 1,
					"istable": 1,
					"fields": [{"fieldname": "value", "fieldtype": "Data", "label": "Value"}],
				}
			).insert(ignore_permissions=True)

	missing = {
		dt: [f for f in fields if not frappe.get_meta(dt).has_field(f["fieldname"])]
		for dt, fields in _PROD_FIELDS.items()
	}
	missing = {dt: fields for dt, fields in missing.items() if fields}
	if missing:
		create_custom_fields(missing, ignore_validate=True)

	# crm's own Delivery Note fields ship as a patch, which a fresh install marks done unrun.
	if not frappe.get_meta("Delivery Note").has_field(LINK_FIELD):
		from fuelbuddy_crm.patches import add_dn_sales_invoice_link

		add_dn_sales_invoice_link.execute()

	frappe.db.commit()
	for dt in ("Delivery Note", "Sales Order Item", "Sales Invoice"):
		frappe.clear_cache(doctype=dt)


def company():
	name = frappe.db.get_single_value("Global Defaults", "default_company")
	if not name:
		import unittest

		raise unittest.SkipTest("needs an ERPNext company (complete the setup wizard)")
	return name


def warehouse():
	return f"Default Warehouse - {frappe.get_cached_value('Company', company(), 'abbr')}"


def ensure_masters():
	"""The fuel item, the warehouse and stock in it."""
	if not frappe.db.exists("Item Group", FUEL_GROUP):
		frappe.get_doc(
			{"doctype": "Item Group", "item_group_name": FUEL_GROUP, "parent_item_group": "All Item Groups"}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Item", ITEM):
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": ITEM,
				"item_name": "HSD-10PPM",
				"description": "Diesel",
				"item_group": "All Item Groups",
				"stock_uom": "Litre",
				"is_stock_item": 1,
				"valuation_rate": 3.0,
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Warehouse", warehouse()):
		frappe.get_doc(
			{"doctype": "Warehouse", "warehouse_name": "Default Warehouse", "company": company()}
		).insert(ignore_permissions=True)

	actual = flt(frappe.db.get_value("Bin", {"item_code": ITEM, "warehouse": warehouse()}, "actual_qty"))
	if actual < 500_000:
		frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": company(),
				"posting_date": STOCK_DATE,
				"posting_time": "00:10:00",
				"set_posting_time": 1,
				"items": [
					{"item_code": ITEM, "qty": 2_000_000, "t_warehouse": warehouse(), "basic_rate": 3.0}
				],
			}
		).insert(ignore_permissions=True).submit()
	frappe.db.commit()


# ---- per-test documents ------------------------------------------------------------------------
def new_customer(tag):
	"""A customer of its own, with a billing address, so tests never see each other's orders."""
	name = f"QC {tag} {frappe.generate_hash(length=5)}"
	series = (frappe.get_meta("Customer").get_options("naming_series") or "").split("\n")[0] or None
	customer = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": name,
			"naming_series": series,
			"customer_group": "All Customer Groups",
			"territory": "All Territories",
			"custom_transaction_type": FUEL_GROUP,  # mandatory via crm fixtures
		}
	).insert(ignore_permissions=True)
	address = frappe.get_doc(
		{
			"doctype": "Address",
			"address_title": name,
			"address_type": "Billing",
			"address_line1": "Plot 1",
			"city": "Dubai",
			"country": "United Arab Emirates",
			"is_primary_address": 1,
			"links": [{"link_doctype": "Customer", "link_name": customer.name}],
		}
	).insert(ignore_permissions=True)
	return frappe._dict(customer=customer.name, address=address.name)


def make_sales_order(party, qty):
	so = frappe.get_doc(
		{
			"doctype": "Sales Order",
			"company": company(),
			"customer": party.customer,
			"customer_address": party.address,
			"transaction_date": DN_FROM,
			"delivery_date": DN_TO,
			"selling_price_list": "Standard Selling",
			"items": [
				{
					"item_code": ITEM,
					"qty": qty,
					"uom": "Litre",
					"rate": RATE,
					"warehouse": warehouse(),
					"delivery_date": DN_TO,
				}
			],
		}
	)
	so.insert(ignore_permissions=True)  # trusted path past block_manual_sales_order
	so.submit()
	return so


def make_delivery_note(party, lines, posting_date):
	"""A submitted DN; ``lines``: [(sales_order, litres)], one line per SO allocation the way
	erp-functions punches a DN that spills past a full order."""
	dn = frappe.get_doc(
		{
			"doctype": "Delivery Note",
			"company": company(),
			"customer": party.customer,
			"customer_address": party.address,
			"posting_date": posting_date,
			"posting_time": "10:30:00",
			"set_posting_time": 1,
			"selling_price_list": "Standard Selling",
			"custom_invoiced_item_id": frappe.generate_hash(length=32),
			"items": [
				{
					"item_code": ITEM,
					"qty": qty,
					"uom": so.items[0].uom,
					"conversion_factor": so.items[0].conversion_factor,
					"rate": so.items[0].rate,
					"warehouse": warehouse(),
					"against_sales_order": so.name,
					"so_detail": so.items[0].name,
				}
				for so, qty in lines
			],
		}
	).insert(ignore_permissions=True)
	dn.submit()
	return dn


def make_sales_invoice(so, submit=False, qty=None, rate=None):
	"""An invoice on ``so`` for the DN window, mapped from the SO the way a user raises one.
	Without ``qty`` it is the MANUAL period invoice (lines rebuilt from the DNs by the hook under
	test); with ``qty`` its one line bills exactly that (at ``rate`` when given: a discount or a
	price change) and the rebuild is skipped."""
	from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice as map_invoice

	si = map_invoice(so.name)
	si.custom_dn_from_date = DN_FROM
	si.custom_dn_to_date = DN_TO
	si.posting_date = si.due_date = DN_TO
	si.set_posting_time = 1
	if qty is not None:
		si.items[0].qty = qty
		if rate is not None:
			si.items[0].rate = rate
		si.flags.fb_auto_invoicing = True
	si.insert(ignore_permissions=True)
	if submit:
		si.submit()
	return si


def dn_status(dn):
	return frappe.db.get_value("Delivery Note", dn.name, "status")


class TestManualInvoiceRebuild(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_prod_schema()
		ensure_masters()

	def setUp(self):
		self.party = new_customer(self._testMethodName[5:30])

	def lines(self, si):
		return [(row.so_detail, flt(row.qty, 3)) for row in si.items]

	def test_split_dn_is_billed_on_the_second_order_after_the_first(self):
		"""The production case: a punch spills past a nearly full order, so one DN carries a line
		on each SO. Billing the first SO leaves the DN Partially Billed; the second SO's manual
		invoice must still take the DN's line on it."""
		so_a = make_sales_order(self.party, 600)
		so_b = make_sales_order(self.party, 5000)
		split = make_delivery_note(self.party, [(so_a, 100), (so_b, 150)], "2026-08-15")
		make_delivery_note(self.party, [(so_b, 250)], "2026-08-16")

		si_a = make_sales_invoice(so_a, submit=True)
		self.assertEqual(self.lines(si_a), [(so_a.items[0].name, 100)])
		self.assertEqual(dn_status(split), "Partially Billed")

		si_b = make_sales_invoice(so_b)
		self.assertEqual(self.lines(si_b), [(so_b.items[0].name, 400)])  # 150 on the split DN + 250

	def test_part_billed_line_gives_only_its_unbilled_remainder(self):
		"""ERPNext spreads an invoice built from the SO over that SO line's DNs oldest first, by
		amount; 300 L billed leaves this 400 L DN three-quarters billed. The manual invoice takes the
		100 L left, and once that bills the DN is Completed and nothing is left to take."""
		so = make_sales_order(self.party, 1000)
		dn = make_delivery_note(self.party, [(so, 400)], "2026-08-15")
		make_sales_invoice(so, submit=True, qty=300)
		self.assertEqual(dn_status(dn), "Partially Billed")

		si = make_sales_invoice(so, submit=True)
		self.assertEqual(self.lines(si), [(so.items[0].name, 100)])
		self.assertEqual(dn_status(dn), "Completed")

		with self.assertRaisesRegex(frappe.ValidationError, "No Delivery Notes found"):
			make_sales_invoice(so)

	def test_litres_fully_invoiced_at_a_discount_are_not_offered(self):
		"""Every litre invoiced at a discounted rate: ERPNext leaves the DN Partially Billed (1,280 of
		1,404 billed). Counting litres nothing is left; counting amounts ~35.33 L would be offered."""
		so = make_sales_order(self.party, 1000)
		dn = make_delivery_note(self.party, [(so, 400)], "2026-08-15")
		make_sales_invoice(so, submit=True, qty=400, rate=3.20)
		self.assertEqual(dn_status(dn), "Partially Billed")

		with patch(LITRES_SWITCH, return_value=True):
			with self.assertRaisesRegex(frappe.ValidationError, "No Delivery Notes found"):
				make_sales_invoice(so)
		with patch(LITRES_SWITCH, return_value=False):
			[(so_detail, qty)] = self.lines(make_sales_invoice(so))
		self.assertEqual(so_detail, so.items[0].name)
		self.assertAlmostEqual(qty, 400 * (1404 - 1280) / 1404, places=2)

	def test_litres_part_invoiced_dn_offers_only_its_remaining_litres(self):
		"""300 of 400 L invoiced at a higher price (1,170 of 1,404 billed): the manual invoice takes
		the 100 L left, not the 66.67 L the unbilled amount would give."""
		so = make_sales_order(self.party, 1000)
		dn = make_delivery_note(self.party, [(so, 400)], "2026-08-15")
		make_sales_invoice(so, submit=True, qty=300, rate=3.90)
		self.assertEqual(dn_status(dn), "Partially Billed")

		with patch(LITRES_SWITCH, return_value=True):
			si = make_sales_invoice(so, submit=True)
			self.assertEqual(self.lines(si), [(so.items[0].name, 100)])
			with self.assertRaisesRegex(frappe.ValidationError, "No Delivery Notes found"):
				make_sales_invoice(so)
