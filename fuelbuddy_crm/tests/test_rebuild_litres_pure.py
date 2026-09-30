# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""The manual invoice rebuild counting litres (IDEV-3270) without a site: dn_unbilled_litres and
auto_invoicing.rebuild_lines_from_dn_range.

Runs as plain Python from the app directory and under ``bench run-tests``:

    python3 -m unittest fuelbuddy_crm.tests.test_rebuild_litres_pure

Both modules are loaded with a SQLite-backed fake frappe (rebuild_litres_fake_frappe.py), never the
real one. ERPNext's own billing fields (DN status, billed_amt) are written by the fixtures as ERPNext
would leave them, so the tests can show the amount-based rule (switch off) and the litre rule (switch
on) side by side on the same data.
"""

import os
import unittest

try:
	from . import rebuild_litres_fake_frappe as ff
except ImportError:  # run as a top-level module by unittest discover
	import rebuild_litres_fake_frappe as ff

SWITCH = "invoice_rebuild_by_litres"
FROM, TO = "2026-08-01", "2026-08-31"  # the manual invoice's DN window
LIST, NET = 3.60, 3.3714  # DN value per litre; invoice rate after the deal discount


def row(so_detail, **fields):
	return ff._dict(name=f"L-{so_detail}", so_detail=so_detail, **fields)


class TestPour(unittest.TestCase):
	"""dn_unbilled_litres.unbilled_litres: ERPNext's oldest-first pour, in litres."""

	@classmethod
	def setUpClass(cls):
		cls.fake = ff.make()
		cls.mod = ff.load(cls.fake, "dn_unbilled_litres")

	@classmethod
	def tearDownClass(cls):
		ff.dispose(cls.fake)

	def pour(self, lines, pools, direct=None, returned=None):
		return self.mod.unbilled_litres(lines, pools, direct or {}, returned or {})

	def test_fills_the_oldest_lines_first(self):
		lines = [
			ff._dict(name=n, so_detail="A", stock_qty=q) for n, q in (("d1", 100), ("d2", 100), ("d3", 100))
		]
		self.assertEqual(self.pour(lines, {"A": 150}), {"d1": 0, "d2": 50, "d3": 100})

	def test_each_sales_order_line_has_its_own_pool(self):
		lines = [
			ff._dict(name="a1", so_detail="A", stock_qty=100),
			ff._dict(name="b1", so_detail="B", stock_qty=100),
		]
		self.assertEqual(self.pour(lines, {"A": 500}), {"a1": 0, "b1": 100})

	def test_direct_billing_counts_for_its_line_and_never_spills(self):
		lines = [
			ff._dict(name="d1", so_detail="A", stock_qty=100),
			ff._dict(name="d2", so_detail="A", stock_qty=100),
		]
		self.assertEqual(self.pour(lines, {}, direct={"d1": 250}), {"d1": 0, "d2": 100})
		self.assertEqual(self.pour(lines, {"A": 30}, direct={"d1": 60}), {"d1": 10, "d2": 100})

	def test_a_line_delivered_against_an_invoice_is_billed_and_uses_the_pool(self):
		lines = [
			ff._dict(name="d1", so_detail="A", stock_qty=100, si_detail="SII-1"),
			ff._dict(name="d2", so_detail="A", stock_qty=100),
		]
		self.assertEqual(self.pour(lines, {"A": 150}), {"d1": 0, "d2": 50})
		# billed in full even when the pool is short of it; nothing is left for the next line
		self.assertEqual(self.pour(lines, {"A": 60}), {"d1": 0, "d2": 100})

	def test_returned_litres_are_not_delivered(self):
		lines = [
			ff._dict(name="d1", so_detail="A", stock_qty=100),
			ff._dict(name="d2", so_detail="A", stock_qty=100),
		]
		self.assertEqual(self.pour(lines, {"A": 80}, returned={"d1": 20}), {"d1": 0, "d2": 100})
		self.assertEqual(self.pour(lines, {}, returned={"d1": 150}), {"d1": 0, "d2": 100})

	def test_litres_fall_back_to_qty_times_conversion_factor(self):
		self.assertEqual(self.mod.litres(ff._dict(qty=10, conversion_factor=4.5)), 45)
		self.assertEqual(self.mod.litres(ff._dict(qty=10, stock_qty=12)), 12)
		self.assertEqual(self.mod.litres(ff._dict(qty=10)), 10)


