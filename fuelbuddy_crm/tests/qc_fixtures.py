# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Test support for the quantity-correction tests (IDEV-3266).

A fresh ERPNext site with fuelbuddy_crm installed lacks parts of the production schema the
Delivery Note code reads: custom fields that exist only in production (made through the UI
long ago, never exported to a fixture) and the child DocTypes some crm fixtures point at
(they come from other production apps). ``ensure_prod_schema`` creates stand-ins that mirror
production — definitions from the 2026-06-03 production dump, or from the code that reads
them where production changed since — and only where missing, so on a production copy it
changes nothing. ``ensure_masters`` adds the master data the tests share.

Both are idempotent and commit (they run DDL). Also usable to prepare a local test site:

    bench --site <site> execute fuelbuddy_crm.tests.qc_fixtures.ensure_prod_schema
    bench --site <site> execute fuelbuddy_crm.tests.qc_fixtures.ensure_masters
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import flt

from fuelbuddy_crm.dn_invoice_link import LINK_FIELD

ITEM = "FB/FL/00001"  # the production fuel item (stock UOM Litre)
IG = "IG"  # production's Imperial Gallon UOM
IG_FACTOR = 4.546  # production's conversion factor for IG on FB/FL/00001
RATE_L = 3.51
RATE_IG = 15.95
STOCK_DATE = "2026-01-01"
FUEL_GROUP = "Fuel"  # erp-functions DELIVERY_NOTE.FUEL: the transaction type on fuel documents

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
		{
			"fieldname": "custom_total_quantity_in_ig",
			"fieldtype": "Float",
			"label": "Total Quantity in IG",
			"read_only": 1,
		},
		{"fieldname": "custom_department", "fieldtype": "Data", "label": "Department"},
		{"fieldname": "custom_billing_location", "fieldtype": "Data", "label": "Billing Location"},
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
		{"fieldname": "custom_location", "fieldtype": "Data", "label": "Location"},
		{"fieldname": "custom_department", "fieldtype": "Data", "label": "Department"},
	],
}


def ensure_prod_schema():
	"""Production-only schema the Delivery Note code depends on; only what is missing."""
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

	# crm's own Delivery Note fields ship as patches, which a fresh install marks done unrun.
	if not frappe.get_meta("Delivery Note").has_field(LINK_FIELD):
		from fuelbuddy_crm.patches import add_dn_sales_invoice_link

		add_dn_sales_invoice_link.execute()
	from fuelbuddy_crm.patches import add_dn_qc_idempotency_key

	if not frappe.get_meta("Delivery Note").has_field(add_dn_qc_idempotency_key.FIELD):
		add_dn_qc_idempotency_key.execute()

	frappe.db.commit()
	for dt in ("Delivery Note", "Sales Order Item", "Sales Invoice"):
		frappe.clear_cache(doctype=dt)


def company():
	name = frappe.db.get_single_value("Global Defaults", "default_company")
	if not name:
		import unittest

		raise unittest.SkipTest("needs an ERPNext company (complete the setup wizard)")
	return name


def abbr():
	return frappe.get_cached_value("Company", company(), "abbr")


def warehouse():
	return f"Default Warehouse - {abbr()}"


def ensure_masters():
	"""UOM IG, the fuel item with its IG conversion, the warehouse, a VAT template, stock."""
	comp = company()
	if not frappe.db.exists("UOM", IG):
		frappe.get_doc({"doctype": "UOM", "uom_name": IG, "must_be_whole_number": 0}).insert(
			ignore_permissions=True
		)
	if not frappe.db.exists("UOM Conversion Factor", {"from_uom": IG, "to_uom": "Litre"}):
		category = frappe.db.get_value("UOM Category", {"name": "Volume"}) or _uom_category("Volume")
		frappe.get_doc(
			{
				"doctype": "UOM Conversion Factor",
				"category": category,
				"from_uom": IG,
				"to_uom": "Litre",
				"value": IG_FACTOR,
			}
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
				"uoms": [
					{"uom": "Litre", "conversion_factor": 1},
					{"uom": IG, "conversion_factor": IG_FACTOR},
				],
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Warehouse", warehouse()):
		frappe.get_doc(
			{"doctype": "Warehouse", "warehouse_name": "Default Warehouse", "company": comp}
		).insert(ignore_permissions=True)

	_ensure_vat_template(comp)

	actual = flt(frappe.db.get_value("Bin", {"item_code": ITEM, "warehouse": warehouse()}, "actual_qty"))
	if actual < 500_000:
		se = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": comp,
				"posting_date": STOCK_DATE,
				"posting_time": "00:10:00",
				"set_posting_time": 1,
				"items": [
					{"item_code": ITEM, "qty": 2_000_000, "t_warehouse": warehouse(), "basic_rate": 3.0}
				],
			}
		).insert(ignore_permissions=True)
		se.submit()
	frappe.db.commit()


