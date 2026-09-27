# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""amend_delivery_note / get_amendment_plan (IDEV-3266) on a live ERPNext site.

Needs erpnext + fuelbuddy_crm and a company (setup wizard done).
Production-only schema is stood in by qc_fixtures.ensure_prod_schema. Every test gets its own
customer, so Sales Orders never leak between tests.

    bench --site <site> run-tests --app fuelbuddy_crm --module fuelbuddy_crm.tests.test_qty_correction
"""

import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt, getdate

from fuelbuddy_crm.api import qty_correction
from fuelbuddy_crm.dn_validation import so_headroom_shortfalls
from fuelbuddy_crm.tests import qc_fixtures as fx

FUTURE = "2099-01-01T05:00:00.000Z"
# Totals are compared within a cent: tax rounding follows the site's currency precision.
PAST = "2026-01-01T09:00:00+04:00"
VAT = 1.05


class QtyCorrectionTestCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_prod_schema()
		fx.ensure_masters()
		# Production behaviour: a backdated submit/cancel only QUEUES its repost.
		frappe.flags.dont_execute_stock_reposts = True

	def setUp(self):
		self.party = fx.new_customer(self._testMethodName[5:30])
		self.key = f"{uuid.uuid4()}-{int(time.time() * 1000)}"

	# ---- helpers ------------------------------------------------------------------------------
	def amend(self, dn, target, key=None, not_after=FUTURE):
		return qty_correction.amend_delivery_note(
			delivery_note=dn, target_qty=target, idempotency_key=key or self.key, not_after=not_after
		)

	def plan(self, iid, target, key=None):
		return qty_correction.get_amendment_plan(
			invoiced_item_id=iid, idempotency_key=key or self.key, target_qty=target
		)

	def assertRefused(self, result, code, retryable=False):
		self.assertFalse(result["ok"], result)
		self.assertEqual(result["code"], code, result)
		self.assertEqual(result["retryable"], retryable, result)
		self.assertIsNone(result["result"])

	def assertOk(self, result, expected):
		self.assertTrue(result["ok"], result)
		self.assertIsNone(result["code"])
		self.assertFalse(result["retryable"])
		self.assertEqual(result["result"], expected, result)

	def assertUntouched(self, dn):
		self.assertEqual(frappe.db.get_value("Delivery Note", dn.name, "docstatus"), dn.docstatus)
		self.assertEqual(self.amendments_of(dn.name), [])
		self.assertIsNone(self.log(), "a refusal must not write the amend log")

	def log(self, key=None):
		name = frappe.db.exists("DN Amend Log", {"idempotency_key": key or self.key})
		return frappe.get_doc("DN Amend Log", name) if name else None

	def so(self, qty, uom="Litre", **kw):
		return fx.make_sales_order(self.party, qty, uom=uom, **kw)

	def dn(self, lines, submit=False, **kw):
		return fx.make_delivery_note(self.party, lines, submit=submit, **kw)

	def soi(self, so, field):
		return flt(frappe.db.get_value("Sales Order Item", so.items[0].name, field))

	def amendments_of(self, name):
		return frappe.get_all("Delivery Note", filters={"amended_from": name}, pluck="name")

	def closed_may(self, closed):
		"""An Accounting Period over May 2026 with Delivery Note closed / open."""
		comp = fx.company()
		name = f"QC May 2026 - {fx.abbr()}"
		if not frappe.db.exists("Accounting Period", name):
			frappe.get_doc(
				{
					"doctype": "Accounting Period",
					"period_name": "QC May 2026",
					"company": comp,
					"start_date": "2026-05-01",
					"end_date": "2026-05-31",
					# given explicitly: ERPNext's bootstrap of this table crashes outside the desk form
					"closed_documents": [{"document_type": "Delivery Note", "closed": 1}],
				}
			).insert(ignore_permissions=True)
		frappe.db.set_value(
			"Closed Document", {"parent": name, "document_type": "Delivery Note"}, "closed", int(closed)
		)

	def may_dn(self, submit):
		self.closed_may(False)
		so = self.so(5000, transaction_date="2026-05-01", delivery_date="2026-05-31")
		dn = self.dn([(so, 1000)], submit=submit, posting_date="2026-05-15")
		self.closed_may(True)
		return dn


class TestAmendDraft(QtyCorrectionTestCase):
	def test_draft_reduction_updates_in_place(self):
		so = self.so(10000)
		dn = self.dn([(so, 1000)])

		r = self.amend(dn.name, 800)

		self.assertOk(r, "DRAFT_UPDATED")
		self.assertEqual(r["new_delivery_note"], dn.name)
		self.assertEqual(r["custom_version"], 1)
		dn.reload()
		self.assertEqual(dn.docstatus, 0)
		self.assertEqual([row.qty for row in dn.items], [800])
		self.assertEqual(dn.items[0].rate, fx.RATE_L)
		self.assertEqual(dn.custom_qc_idempotency_key, self.key)
		self.assertAlmostEqual(r["grand_total"], dn.grand_total)
		self.assertAlmostEqual(dn.grand_total, 800 * fx.RATE_L * VAT, delta=0.01)
		self.assertEqual(self.soi(so, "custom_delivery_note_qty_in_draft"), 800)
		log = self.log()
		self.assertEqual(
			(log.result, log.delivery_note, log.new_delivery_note, log.target_qty),
			("DRAFT_UPDATED", dn.name, dn.name, 800),
		)

	def test_draft_increase_within_headroom_grows_the_line(self):
		so = self.so(10000)
		dn = self.dn([(so, 1000)])
		self.assertOk(self.amend(dn.name, 1500), "DRAFT_UPDATED")
		dn.reload()
		self.assertEqual([(row.so_detail, row.qty) for row in dn.items], [(so.items[0].name, 1500)])
		self.assertEqual(self.soi(so, "custom_delivery_note_qty_in_draft"), 1500)

	def test_draft_increase_spills_onto_the_next_sales_order(self):
		so_a = self.so(1000)
		so_b = self.so(5000)
		dn = self.dn([(so_a, 900)])

		self.assertOk(self.amend(dn.name, 1400), "DRAFT_UPDATED")

		dn.reload()
		self.assertEqual(
			[(row.against_sales_order, row.qty, row.rate) for row in dn.items],
			[(so_a.name, 1000, fx.RATE_L), (so_b.name, 400, fx.RATE_L)],
		)
		self.assertEqual(self.soi(so_b, "custom_delivery_note_qty_in_draft"), 400)

	def test_draft_deleted_at_zero(self):
		so = self.so(10000)
		dn = self.dn([(so, 1000)])

		r = self.amend(dn.name, 0)

		self.assertOk(r, "DRAFT_DELETED")
		self.assertEqual((r["new_delivery_note"], r["custom_version"], r["grand_total"]), (None, None, 0.0))
		self.assertFalse(frappe.db.exists("Delivery Note", dn.name))
		self.assertEqual(self.soi(so, "custom_delivery_note_qty_in_draft"), 0)
		self.assertEqual(self.log().result, "DRAFT_DELETED")

	def test_draft_replaces_a_previous_episodes_key(self):
		so = self.so(10000)
		dn = self.dn([(so, 1000)])
		first = f"{uuid.uuid4()}-1"
		self.assertOk(self.amend(dn.name, 900, key=first), "DRAFT_UPDATED")
		self.assertOk(self.amend(dn.name, 950), "DRAFT_UPDATED")
		self.assertEqual(frappe.db.get_value("Delivery Note", dn.name, "custom_qc_idempotency_key"), self.key)
		self.assertIsNotNone(self.log(first))


class TestAmendSubmitted(QtyCorrectionTestCase):
	def test_submitted_is_cancelled_and_amended_in_one_call(self):
		so = self.so(10000)
		dn = self.dn([(so, 1000)], submit=True, posting_date="2026-08-15", posting_time="10:30:00")

		r = self.amend(dn.name, 750)

		self.assertOk(r, "AMENDED")
		self.assertEqual(frappe.db.get_value("Delivery Note", dn.name, "docstatus"), 2)
		new = frappe.get_doc("Delivery Note", r["new_delivery_note"])
		self.assertEqual(new.name, f"{dn.name}-1")
		self.assertEqual(new.docstatus, 1)
		self.assertEqual(new.amended_from, dn.name)
		self.assertEqual((new.custom_version, r["custom_version"]), ("2", 2))
		self.assertEqual(getdate(new.posting_date), getdate(dn.posting_date))
		self.assertEqual(str(new.posting_time), str(dn.posting_time))
		self.assertEqual(new.set_posting_time, 1)
		self.assertEqual(
			[(row.so_detail, row.qty, row.rate, row.uom) for row in new.items],
			[(so.items[0].name, 750, fx.RATE_L, "Litre")],
		)
		self.assertEqual(
			(new.selling_price_list, new.taxes_and_charges), (dn.selling_price_list, dn.taxes_and_charges)
		)
		self.assertEqual(
			[(t.account_head, t.rate) for t in new.taxes], [(t.account_head, t.rate) for t in dn.taxes]
		)
		self.assertEqual(new.custom_invoiced_item_id, dn.custom_invoiced_item_id)
		self.assertEqual(new.custom_qc_idempotency_key, self.key)
		self.assertAlmostEqual(r["grand_total"], new.grand_total)
		self.assertAlmostEqual(new.grand_total, 750 * fx.RATE_L * VAT, delta=0.01)
		self.assertEqual(self.soi(so, "delivered_qty"), 750)
		log = self.log()
		self.assertEqual((log.result, log.new_delivery_note, log.custom_version), ("AMENDED", new.name, 2))

	def test_amending_an_amendment_increments_the_version_again(self):
		so = self.so(10000)
		dn = self.dn([(so, 1000)], submit=True)
		first = self.amend(dn.name, 900, key=f"{uuid.uuid4()}-1")
		second = self.amend(first["new_delivery_note"], 800)
		self.assertOk(second, "AMENDED")
		self.assertEqual((second["new_delivery_note"], second["custom_version"]), (f"{dn.name}-2", 3))

	def test_submitted_cancelled_at_zero(self):
		so = self.so(10000)
		dn = self.dn([(so, 1000)], submit=True)

		r = self.amend(dn.name, 0)

		self.assertOk(r, "CANCELLED")
		self.assertEqual((r["new_delivery_note"], r["custom_version"], r["grand_total"]), (None, None, 0.0))
		self.assertEqual(frappe.db.get_value("Delivery Note", dn.name, "docstatus"), 2)
		self.assertEqual(self.amendments_of(dn.name), [])
		self.assertEqual(self.soi(so, "delivered_qty"), 0)
		self.assertFalse(frappe.db.exists("Delivery Note", {"custom_qc_idempotency_key": self.key}))
		self.assertEqual(self.log().result, "CANCELLED")

	def test_increase_spills_onto_the_next_sales_order(self):
		so_a = self.so(1000)
		so_b = self.so(5000)
		dn = self.dn([(so_a, 900)], submit=True)

		r = self.amend(dn.name, 1500)

		self.assertOk(r, "AMENDED")
		new = frappe.get_doc("Delivery Note", r["new_delivery_note"])
		self.assertEqual(
			[(row.against_sales_order, row.so_detail, row.qty) for row in new.items],
			[(so_a.name, so_a.items[0].name, 1000), (so_b.name, so_b.items[0].name, 500)],
		)
		self.assertEqual((self.soi(so_a, "delivered_qty"), self.soi(so_b, "delivered_qty")), (1000, 500))

	def test_multi_line_reduction_trims_from_the_last_line(self):
		so_a = self.so(600)
		so_b = self.so(5000)
		dn = self.dn([(so_a, 600), (so_b, 400)], submit=True)

		r = self.amend(dn.name, 850)
		new = frappe.get_doc("Delivery Note", r["new_delivery_note"])
		self.assertEqual(
			[(row.against_sales_order, row.qty) for row in new.items], [(so_a.name, 600), (so_b.name, 250)]
		)

		r = self.amend(new.name, 450, key=f"{uuid.uuid4()}-2")
		newer = frappe.get_doc("Delivery Note", r["new_delivery_note"])
		self.assertEqual([(row.against_sales_order, row.qty) for row in newer.items], [(so_a.name, 450)])
		self.assertEqual((self.soi(so_a, "delivered_qty"), self.soi(so_b, "delivered_qty")), (450, 0))

	def test_imperial_gallon_line_is_written_in_gallons(self):
		so = self.so(1000, uom=fx.IG)
		dn = self.dn([(so, 220)], submit=True)

		r = self.amend(dn.name, 909.2)

		self.assertOk(r, "AMENDED")
		row = frappe.get_doc("Delivery Note", r["new_delivery_note"]).items[0]
		self.assertEqual((row.uom, row.conversion_factor, row.rate), (fx.IG, fx.IG_FACTOR, fx.RATE_IG))
		self.assertAlmostEqual(row.qty, 200, places=6)
		self.assertAlmostEqual(row.stock_qty, 909.2, places=6)
		self.assertAlmostEqual(r["grand_total"], 200 * fx.RATE_IG * VAT, delta=0.01)
		self.assertAlmostEqual(self.soi(so, "delivered_qty"), 200, places=6)

	def test_ui_amendment_of_a_corrected_dn_does_not_inherit_the_key(self):
		so = self.so(10000)
		dn = self.dn([(so, 1000)], submit=True)
		corrected = frappe.get_doc("Delivery Note", self.amend(dn.name, 900)["new_delivery_note"])
		corrected.cancel()
		ui = frappe.copy_doc(corrected)  # what the desk Amend does: no_copy fields included
		ui.amended_from = corrected.name
		ui.docstatus = 0
		for child in ui.get_all_children():
			child.docstatus = 0
		ui.insert()
		self.assertIsNone(ui.custom_qc_idempotency_key)
		self.assertEqual(
			frappe.db.get_value("Delivery Note", corrected.name, "custom_qc_idempotency_key"), self.key
		)


class TestAmendIdempotency(QtyCorrectionTestCase):
	def assertSameResult(self, first, retry):
		keys = ("ok", "code", "retryable", "result", "new_delivery_note", "custom_version", "grand_total")
		self.assertEqual({k: retry[k] for k in keys}, {k: first[k] for k in keys})

	def test_retry_after_amended_returns_the_logged_result(self):
		so = self.so(10000)
		dn = self.dn([(so, 1000)], submit=True)
		first = self.amend(dn.name, 750)
		retry = self.amend(dn.name, 750)  # the input DN is cancelled by now
		self.assertSameResult(first, retry)
		self.assertEqual(self.amendments_of(dn.name), [first["new_delivery_note"]])
		self.assertEqual(frappe.db.count("DN Amend Log", {"idempotency_key": self.key}), 1)

	def test_retry_after_cancelled_returns_the_logged_result(self):
		so = self.so(10000)
		dn = self.dn([(so, 1000)], submit=True)
		first = self.amend(dn.name, 0)
		self.assertSameResult(first, self.amend(dn.name, 0))

	def test_retry_after_draft_deleted_returns_the_logged_result(self):
		so = self.so(10000)
		dn = self.dn([(so, 1000)])
		first = self.amend(dn.name, 0)
		self.assertSameResult(first, self.amend(dn.name, 0))  # the DN no longer exists

	def test_retry_after_draft_updated_returns_the_logged_result(self):
		so = self.so(10000)
		dn = self.dn([(so, 1000)])
		first = self.amend(dn.name, 700)
		self.assertSameResult(first, self.amend(dn.name, 700))
		self.assertEqual(frappe.db.get_value("Delivery Note Item", {"parent": dn.name}, "qty"), 700)

	def test_retry_is_answered_from_the_log_after_the_cut_off(self):
		so = self.so(10000)
		dn = self.dn([(so, 1000)], submit=True)
		first = self.amend(dn.name, 750)
		self.assertSameResult(first, self.amend(dn.name, 750, not_after=PAST))


class TestAmendRefusals(QtyCorrectionTestCase):
	def test_not_live_dn_for_a_cancelled_or_missing_dn(self):
		so = self.so(10000)
		dn = self.dn([(so, 1000)], submit=True)
		dn.cancel()
		self.assertRefused(self.amend(dn.name, 500), "NOT_LIVE_DN")
		self.assertRefused(self.amend("FB/DN/NO-SUCH-DN", 500), "NOT_LIVE_DN")
		self.assertIsNone(self.log())

	def test_so_closed(self):
		for status in ("Closed", "On Hold"):
			with self.subTest(status):
				so = self.so(10000)
				dn = self.dn([(so, 1000)], submit=True)
				frappe.db.set_value("Sales Order", so.name, "status", status)
				r = self.amend(dn.name, 500)
				self.assertRefused(r, "SO_CLOSED")
				self.assertIn(status, r["message"])
				self.assertUntouched(dn)

	def test_invoiced_by_a_draft_or_submitted_sales_invoice(self):
		for submit_si in (False, True):
			with self.subTest(submitted_invoice=submit_si):
				so = self.so(10000)
				dn = self.dn([(so, 1000)], submit=True, posting_date="2026-08-15")
				si = fx.make_sales_invoice(so, "2026-08-01", "2026-08-31", submit=submit_si)
				r = self.amend(dn.name, 500)
				self.assertRefused(r, "INVOICED")
				self.assertIn(si.name, r["message"])
				self.assertUntouched(dn)

	def test_invoice_whose_window_misses_the_posting_date_does_not_block(self):
		so = self.so(10000, transaction_date="2026-07-01")
		dn = self.dn([(so, 1000)], submit=True, posting_date="2026-08-15")
		fx.make_sales_invoice(so, "2026-07-01", "2026-07-31")
		self.assertOk(self.amend(dn.name, 500), "AMENDED")

	def test_invoice_on_the_spill_over_line_blocks_the_reissue(self):
		so_a = self.so(1000)
		so_b = self.so(5000)
		dn = self.dn([(so_a, 900)], submit=True, posting_date="2026-08-15")
		fx.make_sales_invoice(so_b, "2026-08-01", "2026-08-31")
		self.assertRefused(self.amend(dn.name, 1500), "INVOICED")
		self.assertUntouched(dn)

	def test_period_closed(self):
		dn = self.may_dn(submit=True)
		self.assertRefused(self.amend(dn.name, 500), "PERIOD_CLOSED")
		self.assertUntouched(dn)

	def test_period_closed_refuses_a_draft_update_but_not_a_draft_delete(self):
		dn = self.may_dn(submit=False)
		self.assertRefused(self.amend(dn.name, 500), "PERIOD_CLOSED")
		self.assertOk(self.amend(dn.name, 0, key=f"{uuid.uuid4()}-9"), "DRAFT_DELETED")

	def test_deadline_passed_before_the_call(self):
		so = self.so(10000)
		dn = self.dn([(so, 1000)], submit=True)
		r = self.amend(dn.name, 500, not_after=PAST)
		self.assertRefused(r, "DEADLINE")
		self.assertUntouched(dn)

	def test_deadline_checked_inside_the_transaction_rolls_everything_back(self):
		"""The cut-off passes while the amend runs: it is refused at the final check, after the
		cancel and reissue were written, and all of it rolls back."""
		so = self.so(10000)
		dn = self.dn([(so, 1000)], submit=True)
		frappe.db.commit()
		before, after = datetime(2026, 10, 1, 4, 59, 59), datetime(2026, 10, 1, 5, 0, 0)
		clock = iter([before, before, after])
		with patch.object(qty_correction, "_db_now", lambda: next(clock)):
			r = self.amend(dn.name, 500, not_after="2026-10-01T09:00:00+04:00")
		self.assertRefused(r, "DEADLINE")
		self.assertUntouched(dn)
		self.assertEqual(self.soi(so, "delivered_qty"), 1000)

	def test_so_headroom_when_no_sales_order_can_take_the_increase(self):
		so = self.so(1000)
		dn = self.dn([(so, 1000)], submit=True)
		r = self.amend(dn.name, 1200)
		self.assertRefused(r, "SO_HEADROOM")
		self.assertUntouched(dn)

	def test_invalid_input_is_erp_validation(self):
		so = self.so(10000)
		dn = self.dn([(so, 1000)])
		self.assertRefused(self.amend(dn.name, -5), "ERP_VALIDATION")
		self.assertRefused(self.amend(dn.name, "lots"), "ERP_VALIDATION")
		self.assertRefused(self.amend(dn.name, 500, not_after="next tuesday"), "ERP_VALIDATION")
		self.assertRefused(self.amend(dn.name, 500, not_after=""), "ERP_VALIDATION")
		self.assertRefused(qty_correction.amend_delivery_note(dn.name, 500, "", FUTURE), "ERP_VALIDATION")
		self.assertUntouched(dn)


class TestSalesOrderHeadroom(QtyCorrectionTestCase):
	"""IDEV-3266 rulings (27 Sep): a reduction is never refused for Sales Order headroom, an increase
	keeps the check on what it adds; a return frees Sales Order qty once."""

	def over_booked(self, qty, submit):
		"""A 1000 L Sales Order line: the Delivery Note under test with ``qty`` on it, another live
		draft holding the other 1000 - ``qty``, then the order cut to 900 L: over-booked by 100 L.
		Drafts, not submitted notes, over-book it, so ERPNext's own over-delivery check (which counts
		only submitted notes) stays out of the way and the headroom check is the one tested."""
		so = self.so(1000)
		dn = self.dn([(so, qty)], submit=submit)
		self.dn([(so, 1000 - qty)])
		frappe.db.set_value("Sales Order Item", so.items[0].name, "qty", 900)
		return so, dn

	def test_submitted_reduction_on_an_over_booked_line_is_amended(self):
		so, dn = self.over_booked(600, submit=True)

		r = self.amend(dn.name, 550)

		self.assertOk(r, "AMENDED")
		new = frappe.get_doc("Delivery Note", r["new_delivery_note"])
		self.assertEqual([(row.so_detail, row.qty) for row in new.items], [(so.items[0].name, 550)])
		self.assertEqual(self.soi(so, "delivered_qty"), 550)
		self.assertEqual(self.log().result, "AMENDED")

	def test_draft_reduction_on_an_over_booked_line_is_updated(self):
		so, dn = self.over_booked(600, submit=False)

		self.assertOk(self.amend(dn.name, 550), "DRAFT_UPDATED")

		dn.reload()
		self.assertEqual([row.qty for row in dn.items], [550])
		self.assertEqual(self.soi(so, "custom_delivery_note_qty_in_draft"), 950)

	def test_submitted_increase_on_a_full_line_is_refused(self):
		so, dn = self.over_booked(600, submit=True)
		self.assertRefused(self.amend(dn.name, 650), "SO_HEADROOM")
		self.assertUntouched(dn)
		self.assertEqual(self.soi(so, "delivered_qty"), 600)

	def test_draft_increase_on_a_full_line_is_refused(self):
		so, dn = self.over_booked(600, submit=False)
		self.assertRefused(self.amend(dn.name, 650), "SO_HEADROOM")
		self.assertUntouched(dn)
		self.assertEqual(frappe.db.get_value("Delivery Note Item", {"parent": dn.name}, "qty"), 600)

	def test_increase_on_an_over_booked_line_spills_onto_the_next_sales_order(self):
		"""The Delivery Note's own line keeps its 600 L and is not refused, over-booked as it is; the
		100 L increase goes to the next order, which has room."""
		so_a, dn = self.over_booked(600, submit=True)
		so_b = self.so(5000)

		r = self.amend(dn.name, 700)

		self.assertOk(r, "AMENDED")
		new = frappe.get_doc("Delivery Note", r["new_delivery_note"])
		self.assertEqual(
			[(row.against_sales_order, row.qty) for row in new.items], [(so_a.name, 600), (so_b.name, 100)]
		)
		self.assertEqual((self.soi(so_a, "delivered_qty"), self.soi(so_b, "delivered_qty")), (600, 100))

	def test_a_grown_line_is_checked_on_the_growth(self):
		so = self.so(1000)
		dn = self.dn([(so, 600)], submit=True)
		self.dn([(so, 300)])  # 100 L left

		r = self.amend(dn.name, 700)  # grows by exactly what is left
		self.assertOk(r, "AMENDED")
		self.assertEqual(self.soi(so, "delivered_qty"), 700)

		key = f"{uuid.uuid4()}-2"
		self.assertRefused(self.amend(r["new_delivery_note"], 710, key=key), "SO_HEADROOM")  # none left
		self.assertEqual(self.soi(so, "delivered_qty"), 700)

	def test_shortfalls_spare_a_reissue_line_not_grown_past_the_note_it_replaces(self):
		so = self.so(1000)
		dn = self.dn([(so, 600)], submit=True)
		self.dn([(so, 400)])  # the rest is held by a draft
		dn.reload()
		dn.cancel()  # what the amend does first: 600 back, 400 held
		frappe.db.set_value("Sales Order Item", so.items[0].name, "qty", 900)  # 500 left
		reissue = frappe.copy_doc(dn)
		reissue.items[0].qty = 550

		(short,) = so_headroom_shortfalls(reissue)  # without the flag: a new 550 L punch
		self.assertEqual((short.increase, short.available), (550, 500))
		reissue.flags.qc_replaced_qty = {so.items[0].name: 600}
		self.assertEqual(so_headroom_shortfalls(reissue), [])
		reissue.items[0].qty = 650  # grown past the original: its full qty against what is left
		(short,) = so_headroom_shortfalls(reissue)
		self.assertEqual((short.increase, short.available), (650, 500))

	def test_a_return_frees_its_qty_once(self):
		"""ERPNext takes a submitted return off delivered_qty and also records it in returned_qty;
		what the line has left counts it once."""
		from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_return

		so = self.so(1000)
		dn = self.dn([(so, 1000)], submit=True)
		ret = make_sales_return(dn.name)
		ret.items[0].qty = -100
		ret.posting_date = "2026-08-16"
		ret.set_posting_time = 1
		ret.insert()
		ret.submit()
		self.assertEqual((self.soi(so, "delivered_qty"), self.soi(so, "returned_qty")), (900, 100))

		punch = self.dn([(so, 100)])  # exactly what the return gave back
		punch.items[0].qty = 150
		(short,) = so_headroom_shortfalls(punch)
		self.assertEqual((short.increase, short.available), (50, 0))


class TestAmendErrorMapping(QtyCorrectionTestCase):
	"""Exceptions raised inside the transaction map to codes and roll back."""

	def setUp(self):
		super().setUp()
		self.target = self.dn([(self.so(10000), 1000)], submit=True)

	def amend_raising(self, exc):
		with patch.object(qty_correction, "_amend", side_effect=exc):
			return self.amend(self.target.name, 500)

	def test_lock_and_timestamp_errors_are_lock_retry(self):
		for exc in (
			frappe.TimestampMismatchError("modified since"),
			frappe.QueryDeadlockError("deadlock"),
			frappe.QueryTimeoutError("lock wait timeout"),
		):
			with self.subTest(type(exc).__name__):
				self.assertRefused(self.amend_raising(exc), "LOCK_RETRY", retryable=True)

	def test_unique_clash_without_a_log_is_erp_validation_not_a_retry(self):
		# Amends are serialised (DN lock, log gap lock), so a unique clash that did not come from
		# this episode's own committed log is structural: retrying cannot fix it.
		for exc in (
			frappe.DuplicateEntryError("Delivery Note", "MAT-DN-2026-00001-1"),
			frappe.UniqueValidationError("custom_qc_idempotency_key already exists"),
		):
			with self.subTest(type(exc).__name__):
				r = self.amend_raising(exc)
				self.assertRefused(r, "ERP_VALIDATION")
				self.assertTrue(r["message"])

	def test_unique_clash_answered_by_this_episodes_committed_log(self):
		logged = frappe._dict(
			result="AMENDED", new_delivery_note="MAT-DN-X-1", custom_version=2, grand_total=123.4
		)
		# the first read (before the amend) finds nothing; after the clash and rollback it does
		with patch.object(qty_correction, "_read_log", side_effect=[None, logged]):
			r = self.amend_raising(frappe.DuplicateEntryError("DN Amend Log", self.key))
		self.assertOk(r, "AMENDED")
		self.assertEqual((r["new_delivery_note"], r["custom_version"]), ("MAT-DN-X-1", 2))

	def test_other_validation_errors_are_erp_validation_with_the_message(self):
		r = self.amend_raising(frappe.ValidationError("Item FB/FL/00001 is disabled"))
		self.assertRefused(r, "ERP_VALIDATION")
		self.assertEqual(r["message"], "Item FB/FL/00001 is disabled")

	def test_permission_error_is_erp_validation(self):
		self.assertRefused(self.amend_raising(frappe.PermissionError("no cancel")), "ERP_VALIDATION")

	def test_infrastructure_errors_propagate_and_roll_back(self):
		with self.assertRaises(RuntimeError):
			self.amend_raising(RuntimeError("connection reset"))
		self.assertEqual(frappe.db.get_value("Delivery Note", self.target.name, "docstatus"), 1)

	def test_success_is_committed(self):
		r = self.amend(self.target.name, 500)
		self.assertOk(r, "AMENDED")
		frappe.db.rollback()  # nothing of the amend is left to roll back
		self.assertEqual(frappe.db.get_value("Delivery Note", self.target.name, "docstatus"), 2)
		self.assertEqual(frappe.db.get_value("Delivery Note", r["new_delivery_note"], "docstatus"), 1)
		self.assertEqual(self.log().result, "AMENDED")


class TestAmendSalesOrderLineLock(QtyCorrectionTestCase):
	"""Amends of different Delivery Notes on one Sales Order line take turns on the line. A second
	database session stands in for the other amend; fixtures are committed so it sees them."""

	def hold(self, *so_details):
		"""A second session holding these Sales Order lines, as a concurrent amend would."""
		other = frappe.db.create_connection()
		self.addCleanup(other.close)  # an open transaction rolls back on close
		other.cursor().execute(
			"select name from `tabSales Order Item` where name in %s for update", [so_details]
		)
		return other

	@contextmanager
	def lock_wait(self, seconds):
		before = frappe.db.sql("select @@session.innodb_lock_wait_timeout")[0][0]
		frappe.db.sql("set session innodb_lock_wait_timeout = %s", seconds)
		try:
			yield
		finally:
			frappe.db.sql("set session innodb_lock_wait_timeout = %s", before)

	def commit_once_waiting(self, other):
		"""Commit ``other`` as soon as this session waits for the Sales Order line lock."""
		me = frappe.db.sql("select connection_id()")[0][0]
		watcher = frappe.db.create_connection()
		self.addCleanup(watcher.close)

		def run():
			deadline = time.monotonic() + 20
			while time.monotonic() < deadline:
				cursor = watcher.cursor()
				cursor.execute("select info from information_schema.processlist where id = %s", me)
				info = (cursor.fetchone() or [None])[0] or ""
				if "`tabSales Order Item`" in info and "for update" in info:
					break
				time.sleep(0.05)
			other.commit()

		thread = threading.Thread(target=run)
		thread.start()
		self.addCleanup(thread.join)
		return thread

	def amend_while_held(self, dn, target, held_so):
		"""Amend while another session holds ``held_so``'s line: LOCK_RETRY, and the amend waited on
		the line before it read any headroom (a save or submit waits on it too, but after the check)."""
		frappe.db.commit()
		self.hold(held_so.items[0].name)
		with (
			self.lock_wait(1),
			patch.object(qty_correction, "so_headroom_shortfalls", wraps=so_headroom_shortfalls) as check,
		):
			r = self.amend(dn.name, target)
		self.assertRefused(r, "LOCK_RETRY", retryable=True)
		check.assert_not_called()
		self.assertUntouched(dn)

	def draft_qty(self, dn):
		return frappe.db.get_value("Delivery Note Item", {"parent": dn.name}, "qty")

	def test_lines_to_lock_are_the_dn_s_own_plus_spill_over_lines_for_an_increase(self):
		so_a = self.so(1000)
		so_b = self.so(5000)
		dn = self.dn([(so_a, 900)], submit=True)
		own, spill = so_a.items[0].name, so_b.items[0].name
		self.assertEqual(qty_correction._so_lines_to_lock(dn.name, 800), {own})
		self.assertEqual(qty_correction._so_lines_to_lock(dn.name, 0), {own})
		self.assertEqual(qty_correction._so_lines_to_lock(dn.name, 1500), {own, spill})
		self.assertEqual(qty_correction._so_lines_to_lock("FB/DN/NO-SUCH-DN", 1500), set())

	def test_a_held_line_of_its_own_is_lock_retry(self):
		so = self.so(1000)
		dn = self.dn([(so, 400)])
		self.amend_while_held(dn, 450, so)
		self.assertEqual(self.draft_qty(dn), 400)

	def test_a_held_spill_over_line_is_lock_retry(self):
		so_a = self.so(1000)
		so_b = self.so(5000)
		dn = self.dn([(so_a, 900)], submit=True)
		self.amend_while_held(dn, 1500, so_b)  # 100 more on so_a's line, 500 onto so_b's
		self.assertEqual((self.soi(so_a, "delivered_qty"), self.soi(so_b, "delivered_qty")), (900, 0))

	def test_an_amend_that_waited_for_the_line_sees_what_the_holder_committed(self):
		"""The snapshot is taken after the lock. Another session holds the line and grows a draft on
		it while the amend waits; once it commits, the amend's headroom check counts that draft."""
		so = self.so(1000)
		held = self.dn([(so, 400)])
		dn = self.dn([(so, 400)])  # 200 left: room for one +150, not two
		frappe.db.commit()
		other = self.hold(so.items[0].name)
		cursor = other.cursor()
		cursor.execute("update `tabDelivery Note Item` set qty = 550 where parent = %s", held.name)
		cursor.execute(
			"update `tabSales Order Item` set custom_delivery_note_qty_in_draft = 950 where name = %s",
			so.items[0].name,
		)
		waiter = self.commit_once_waiting(other)

		with self.lock_wait(15):
			r = self.amend(dn.name, 550)
		waiter.join()

		self.assertRefused(r, "SO_HEADROOM")
		self.assertEqual((self.draft_qty(held), self.draft_qty(dn)), (550, 400))
		self.assertIsNone(self.log())

	def test_a_line_it_did_not_lock_is_lock_retry(self):
		"""The lines to lock are read before the transaction; if the amend then needs another one
		(here: a Sales Order it did not see), it refuses rather than read it unlocked."""
		so_a = self.so(1000)
		so_b = self.so(5000)
		dn = self.dn([(so_a, 900)], submit=True)
		with patch.object(qty_correction, "_so_lines_to_lock", return_value={so_a.items[0].name}):
			r = self.amend(dn.name, 1500)
		self.assertRefused(r, "LOCK_RETRY", retryable=True)
		self.assertIn(so_b.items[0].name, r["message"])
		self.assertUntouched(dn)

	def test_plan_takes_no_lock(self):
		so = self.so(1000)
		dn = self.dn([(so, 400)], submit=True)
		frappe.db.commit()
		self.hold(so.items[0].name)
		with self.lock_wait(1):
			self.assertTrue(self.plan(dn.custom_invoiced_item_id, 900)["ok"])


