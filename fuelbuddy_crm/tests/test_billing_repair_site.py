# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""billing_repair on a real site (IDEV-3268), for the lab phase (a production copy).

    bench --site <site> run-tests --module fuelbuddy_crm.tests.test_billing_repair_site

- The per_billed / status billing_repair predicts equal what ERPNext's own
  update_billing_percentage(update_modified=True) writes, on real Delivery Notes of every kind
  (plain, partly returned, returns, Closed): each sampled DN is refreshed with stock's code inside
  the test transaction, compared, and everything is rolled back.
- A dry-run repair, audit() and audit_links() issue no write statement on MariaDB.

Nothing is committed: every test rolls back.
"""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days

from fuelbuddy_crm import billing_repair, dn_invoice_link

WRITES = ("update", "insert", "delete")


class TestBillingRepairSite(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def sample(self):
		picks = []
		for cond in (
			"1 = 1",
			"per_returned > 0",
			"is_return = 1",
			"status = 'Closed'",
			"per_billed > 0 and per_billed < 100",
		):
			picks += frappe.db.sql_list(
				f"select name from `tabDelivery Note` where docstatus = 1 and {cond} order by rand() limit 40"
			)
		if not picks:
			self.skipTest("no submitted Delivery Notes on this site")
		return sorted(set(picks))

	def watch_writes(self, fn, *args, **kwargs):
		seen = []
		real = frappe.db.sql

		def spy(query, *a, **k):
			if query.lstrip()[:6].lower() in WRITES:
				seen.append(query)
			return real(query, *a, **k)

		frappe.db.sql = spy
		try:
			result = fn(*args, **kwargs)
		finally:
			frappe.db.sql = real
		return result, seen

	def test_prediction_equals_stock_refresh(self):
		names = self.sample()
		predicted = {d.name: d for d in billing_repair.stock_billing(names)}
		self.assertEqual(sorted(predicted), names)
		for name in names:
			frappe.get_doc("Delivery Note", name).update_billing_percentage(update_modified=True)
		after = dict(
			(r.name, r)
			for r in frappe.db.sql(
				"select name, per_billed, status from `tabDelivery Note` where name in %(names)s",
				{"names": names},
				as_dict=True,
			)
		)
		mismatches = [
			(
				name,
				predicted[name].per_billed[1],
				after[name].per_billed,
				predicted[name].status[1],
				after[name].status,
			)
			for name in names
			if predicted[name].per_billed[1] != after[name].per_billed
			or (predicted[name].status[1] is not None and predicted[name].status[1] != after[name].status)
		]
		self.assertEqual(mismatches, [])

	def test_dry_run_and_audit_write_nothing(self):
		latest = frappe.db.sql("select max(posting_date) from `tabDelivery Note` where docstatus = 1")[0][0]
		if not latest:
			self.skipTest("no submitted Delivery Notes on this site")
		week = {"from_date": add_days(latest, -7), "page_size": 500, "lines": 0}
		report, writes = self.watch_writes(billing_repair.repair, **week)
		self.assertTrue(report.dry_run)
		self.assertEqual(writes, [])
		report, writes = self.watch_writes(billing_repair.audit, **week)
		self.assertEqual(writes, [])
		self.assertEqual(set(report.links), {"linked_not_submitted", "linked_to_dead_invoice", "over_linked"})

	def test_audit_links_runs_on_mariadb(self):
		if not frappe.db.has_column("Delivery Note", dn_invoice_link.LINK_FIELD):
			self.skipTest("dn_invoice_link fields not installed")
		found, writes = self.watch_writes(dn_invoice_link.audit_links, 5)
		self.assertEqual(writes, [])
		for check in found.values():
			self.assertLessEqual(len(check["sample"]), 5)