def _ensure_item_group(name):
	if not frappe.db.exists("Item Group", name):
		frappe.get_doc(
			{"doctype": "Item Group", "item_group_name": name, "parent_item_group": "All Item Groups"}
		).insert(ignore_permissions=True)
	return name


def vat_template():
	return f"UAE VAT 5% - {abbr()}"


def _uom_category(name):
	return (
		frappe.get_doc({"doctype": "UOM Category", "category_name": name})
		.insert(ignore_permissions=True)
		.name
	)


def _ensure_vat_template(comp):
	account = f"VAT 5% - {abbr()}"
	if not frappe.db.exists("Account", account):
		parent = frappe.db.get_value(
			"Account", {"company": comp, "account_type": "Tax", "is_group": 1}, "name"
		) or frappe.db.get_value("Account", {"company": comp, "account_name": "Duties and Taxes"}, "name")
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": "VAT 5%",
				"company": comp,
				"parent_account": parent,
				"account_type": "Tax",
				"tax_rate": 5,
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Sales Taxes and Charges Template", vat_template()):
		frappe.get_doc(
			{
				"doctype": "Sales Taxes and Charges Template",
				"title": "UAE VAT 5%",
				"company": comp,
				"taxes": [
					{
						"charge_type": "On Net Total",
						"account_head": account,
						"description": "VAT 5%",
						"rate": 5,
					}
				],
			}
		).insert(ignore_permissions=True)


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
			"custom_transaction_type": _ensure_item_group(FUEL_GROUP),  # mandatory via crm fixtures
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


def make_sales_order(party, qty, uom="Litre", transaction_date="2026-08-01", delivery_date="2026-08-31"):
	rate = RATE_IG if uom == IG else RATE_L
	so = frappe.get_doc(
		{
			"doctype": "Sales Order",
			"company": company(),
			"customer": party.customer,
			"customer_address": party.address,
			"transaction_date": transaction_date,
			"delivery_date": delivery_date,
			"selling_price_list": "Standard Selling",
			"taxes_and_charges": vat_template(),
			"taxes": frappe.get_doc("Sales Taxes and Charges Template", vat_template()).as_dict()["taxes"],
			"items": [
				{
					"item_code": ITEM,
					"qty": qty,
					"uom": uom,
					"rate": rate,
					"warehouse": warehouse(),
					"delivery_date": delivery_date,
				}
			],
		}
	)
	so.insert(ignore_permissions=True)  # trusted path past block_manual_sales_order
	so.submit()
	return so


def make_delivery_note(
	party, lines, posting_date="2026-08-15", posting_time="10:30:00", submit=False, iid=None
):
	"""``lines``: [(sales_order, qty in that SO line's uom)]. Built the way erp-functions
	punches a DN: one line per SO allocation, the SO line's uom, factor and rate."""
	first = lines[0][0]
	dn = frappe.get_doc(
		{
			"doctype": "Delivery Note",
			"company": company(),
			"customer": party.customer,
			"customer_address": party.address,
			"posting_date": posting_date,
			"posting_time": posting_time,
			"set_posting_time": 1,
			"selling_price_list": first.selling_price_list,
			"taxes_and_charges": first.taxes_and_charges,
			"taxes": [
				{k: t.get(k) for k in ("charge_type", "account_head", "description", "rate", "cost_center")}
				for t in first.taxes
			],
			"custom_invoiced_item_id": iid or frappe.generate_hash(length=32),
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
	if submit:
		dn.submit()
	return dn


def make_sales_invoice(so, from_date, to_date, submit=False):
	"""A period invoice billing ``so``'s line for the DN window [from_date, to_date]."""
	from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice as map_invoice

	si = map_invoice(so.name)
	si.custom_dn_from_date = from_date
	si.custom_dn_to_date = to_date
	si.posting_date = to_date
	si.set_posting_time = 1
	si.due_date = to_date
	for row in si.items:
		row.qty = row.qty or 1
	si.flags.fb_auto_invoicing = True  # lines as given; skip the manual DN-range rebuild
	si.insert(ignore_permissions=True)
	if submit:
		si.submit()
	return si