class RebuildCase(unittest.TestCase):
	"""auto_invoicing.rebuild_lines_from_dn_range on the fake, one Sales Order line per letter."""

	switch = 1

	def setUp(self):
		self.fake = ff.make(site_config={SWITCH: self.switch} if self.switch is not None else None)
		self.db = self.fake.db
		self.ai = ff.load(self.fake)

	def tearDown(self):
		ff.dispose(self.fake)

	# ---- fixtures: documents as ERPNext leaves them ----------------------------------------------
	def dn(self, name, posting_date, lines, status="To Bill", docstatus=1, is_return=0):
		"""``lines``: [(so_detail, litres, fields)]; billed_amt and status are what ERPNext stored."""
		self.db.insert(
			"Delivery Note",
			name=name,
			posting_date=posting_date,
			status=status,
			docstatus=docstatus,
			is_return=is_return,
		)
		for idx, (so_detail, litres, fields) in enumerate(lines, 1):
			fields = {"rate": LIST, **fields}
			cf = fields.pop("conversion_factor", 1)
			rate = fields.pop("rate")
			qty = litres / cf
			self.db.insert(
				"Delivery Note Item",
				name=fields.pop("name", f"{name}-{idx}"),
				parent=name,
				idx=idx,
				so_detail=so_detail,
				qty=qty,
				stock_qty=litres,
				conversion_factor=cf,
				amount=round(qty * rate, 2),
				**fields,
			)

	def si(self, name, lines, docstatus=1, is_return=0, update_stock=0):
		"""``lines``: [(so_detail, litres, fields)]."""
		self.db.insert(
			"Sales Invoice", name=name, docstatus=docstatus, is_return=is_return, update_stock=update_stock
		)
		for idx, (so_detail, litres, fields) in enumerate(lines, 1):
			cf = fields.get("conversion_factor", 1)
			self.db.insert(
				"Sales Invoice Item",
				name=f"{name}-{idx}",
				parent=name,
				idx=idx,
				so_detail=so_detail,
				qty=litres / cf,
				stock_qty=litres,
				**fields,
			)

	def rebuild(self, *so_details, from_date=FROM, to_date=TO, **item_fields):
		"""A new manual invoice mapped from the Sales Order (one line per SO line) for the window;
		returns [(so_detail, qty)] after the rebuild hook."""
		items = [
			{"sales_order": "SO-1", "so_detail": s, "item_code": "FUEL", "qty": 1, **item_fields}
			for s in so_details
		]
		doc = ff.Invoice("CUST-1", items, custom_dn_from_date=from_date, custom_dn_to_date=to_date)
		self.ai.rebuild_lines_from_dn_range(doc)
		return [(r.so_detail, round(r.qty, 3)) for r in doc.items]

	def assertNothingOffered(self, *so_details, **kwargs):
		with self.assertRaisesRegex(ff.ValidationError, "No Delivery Notes found"):
			self.rebuild(*so_details, **kwargs)


