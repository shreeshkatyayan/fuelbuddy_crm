# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""The billing re-check on a live ERPNext site (IDEV-3268): twin equivalence with stock.

Written for the lab phase; these need an ERPNext site with fuelbuddy_crm installed and a company:

    bench --site <site> run-tests --app fuelbuddy_crm --module fuelbuddy_crm.tests.test_billing_recheck_site

Twin runs: each event runs twice on the same data inside one savepoint, first with every switch off
(stock ERPNext: the patches are installed but call the code they replaced), then with the re-check on;
every stored value the billing code can touch must be identical (billed_amt, returned_qty, per_billed,
per_returned, status, docstatus, Label comments, the Sales Order line and header). Then a stock walk
plus a stock refresh of every sibling over the re-check's result, rolled back, must change nothing.
Lines are small (tens of Delivery Notes); the 3k / 30k runs belong to the lab harness.
"""

from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from fuelbuddy_crm.billing_recheck import bulk, config, guard, install, observe
from fuelbuddy_crm.billing_recheck import stock_refresh as sr
from fuelbuddy_crm.tests import billing_recheck_fixtures as fx

DN = "Delivery Note"


class BillingRecheckCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_masters()
		install.install()

	def twin(self, event, lines, extra=(), walk_on=1, bulk_over=0):
		"""Run ``event`` with stock, roll back, run it with the re-check; assert equal; keep the second
		run's state. Returns the re-check run's counters delta."""
		frappe.db.savepoint("br_twin")
		with fx.switches():
			event()
		stock = fx.snapshot(lines, extra)
		frappe.db.rollback(save_point="br_twin")
		fx.forget_caches()
		before = observe.counters()
		with fx.switches(walk=walk_on, bulk_over=bulk_over):
			event()
		ours = fx.snapshot(lines, extra)
		self.assertEqual(stock, ours)
		self.assertFixpoint(lines, extra)
		after = observe.counters()
		return {k: after.get(k, 0) - before.get(k, 0) for k in after if after.get(k, 0) != before.get(k, 0)}

	def assertFixpoint(self, lines, extra=()):
		"""A stock walk and a stock refresh of every sibling over the current state change nothing."""
		current = fx.snapshot(lines, extra)
		frappe.db.savepoint("br_fixpoint")
		with fx.switches():
			for so_detail in lines:
				install.stock_walk()(so_detail)
			for name in fx.line_dns(lines, extra):
				if frappe.db.get_value(DN, name, "docstatus") == 1:
					frappe.get_doc(DN, name).update_billing_percentage()
		after = fx.snapshot(lines, extra)
		frappe.db.rollback(save_point="br_fixpoint")
		fx.forget_caches()
		for key in ("items", "dns", "so_items", "sos"):
			self.assertEqual(current[key], after[key], key)

	def new_line(self, qty=100_000, deliveries=20, tag=None):
		customer = fx.new_customer(tag or self._testMethodName[5:25])
		so = fx.make_sales_order(customer, qty)
		dns = [
			fx.make_delivery_note(
				customer,
				[(so, 100 + 7 * i)],
				f"2026-08-{2 + i % 20:02d}",
				f"{8 + i % 10:02d}:{i % 60:02d}:00",
			)
			for i in range(deliveries)
		]
		return customer, so, dns


class TestInstallAndGuard(BillingRecheckCase):
	def test_installed_and_guard_holds(self):
		self.assertTrue(install.installed())
		self.assertEqual(guard.mismatches(), [])
		self.assertEqual(bulk.safe_gap_reasons(), [])

	def test_install_is_idempotent(self):
		dn_mod, si_mod, _sc, _su = guard.modules()
		before = (dn_mod.DeliveryNote.update_billing_status, si_mod.SalesInvoice.update_billing_status_in_dn)
		install.install()
		after = (dn_mod.DeliveryNote.update_billing_status, si_mod.SalesInvoice.update_billing_status_in_dn)
		self.assertEqual(before, after)

	def test_config_reads_site_config_json(self):
		self.assertEqual(config.site_value("db_name"), frappe.conf.db_name)

	def test_not_installed_is_counted(self):
		before = observe.counters().get("not_installed", 0)
		doc = frappe._dict(doctype=DN, name="X")
		with fx.switches(walk=1), mock.patch.object(install, "installed", return_value=False):
			install.count_not_installed(doc, "on_submit")
		self.assertEqual(observe.counters().get("not_installed", 0), before + 1)


class TestTwinEvents(BillingRecheckCase):
	def test_dn_events_on_an_invoiced_line(self):
		customer, so, dns = self.new_line()
		lines = [so.items[0].name]
		delivered = sum(d.items[0].qty for d in dns)
		# SI#1 bills about 40% directly against the Sales Order line (with a discount)
		delta = self.twin(
			lambda: fx.make_so_invoice(so.name, round(delivered * 0.4, 3), discount_percentage=2.5), lines
		)
		self.assertEqual(delta.get("fifo"), 1)
		si1 = frappe.get_all("Sales Invoice", {"customer": customer, "docstatus": 1}, pluck="name")[0]

		frontier = next(
			d
			for d in sorted(dns, key=lambda d: (str(d.posting_date), str(d.posting_time), d.name))
			if 0 < frappe.db.get_value(DN, d.name, "per_billed") < 100
		)
		early = min(dns, key=lambda d: (str(d.posting_date), str(d.posting_time), d.name))

		self.twin(lambda: frappe.get_doc(DN, early.name).cancel(), lines)  # in window
		self.twin(
			lambda: fx.make_delivery_note(customer, [(so, 55.5)], "2026-08-01", "07:00:00"), lines
		)  # back-dated
		self.twin(lambda: frappe.get_doc(DN, frontier.name).cancel(), lines)  # at the frontier
		partly = next(
			d.name
			for d in dns
			if d.name not in (early.name, frontier.name) and frappe.db.get_value(DN, d.name, "per_billed") > 0
		)
		self.twin(lambda: fx.make_return(partly, 20), lines, extra=[partly])  # return of a billed DN
		ret = frappe.get_all(DN, {"return_against": partly, "docstatus": 1}, pluck="name")[0]
		self.twin(lambda: frappe.get_doc(DN, ret).cancel(), lines, extra=[partly])
		self.twin(
			lambda: fx.make_delivery_note(customer, [(so, 80)], "2026-08-30", "23:00:00"), lines
		)  # tail
		self.twin(lambda: fx.make_credit_note(si1, 50), lines)
		cn = frappe.get_all("Sales Invoice", {"return_against": si1, "docstatus": 1}, pluck="name")[0]
		self.twin(lambda: frappe.get_doc("Sales Invoice", cn).cancel(), lines)
		self.twin(lambda: fx.make_so_invoice(so.name, 300.0), lines)  # SI#2
		si2 = frappe.get_all(
			"Sales Invoice", {"customer": customer, "docstatus": 1, "name": ["!=", si1]}, pluck="name"
		)[0]
		self.twin(lambda: frappe.get_doc("Sales Invoice", si2).cancel(), lines)
		self.twin(lambda: frappe.get_doc("Sales Invoice", si1).cancel(), lines)

	def test_invoice_events_with_the_bulk_refresh(self):
		customer, so, dns = self.new_line(deliveries=30)
		lines = [so.items[0].name]
		delivered = sum(d.items[0].qty for d in dns)
		delta = self.twin(lambda: fx.make_so_invoice(so.name, round(delivered * 0.7, 2)), lines, bulk_over=5)
		self.assertEqual(delta.get("bulk"), 1)
		si1 = frappe.get_all("Sales Invoice", {"customer": customer, "docstatus": 1}, pluck="name")[0]
		self.twin(lambda: fx.make_credit_note(si1, 40), lines, bulk_over=5)  # small set: stock loop
		cn = frappe.get_all("Sales Invoice", {"return_against": si1, "docstatus": 1}, pluck="name")[0]
		self.twin(lambda: frappe.get_doc("Sales Invoice", cn).cancel(), lines, bulk_over=5)
		self.twin(lambda: frappe.get_doc("Sales Invoice", si1).cancel(), lines, bulk_over=5)
		# bulk with the walk off: stock's full list, refreshed set-based
		self.twin(
			lambda: fx.make_so_invoice(so.name, round(delivered * 0.5, 2)), lines, walk_on=0, bulk_over=5
		)

	def test_fast_path_on_an_uninvoiced_line(self):
		customer, so, _dns = self.new_line(deliveries=10)
		delta = self.twin(
			lambda: fx.make_delivery_note(customer, [(so, 42)], "2026-08-25"), [so.items[0].name]
		)
		self.assertEqual(delta.get("fast"), 1)

	def test_two_row_delivery_note_falls_back_to_stock(self):
		customer, so, dns = self.new_line(deliveries=10)
		lines = [so.items[0].name]
		fx.make_so_invoice(so.name, 500.0)
		delta = self.twin(lambda: fx.make_delivery_note(customer, [(so, 30), (so, 20)], "2026-08-03"), lines)
		self.assertGreaterEqual(delta.get("fallback_multi_item", 0), 1)
		two_row = frappe.get_all(
			DN, {"customer": customer, "docstatus": 1, "posting_date": "2026-08-03"}, pluck="name"
		)
		two_row = next(n for n in two_row if len(frappe.get_doc(DN, n).items) == 2)
		delta = self.twin(lambda: frappe.get_doc(DN, two_row).cancel(), lines)
		# the line is back on the re-check once that DN is gone (one walk per row of the cancelled DN)
		self.assertEqual(delta.get("fifo"), 2)
		self.assertNotIn("fallback_multi_item", delta)

	def test_si_detail_row_falls_back_to_stock(self):
		customer, so, dns = self.new_line(deliveries=5)
		lines = [so.items[0].name]
		si = fx.make_so_invoice(so.name, 200.0)
		# a row delivered against an invoice (set directly: the fallback only looks at the column)
		frappe.db.set_value("Delivery Note Item", dns[1].items[0].name, "si_detail", si.items[0].name)
		delta = self.twin(lambda: fx.make_delivery_note(customer, [(so, 10)], "2026-08-29"), lines)
		self.assertEqual(delta.get("fallback_si_detail"), 1)

	def test_return_of_a_delivery_note_without_sales_order(self):
		customer = fx.new_customer("nonso")
		dn = fx.make_delivery_note(customer, [(None, 200)], "2026-08-05")
		si = fx.make_dn_invoice(dn.name, submit=False)
		si.items[0].qty = 100
		si.save()
		si.submit()
		self.twin(lambda: fx.make_return(dn.name, 20), [], extra=[dn.name])

	def test_guard_off_runs_stock(self):
		customer, so, dns = self.new_line(deliveries=8)
		lines = [so.items[0].name]
		with mock.patch.object(guard, "mismatches", return_value=["test"]):
			delta = self.twin(lambda: fx.make_so_invoice(so.name, 300.0), lines)
		self.assertEqual(delta.get("guard_disabled"), 1)


class TestDriftAndRepair(BillingRecheckCase):
	def test_pre_stale_sibling_is_left_alone_and_repaired_by_the_api(self):
		"""The documented difference: stock repairs a sibling that was already wrong, the re-check does
		not; header_drift finds it and refresh_dns repairs it with stock code."""
		from fuelbuddy_crm.billing_recheck import api

		customer, so, dns = self.new_line(deliveries=10)
		lines = [so.items[0].name]
		fx.make_so_invoice(so.name, sum(d.items[0].qty for d in dns))  # everything billed
		stale = dns[4].name
		frappe.db.set_value(DN, stale, {"per_billed": 0, "status": "To Bill"}, update_modified=False)
		with fx.switches(walk=1):
			fx.make_delivery_note(customer, [(so, 10)], "2026-08-30")
		self.assertEqual(frappe.db.get_value(DN, stale, "status"), "To Bill")
		drift = api.header_drift(fx.line_dns(lines))
		self.assertEqual([d["name"] for d in drift], [stale])
		self.assertEqual(drift[0]["status"], ("To Bill", "Completed"))
		self.assertEqual(api.stock_would_change(lines[0])["rows"], [])
		api.refresh_dns([stale])
		self.assertEqual(api.header_drift(fx.line_dns(lines)), [])
		self.assertFixpoint(lines)

	def test_recompute_line_writes_what_stock_would(self):
		from fuelbuddy_crm.billing_recheck import api

		customer, so, dns = self.new_line(deliveries=12)
		lines = [so.items[0].name]
		fx.make_so_invoice(so.name, 600.0)
		frappe.db.set_value(
			"Delivery Note Item", dns[0].items[0].name, "billed_amt", 0, update_modified=False
		)
		predicted = api.stock_would_change(lines[0])
		self.assertEqual([r["name"] for r in predicted["rows"]], [dns[0].items[0].name])
		changed = api.recompute_line(lines[0])
		self.assertEqual(changed, [dns[0].name])
		api.refresh_dns(sorted(set(changed) | {d["name"] for d in api.header_drift(fx.line_dns(lines))}))
		self.assertFixpoint(lines)


class TestBulkRefreshEqualsStock(BillingRecheckCase):
	def test_refresh_of_perturbed_rows(self):
		"""Stock's per-DN loop and bulk_refresh over the same DNs, rows perturbed first, update_modified
		on and off: per_billed, status, modified_by, Label comments (every column but name / creation /
		modified) and the realtime messages queued."""
		customer, so, dns = self.new_line(deliveries=40)
		fx.make_so_invoice(so.name, sum(d.items[0].qty for d in dns) * 0.5)
		names = {d.name for d in dns}
		statuses = ["To Bill", "Completed", "Partially Billed", "Return Issued"]
		for i, name in enumerate(sorted(names)):
			frappe.db.set_value(
				DN, name, {"per_billed": (i * 13) % 101, "status": statuses[i % 4]}, update_modified=False
			)
		for update_modified in (True, False):
			with self.subTest(update_modified=update_modified):
				results = []
				for use_bulk in (False, True):
					frappe.db.savepoint("br_bulk")
					frappe.local._realtime_log = []
					with fx.switches(bulk_over=5):
						if use_bulk:
							self.assertIsNone(bulk.why_stock(names))
							bulk.bulk_refresh(names, update_modified)
						else:
							for name in names:
								frappe.get_doc(DN, name).update_billing_percentage(
									update_modified=update_modified
								)
					results.append(self._state(names))
					frappe.db.rollback(save_point="br_bulk")
					fx.forget_caches()
				self.assertEqual(results[0], results[1])

	def _state(self, names):
		cols = [c for c in bulk.COMMENT_FIELDS if c not in ("name", "creation", "modified")]
		return {
			"dns": frappe.db.sql(
				"select name, per_billed, status, modified_by from `tabDelivery Note` where name in %(n)s order by name",
				{"n": tuple(names)},
			),
			"comments": sorted(
				frappe.db.sql(
					f"select {', '.join(cols)} from `tabComment` where reference_doctype = 'Delivery Note' "
					"and comment_type = 'Label' and reference_name in %(n)s",
					{"n": tuple(names)},
				)
			),
			"realtime": sorted(
				(
					event,
					message.get("doctype"),
					message.get("name") if message.get("doctype") == DN else "comment",
				)
				for event, message, _room in getattr(frappe.local, "_realtime_log", [])
			),
		}

	def test_status_evaluation_matches_set_status(self):
		customer, so, dns = self.new_line(deliveries=6)
		names = [d.name for d in dns]
		for name, (stored, computed) in sr.statuses(names).items():
			doc = frappe.get_doc(DN, name)
			with mock.patch.object(type(doc), "add_comment"):
				doc.set_status()
			self.assertEqual(doc.status, computed, name)
			self.assertEqual(stored, frappe.db.get_value(DN, name, "status"))
		self.assertTrue(set(sr.status_fields()) >= {"docstatus", "per_billed", "status"})