class TestGetAmendmentPlan(QtyCorrectionTestCase):
	def test_live_submitted_dn_not_at_target(self):
		so = self.so(10000)
		dn = self.dn([(so, 1000)], submit=True)
		p = self.plan(dn.custom_invoiced_item_id, 800)
		self.assertEqual(
			p,
			{
				"ok": True,
				"code": None,
				"message": None,
				"retryable": False,
				"amend_log": None,
				"live_delivery_note": dn.name,
				"live_dn_qty_litres": 1000,
				"dn_at_target": False,
				"docstatus": 1,
			},
		)

	def test_live_draft_dn(self):
		dn = self.dn([(self.so(10000), 1000)])
		p = self.plan(dn.custom_invoiced_item_id, 800)
		self.assertTrue(p["ok"])
		self.assertEqual((p["live_delivery_note"], p["docstatus"]), (dn.name, 0))

	def test_dn_at_target_skips_the_erp_checks(self):
		so = self.so(10000)
		dn = self.dn([(so, 1000)], submit=True)
		frappe.db.set_value("Sales Order", so.name, "status", "Closed")
		p = self.plan(dn.custom_invoiced_item_id, 1000.004)
		self.assertTrue(p["ok"], p)
		self.assertTrue(p["dn_at_target"])

	def test_imperial_gallon_dn_reports_litres(self):
		dn = self.dn([(self.so(1000, uom=fx.IG), 200)], submit=True)
		p = self.plan(dn.custom_invoiced_item_id, 909.2)
		self.assertAlmostEqual(p["live_dn_qty_litres"], 909.2, places=6)
		self.assertTrue(p["dn_at_target"])

	def test_amend_log_is_returned_after_the_amend(self):
		so = self.so(10000)
		dn = self.dn([(so, 1000)], submit=True)
		r = self.amend(dn.name, 750)
		p = self.plan(dn.custom_invoiced_item_id, 750)
		self.assertTrue(p["ok"])
		self.assertEqual(
			p["amend_log"],
			{"result": "AMENDED", "new_delivery_note": r["new_delivery_note"], "custom_version": 2},
		)
		self.assertEqual(
			(p["live_delivery_note"], p["dn_at_target"], p["docstatus"]), (r["new_delivery_note"], True, 1)
		)

	def test_amend_log_for_a_deleted_draft(self):
		dn = self.dn([(self.so(10000), 1000)])
		self.amend(dn.name, 0)
		p = self.plan(dn.custom_invoiced_item_id, 0)
		self.assertTrue(p["ok"])
		self.assertEqual(
			p["amend_log"], {"result": "DRAFT_DELETED", "new_delivery_note": None, "custom_version": None}
		)
		self.assertIsNone(p["live_delivery_note"])

	def test_no_live_dn(self):
		p = self.plan(str(uuid.uuid4()), 500)
		self.assertEqual(
			(
				p["ok"],
				p["code"],
				p["live_delivery_note"],
				p["live_dn_qty_litres"],
				p["dn_at_target"],
				p["docstatus"],
			),
			(True, None, None, None, False, None),
		)

	def test_duplicate_live_dn(self):
		so = self.so(10000)
		first = self.dn([(so, 1000)], submit=True)
		second = self.dn([(so, 500)])
		frappe.db.set_value(
			"Delivery Note", second.name, "custom_invoiced_item_id", first.custom_invoiced_item_id
		)
		p = self.plan(first.custom_invoiced_item_id, 800)
		self.assertEqual((p["ok"], p["code"], p["retryable"]), (False, "DUPLICATE_LIVE_DN", False))
		self.assertIn(first.name, p["message"])
		self.assertIn(second.name, p["message"])

	def test_so_closed(self):
		so = self.so(10000)
		dn = self.dn([(so, 1000)], submit=True)
		frappe.db.set_value("Sales Order", so.name, "status", "On Hold")
		p = self.plan(dn.custom_invoiced_item_id, 800)
		self.assertEqual((p["ok"], p["code"], p["live_delivery_note"]), (False, "SO_CLOSED", dn.name))

	def test_invoiced(self):
		for submit_si in (False, True):
			with self.subTest(submitted_invoice=submit_si):
				so = self.so(10000)
				dn = self.dn([(so, 1000)], submit=True, posting_date="2026-08-15")
				fx.make_sales_invoice(so, "2026-08-01", "2026-08-31", submit=submit_si)
				p = self.plan(dn.custom_invoiced_item_id, 800)
				self.assertEqual((p["ok"], p["code"]), (False, "INVOICED"))

	def test_a_draft_dn_is_never_invoiced(self):
		so = self.so(10000)
		dn = self.dn([(so, 1000)], posting_date="2026-08-15")
		fx.make_sales_invoice(so, "2026-08-01", "2026-08-31")
		self.assertTrue(self.plan(dn.custom_invoiced_item_id, 800)["ok"])

	def test_period_closed(self):
		dn = self.may_dn(submit=True)
		p = self.plan(dn.custom_invoiced_item_id, 800)
		self.assertEqual((p["ok"], p["code"]), (False, "PERIOD_CLOSED"))
		self.assertEqual(frappe.local.message_log, [], "the refusal must not leak a msgprint")

	def test_period_closed_does_not_block_deleting_a_draft(self):
		dn = self.may_dn(submit=False)
		self.assertEqual(self.plan(dn.custom_invoiced_item_id, 800)["code"], "PERIOD_CLOSED")
		self.assertTrue(self.plan(dn.custom_invoiced_item_id, 0)["ok"])

	def test_plan_writes_nothing(self):
		so = self.so(10000)
		dn = self.dn([(so, 1000)], submit=True)
		frappe.db.commit()
		modified = frappe.db.get_value("Delivery Note", dn.name, "modified")
		self.plan(dn.custom_invoiced_item_id, 800)
		self.assertEqual(frappe.db.get_value("Delivery Note", dn.name, "modified"), modified)
		self.assertIsNone(self.log())

	def test_invalid_input(self):
		p = self.plan(str(uuid.uuid4()), -1)
		self.assertEqual((p["ok"], p["code"]), (False, "ERP_VALIDATION"))