class TestRebuildByLitres(RebuildCase):
	"""Switch on: the rebuild counts litres."""

	def test_fully_invoiced_litres_at_a_discount_are_not_offered(self):
		"""The SO 1068 shape: every litre invoiced, at the deal-discounted rate. ERPNext leaves the DN
		Partially Billed (1,348.56 of 1,440 billed); by litres nothing is left."""
		self.dn(
			"DN-1", "2026-08-10", [("A", 400, {"billed_amt": round(400 * NET, 2)})], status="Partially Billed"
		)
		self.si("SI-1", [("A", 400, {})])
		self.assertNothingOffered("A")

	def test_fully_invoiced_litres_at_a_changed_price_are_not_offered(self):
		"""DNs valued at two list prices (the July price on the first), one invoice for all the
		litres at the new net rate: the invoiced money runs out before the second DN's value."""
		self.dn(
			"DN-1", "2026-08-04", [("A", 300, {"rate": 3.4286, "billed_amt": 1028.58})], status="Completed"
		)
		self.dn(
			"DN-2",
			"2026-08-20",
			[("A", 300, {"rate": 3.6190, "billed_amt": 994.26})],
			status="Partially Billed",
		)
		self.si("SI-1", [("A", 600, {})])
		self.assertNothingOffered("A")

	def test_part_invoiced_dn_offers_only_its_remaining_litres(self):
		self.dn(
			"DN-1", "2026-08-10", [("A", 400, {"billed_amt": round(300 * NET, 2)})], status="Partially Billed"
		)
		self.si("SI-1", [("A", 300, {})])
		self.assertEqual(self.rebuild("A"), [("A", 100)])
		self.assertIn("DN-1", self.fake.messages[-1])

	def test_part_invoiced_at_a_higher_price_still_offers_its_remaining_litres(self):
		"""An invoice priced above the DN value covers the DN's money with fewer litres: ERPNext
		marks it Completed, but 100 L are not invoiced."""
		self.dn("DN-1", "2026-08-10", [("A", 400, {"rate": 3.0, "billed_amt": 1200})], status="Completed")
		self.si("SI-1", [("A", 300, {})])
		self.assertEqual(self.rebuild("A"), [("A", 100)])

	def test_split_dn_offers_its_line_on_the_second_order(self):
		"""The IDEV-3270 production case: one punch carries a line on each of two SO lines; the first
		SO line is invoiced, the second SO's manual invoice still takes the DN's line on it."""
		self.dn("DN-1", "2026-08-15", [("A", 100, {}), ("B", 150, {})], status="Partially Billed")
		self.dn("DN-2", "2026-08-16", [("B", 250, {})])
		self.si("SI-A", [("A", 100, {})])
		self.assertEqual(self.rebuild("B"), [("B", 400)])
		self.assertNothingOffered("A")

	def test_invoiced_litres_fill_the_oldest_dns_whatever_the_window(self):
		"""ERPNext's pour, in litres: one 100 L invoice fills the July DN first, so the August DN is
		what is left, whichever window the invoice was raised for. Litres never exceed delivery."""
		self.dn("DN-JUL", "2026-07-20", [("A", 100, {})])
		self.dn("DN-AUG", "2026-08-20", [("A", 100, {})])
		self.si("SI-1", [("A", 100, {})])
		self.assertNothingOffered("A", from_date="2026-07-01", to_date="2026-07-31")
		self.assertEqual(self.rebuild("A"), [("A", 100)])

	def test_a_draft_invoice_counts_and_a_cancelled_one_does_not(self):
		self.dn("DN-1", "2026-08-10", [("A", 400, {})])
		self.si("SI-DRAFT", [("A", 400, {})], docstatus=0)
		self.assertNothingOffered("A")
		self.db.sql("update `tabSales Invoice` set docstatus = 2 where name = 'SI-DRAFT'")
		self.assertEqual(self.rebuild("A"), [("A", 400)])

	def test_a_credit_note_gives_no_litres_back(self):
		self.dn("DN-1", "2026-08-10", [("A", 400, {})], status="To Bill")
		self.si("SI-1", [("A", 400, {})])
		self.si("CN-1", [("A", -400, {})], is_return=1)
		self.assertNothingOffered("A")

	def test_returned_litres_are_not_offered(self):
		self.dn("DN-1", "2026-08-10", [("A", 400, {"name": "DN-1-1"})])
		self.dn("RET-1", "2026-08-11", [("A", -100, {"dn_detail": "DN-1-1"})], status="Return", is_return=1)
		self.dn(
			"RET-2",
			"2026-08-12",
			[("A", -50, {"dn_detail": "DN-1-1"})],
			status="Draft",
			docstatus=0,
			is_return=1,
		)
		self.assertEqual(self.rebuild("A"), [("A", 300)])

	def test_a_closed_dn_is_not_offered_and_does_not_soak_up_invoiced_litres(self):
		self.dn("DN-C", "2026-08-01", [("A", 100, {})], status="Closed")
		self.dn("DN-D", "2026-08-02", [("A", 100, {})])
		self.assertEqual(self.rebuild("A"), [("A", 100)])
		self.si("SI-1", [("A", 100, {})])
		self.assertNothingOffered("A")

	def test_an_invoice_on_the_dn_line_bills_that_line(self):
		self.dn("DN-1", "2026-08-10", [("A", 400, {"name": "DN-1-1"})])
		self.dn("DN-2", "2026-08-11", [("A", 200, {})])
		self.si("SI-1", [("A", 400, {"dn_detail": "DN-1-1"})])
		self.assertEqual(self.rebuild("A"), [("A", 200)])

	def test_an_invoice_that_updates_stock_fills_no_dn(self):
		self.dn("DN-1", "2026-08-10", [("A", 400, {})])
		self.si("SI-POS", [("A", 400, {})], update_stock=1)
		self.assertEqual(self.rebuild("A"), [("A", 400)])

	def test_litres_come_back_in_the_line_uom(self):
		"""An Imperial Gallon SO line: 100 IG delivered (454.609 L), 50 IG invoiced."""
		cf = 4.54609
		self.dn("DN-1", "2026-08-10", [("A", 100 * cf, {"conversion_factor": cf})])
		self.si("SI-1", [("A", 50 * cf, {"conversion_factor": cf})])
		self.assertEqual(self.rebuild("A", conversion_factor=cf), [("A", 50)])

	def test_dns_outside_the_window_are_not_offered(self):
		self.dn("DN-JUL", "2026-07-31", [("A", 100, {})])
		self.dn("DN-AUG", "2026-08-15", [("A", 200, {})])
		self.dn("DN-SEP", "2026-09-01", [("A", 300, {})])
		self.assertEqual(self.rebuild("A"), [("A", 200)])
		lines_query = next(s for s in self.db.statements if "dn.posting_date <=" in s)
		self.assertNotIn("between", lines_query)  # the pour needs the DNs before the window too

	def test_an_invoice_line_with_no_litres_left_refuses_the_save(self):
		"""As before for a line with no delivery in range: one message, not a zero-quantity line."""
		self.dn("DN-1", "2026-08-10", [("A", 100, {}), ("B", 100, {})])
		self.si("SI-A", [("A", 100, {})])
		self.assertEqual(self.rebuild("B"), [("B", 100)])
		with self.assertRaisesRegex(ff.ValidationError, "No Delivery Notes found for FUEL"):
			self.rebuild("A", "B")


