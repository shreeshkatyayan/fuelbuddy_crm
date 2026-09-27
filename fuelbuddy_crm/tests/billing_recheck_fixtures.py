# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Documents for the billing re-check site tests (IDEV-3268).

Plain ERPNext documents on one stock item, built the way crm's own tests build them (inserted with
ignore_permissions, which is the trusted path past block_manual_sales_order; Sales Invoices flagged
fb_auto_invoicing, so crm keeps their lines as given). Every test gets its own customer.
"""

import contextlib
import unittest

import frappe
from frappe.utils import flt

ITEM = "BR-TEST-FUEL"
RATE = 3.51
STOCK_DATE = "2026-01-01"


def company():
	name = frappe.db.get_single_value("Global Defaults", "default_company")
	if not name:
		raise unittest.SkipTest("needs an ERPNext company (complete the setup wizard)")
	return name


def warehouse():
	name = frappe.db.get_single_value("Stock Settings", "default_warehouse")
	if name and frappe.db.get_value("Warehouse", name, "company") == company():
		return name
	name = frappe.db.get_value("Warehouse", {"company": company(), "is_group": 0}, "name")
	if not name:
		raise unittest.SkipTest("needs a warehouse of the default company")
	return name


def ensure_masters():
	if not frappe.db.exists("Item", ITEM):
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": ITEM,
				"item_name": "Billing re-check test fuel",
				"item_group": "All Item Groups",
				"stock_uom": "Litre" if frappe.db.exists("UOM", "Litre") else "Nos",
				"is_stock_item": 1,
				"valuation_rate": 3.0,
			}
		).insert(ignore_permissions=True)
	actual = flt(frappe.db.get_value("Bin", {"item_code": ITEM, "warehouse": warehouse()}, "actual_qty"))
	if actual < 1_000_000:
		se = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": company(),
				"posting_date": STOCK_DATE,
				"posting_time": "00:10:00",
				"set_posting_time": 1,
				"items": [
					{"item_code": ITEM, "qty": 5_000_000, "t_warehouse": warehouse(), "basic_rate": 3.0}
				],
			}
		).insert(ignore_permissions=True)
		se.submit()
	frappe.db.commit()


def new_customer(tag):
	doc = {
		"doctype": "Customer",
		"customer_name": f"BR {tag} {frappe.generate_hash(length=6)}",
		"customer_group": "All Customer Groups",
		"territory": "All Territories",
	}
	if frappe.get_meta("Customer").has_field("custom_transaction_type"):  # mandatory via crm fixtures
		if not frappe.db.exists("Item Group", "Fuel"):
			frappe.get_doc(
				{"doctype": "Item Group", "item_group_name": "Fuel", "parent_item_group": "All Item Groups"}
			).insert(ignore_permissions=True)
		doc["custom_transaction_type"] = "Fuel"
	return frappe.get_doc(doc).insert(ignore_permissions=True).name


def make_sales_order(customer, qty, date="2026-08-01"):
	so = frappe.get_doc(
		{
			"doctype": "Sales Order",
			"company": company(),
			"customer": customer,
			"transaction_date": date,
			"delivery_date": "2026-08-31",
			"selling_price_list": "Standard Selling",
			"items": [
				{
					"item_code": ITEM,
					"qty": qty,
					"rate": RATE,
					"warehouse": warehouse(),
					"delivery_date": "2026-08-31",
				}
			],
		}
	)
	so.insert(ignore_permissions=True)
	so.submit()
	return so


def make_delivery_note(customer, lines, posting_date, posting_time="10:00:00", submit=True, rate=RATE):
	"""``lines``: [(sales order or None, qty)]; one row per entry (two entries on one order give a
	Delivery Note with two rows on that line)."""
	items = []
	for so, qty in lines:
		row = {"item_code": ITEM, "qty": qty, "rate": rate, "warehouse": warehouse()}
		if so is not None:
			row.update(against_sales_order=so.name, so_detail=so.items[0].name, rate=so.items[0].rate)
		items.append(row)
	dn = frappe.get_doc(
		{
			"doctype": "Delivery Note",
			"company": company(),
			"customer": customer,
			"posting_date": posting_date,
			"posting_time": posting_time,
			"set_posting_time": 1,
			"selling_price_list": "Standard Selling",
			"items": items,
		}
	).insert(ignore_permissions=True)
	if submit:
		dn.submit()
	return dn


def make_so_invoice(so_name, qty, posting_date="2026-08-31", submit=True, discount_percentage=0):
	"""An invoice billing ``qty`` against the Sales Order line directly (so_detail, no dn_detail)."""
	from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

	si = make_sales_invoice(so_name)
	si.posting_date = si.due_date = posting_date
	si.set_posting_time = 1
	for row in si.items:
		row.qty = qty
		if discount_percentage:
			row.discount_percentage = discount_percentage
	si.flags.fb_auto_invoicing = True
	si.insert(ignore_permissions=True)
	if submit:
		si.submit()
	return si


def make_dn_invoice(dn_name, submit=True):
	"""An invoice billing a Delivery Note (items carry dn_detail)."""
	from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

	si = make_sales_invoice(dn_name)
	si.flags.fb_auto_invoicing = True
	si.insert(ignore_permissions=True)
	if submit:
		si.submit()
	return si


def make_return(dn_name, qty, submit=True):
	"""A return Delivery Note of ``qty`` (positive number) against ``dn_name``."""
	from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_return

	ret = make_sales_return(dn_name)
	for row in ret.items:
		row.qty = -abs(qty)
	ret.insert(ignore_permissions=True)
	if submit:
		ret.submit()
	return ret


def make_credit_note(si_name, qty, submit=True):
	from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return

	cn = make_sales_return(si_name)
	cn.update_billed_amount_in_sales_order = 1
	cn.update_billed_amount_in_delivery_note = 1
	for row in cn.items:
		row.qty = -abs(qty)
	cn.flags.fb_auto_invoicing = True
	cn.insert(ignore_permissions=True)
	if submit:
		cn.submit()
	return cn


# ---- running and comparing -------------------------------------------------------------------------------
@contextlib.contextmanager
def switches(walk=0, bulk_over=0, alert_rows=None):
	"""The re-check's site_config switches, for the duration of the block (the file is not touched)."""
	from unittest import mock

	from fuelbuddy_crm.billing_recheck import config

	conf = {config.WALK: walk, config.BULK_OVER: bulk_over, config.ALERT_ROWS: alert_rows}
	with mock.patch.object(config, "site_value", side_effect=conf.get):
		yield