class TestSharedChecks(QtyCorrectionTestCase):
	"""dn_validation / dn_invoice_link now expose their checks for the amend; their own hooks must
	behave exactly as before."""

	def test_enforce_so_headroom_still_refuses_an_over_punch(self):
		so = self.so(1000)
		with self.assertRaisesRegex(frappe.ValidationError, "exceeds what Sales Order"):
			self.dn([(so, 1200)])

	def test_so_headroom_shortfalls(self):
		from fuelbuddy_crm.dn_validation import so_headroom_shortfalls

		so = self.so(1000)
		dn = self.dn([(so, 600)])
		self.assertEqual(so_headroom_shortfalls(dn), [])
		dn.items[0].qty = 1100
		(short,) = so_headroom_shortfalls(dn)
		self.assertEqual((short.so_detail, short.increase, short.available), (so.items[0].name, 500, 400))

	def test_covering_invoices(self):
		from fuelbuddy_crm.dn_invoice_link import covering_invoices

		so = self.so(10000)
		draft = self.dn([(so, 100)], posting_date="2026-08-10")
		submitted = self.dn([(so, 200)], submit=True, posting_date="2026-08-15")
		si = fx.make_sales_invoice(so, "2026-08-01", "2026-08-31")
		self.assertEqual(covering_invoices(draft), set())  # drafts are never billed
		self.assertEqual(covering_invoices(submitted), {si.name})

	def test_cancelling_an_invoiced_dn_still_reallocates_its_invoice(self):
		so = self.so(10000)
		dn = self.dn([(so, 200)], submit=True, posting_date="2026-08-15")
		si = fx.make_sales_invoice(so, "2026-08-01", "2026-08-31")
		self.assertEqual(frappe.db.get_value("Delivery Note", dn.name, "custom_sales_invoice"), si.name)
		dn.reload()
		dn.cancel()
		self.assertIsNone(frappe.db.get_value("Delivery Note", dn.name, "custom_sales_invoice"))


class TestDropCopiedIdempotencyKey(FrappeTestCase):
	"""Every Delivery Note insert runs dn_versioning.drop_copied_idempotency_key."""

	def test_hook_is_registered_from_dn_versioning(self):
		before_insert = frappe.get_hooks("doc_events").get("Delivery Note", {}).get("before_insert", [])
		self.assertIn("fuelbuddy_crm.dn_versioning.drop_copied_idempotency_key", before_insert)

	def test_hook_clears_a_copied_key_and_keeps_the_amend_s_own(self):
		from fuelbuddy_crm.dn_versioning import drop_copied_idempotency_key

		class Doc(dict):
			def __init__(self, key, flag=None):
				super().__init__(custom_qc_idempotency_key=key)
				self.flags = frappe._dict(qc_idempotency_key=flag)

			def set(self, field, value):
				self[field] = value

		copied = Doc("ep-1")  # a UI amendment copied the corrected DN's key
		drop_copied_idempotency_key(copied)
		self.assertIsNone(copied["custom_qc_idempotency_key"])

		own = Doc("ep-2", flag="ep-2")  # amend_delivery_note's own amendment
		drop_copied_idempotency_key(own)
		self.assertEqual(own["custom_qc_idempotency_key"], "ep-2")