class TestRebuildByAmountWhenSwitchedOff(RebuildCase):
	"""Switch off (the default): the amount-based rule, unchanged, on the same data."""

	switch = None  # no site_config.json key at all

	def test_the_discounted_dn_is_offered_by_its_unbilled_amount(self):
		self.dn(
			"DN-1", "2026-08-10", [("A", 400, {"billed_amt": round(400 * NET, 2)})], status="Partially Billed"
		)
		self.si("SI-1", [("A", 400, {})])
		self.assertEqual(self.rebuild("A"), [("A", round(400 * (1440 - round(400 * NET, 2)) / 1440, 3))])

	def test_a_completed_dn_is_not_offered(self):
		self.dn("DN-1", "2026-08-10", [("A", 400, {"rate": 3.0, "billed_amt": 1200})], status="Completed")
		self.si("SI-1", [("A", 300, {})])
		self.assertNothingOffered("A")

	def test_the_litre_reads_do_not_run(self):
		self.dn("DN-1", "2026-08-10", [("A", 400, {})])
		self.assertEqual(self.rebuild("A"), [("A", 400)])
		self.assertFalse(any("tabSales Invoice Item" in s for s in self.db.statements))


class TestSwitch(unittest.TestCase):
	"""dn_unbilled_litres.litres_rebuild_enabled: the site's own site_config.json, off unless set there."""

	def enabled(self, site_config=None, common_config=None):
		fake = ff.make(site_config=site_config, common_config=common_config)
		try:
			return ff.load(fake, "dn_unbilled_litres").litres_rebuild_enabled(), fake.log.warnings
		finally:
			ff.dispose(fake)

	def test_off_by_default(self):
		self.assertEqual(self.enabled(), (False, []))
		self.assertEqual(self.enabled({"db_name": "x"}), (False, []))

	def test_on_only_when_set_on_the_site(self):
		self.assertEqual(self.enabled({SWITCH: 1}), (True, []))
		self.assertEqual(self.enabled({SWITCH: "1"}), (True, []))
		self.assertEqual(self.enabled({SWITCH: 0}), (False, []))

	def test_common_site_config_is_ignored_and_logged(self):
		on, warnings = self.enabled({"db_name": "x"}, common_config={SWITCH: 1})
		self.assertFalse(on)
		self.assertEqual(len(warnings), 1)
		self.assertIn(SWITCH, warnings[0])

	def test_an_unreadable_site_config_is_off(self):
		fake = ff.make()
		try:
			with open(os.path.join(fake.local.site_path, "site_config.json"), "w") as fh:
				fh.write("{not json")
			self.assertFalse(ff.load(fake, "dn_unbilled_litres").litres_rebuild_enabled())
		finally:
			ff.dispose(fake)


if __name__ == "__main__":
	unittest.main()