def forget_caches():
	"""A savepoint rollback runs no after_rollback callbacks: drop what they would have dropped."""
	for doctype in ("Sales Order", "Sales Invoice", "Delivery Note"):
		frappe.clear_document_cache(doctype)
	frappe.db.value_cache.clear()


def line_dns(so_details, extra=()):
	"""Every Delivery Note on the lines (any docstatus, returns included), plus ``extra``."""
	names = set(extra)
	if so_details:
		names.update(
			frappe.db.sql_list(
				"select distinct parent from `tabDelivery Note Item` where so_detail in %(l)s",
				{"l": tuple(so_details)},
			)
		)
	return sorted(n for n in names if n)


def snapshot(so_details, extra=()):
	"""Every stored value the billing re-check can influence, for exact comparison."""
	dns = line_dns(so_details, extra) or ["-"]
	lines = tuple(so_details) or ("-",)
	return {
		"items": frappe.db.sql(
			"""select name, parent, billed_amt, returned_qty from `tabDelivery Note Item`
			where parent in %(n)s order by name""",
			{"n": dns},
		),
		"dns": frappe.db.sql(
			"""select name, docstatus, per_billed, per_returned, status from `tabDelivery Note`
			where name in %(n)s order by name""",
			{"n": dns},
		),
		"labels": frappe.db.sql(
			"""select reference_name, content, count(*) from `tabComment`
			where comment_type = 'Label' and reference_doctype = 'Delivery Note' and reference_name in %(n)s
			group by reference_name, content order by reference_name, content""",
			{"n": dns},
		),
		"so_items": frappe.db.sql(
			"""select name, billed_amt, delivered_qty, returned_qty from `tabSales Order Item`
			where name in %(l)s order by name""",
			{"l": lines},
		),
		"sos": frappe.db.sql(
			"""select so.name, so.per_billed, so.per_delivered, so.status from `tabSales Order` so
			where so.name in (select parent from `tabSales Order Item` where name in %(l)s) order by so.name""",
			{"l": lines},
		),
	}
