# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""billing_recheck.fifo must compute exactly what ERPNext's own walk writes (IDEV-3268).

The reference is ERPNext v15.96.0's ``update_billed_amount_based_on_so`` itself: the verbatim source in
``fixtures/`` (checked here against the pinned fingerprint, so it is provably the pinned stock function)
is executed against a stand-in ``frappe`` that answers its three queries from synthetic rows and records
its ``set_value`` writes. Every value fifo.stock_values computes must be bit-identical (same repr, so
also the same int / float type) to what stock writes, on hand-made lines (frontier, returns and credit
notes, discounts, direct dn_detail billing, si_detail rows, over-billing, the 2**23 band) and on
thousands of random ones.

Pure: no site needed (``python -m unittest fuelbuddy_crm.tests.test_billing_recheck_fifo`` from the app
directory); it also runs under ``bench run-tests``.
"""

import ast
import pathlib
import random
import sys
import types
import unittest
from decimal import ROUND_HALF_EVEN, Decimal
from unittest import mock

from fuelbuddy_crm.billing_recheck import fifo
from fuelbuddy_crm.billing_recheck import fingerprint as fp

FIXTURE = (
	pathlib.Path(__file__).parent / "fixtures" / "erpnext_v15_96_0_update_billed_amount_based_on_so.py.txt"
)
SOURCE = FIXTURE.read_text()
PINNED = fp.load_pins()["erpnext"]["15.96.0"]["segments"]["delivery_note.update_billed_amount_based_on_so"]


def frappe_flt(s, precision=None, rounding_method=None):
	"""frappe.utils.data.flt (15.99.0 / 15.113.1, fingerprinted as data.flt) without precision, the only
	way the walk calls it."""
	assert precision is None
	if isinstance(s, str):
		s = s.replace(",", "")
	try:
		num = float(s)
	except Exception:
		num = 0.0
	return num


class Row(dict):
	"""frappe._dict: attribute access, None for a missing key."""

	__getattr__ = dict.get


class _Query:
	"""Absorbs a frappe.qb query chain; run() answers it."""

	def __init__(self, owner):
		self._owner = owner

	def __getattr__(self, name):
		return self

	def __call__(self, *args, **kwargs):
		return self

	def __eq__(self, other):
		return self

	__and__ = __or__ = __rand__ = __ror__ = __eq__
	__hash__ = object.__hash__

	def run(self, as_dict=False):
		if as_dict:  # stock's dn_details query
			return [Row(r) for r in self._owner.rows]
		return [(self._owner.billed_raw,)]  # stock's Sum() query: one row, NULL without invoices


class FakeFrappe:
	def __init__(self, billed_raw, rows, direct):
		self.billed_raw, self.rows, self.direct = billed_raw, rows, direct
		self.writes = []
		self.qb = _Query(self)
		self.db = types.SimpleNamespace(sql=self._sql, set_value=self._set_value)

	def _sql(self, query, name):
		assert "dn_detail=%s" in query
		return [(self.direct.get(name),)]  # aggregate without GROUP BY: one row, NULL when none

	def _set_value(self, doctype, name, field, value, update_modified=True):
		assert (doctype, field) == ("Delivery Note Item", "billed_amt")
		self.writes.append((name, value))


def run_stock(billed_raw, rows, direct):
	"""Execute ERPNext's function on the synthetic line: [(row name, value written)], returned parents."""
	fake = FakeFrappe(billed_raw, rows, direct)
	functions = types.ModuleType("frappe.query_builder.functions")
	functions.Sum = lambda *a, **k: _Query(fake)
	namespace = {"frappe": fake, "flt": frappe_flt}
	with mock.patch.dict(sys.modules, {"frappe.query_builder.functions": functions}):
		exec(compile(SOURCE, str(FIXTURE), "exec"), namespace)
		parents = namespace["update_billed_amount_based_on_so"]("SOI-LINE", True)
	return fake.writes, parents


def run_ours(billed_raw, rows, direct):
	res = [(billed_raw,)]  # what the qb query returns, read as walk.billed_against_so_of reads it
	billed_against_so = (res and res[0][0]) or 0
	values = fifo.stock_values(billed_against_so, [Row(r) for r in rows], direct, frappe_flt)
	return list(zip([r["name"] for r in rows], values, strict=True))


def line(amounts, si_detail=(), stored=None):
	return [
		{
			"name": f"DNI-{i:05d}",
			"amount": amount,
			"si_detail": f"SII-{i}" if i in si_detail else None,
			"parent": f"DN-{i:05d}",
			"billed_amt": None if stored is None else stored[i],
		}
		for i, amount in enumerate(amounts)
	]


class TestFixtureIsStock(unittest.TestCase):
	def test_fixture_matches_the_pinned_fingerprint(self):
		self.assertEqual(fp.segment_hash(ast.parse(SOURCE), fp.WALK), PINNED)


class TestStockValues(unittest.TestCase):
	def assertSameAsStock(self, billed_raw, rows, direct=None):
		direct = direct or {}
		writes, parents = run_stock(billed_raw, rows, direct)
		ours = run_ours(billed_raw, rows, direct)
		self.assertEqual([(n, repr(v)) for n, v in writes], [(n, repr(v)) for n, v in ours])
		self.assertEqual(parents, [r["parent"] for r in rows])
		return [v for _n, v in ours]

	def test_unbilled_line_is_all_zero(self):
		values = self.assertSameAsStock(None, line([100.0] * 12))
		self.assertEqual(values, [0] * 12)

	def test_frontier_row_is_partly_billed(self):
		values = self.assertSameAsStock(350.0, line([100.0] * 10))
		self.assertEqual(values[:5], [100.0, 100.0, 100.0, 50.0, 0])

	def test_discounted_amounts(self):
		amounts = [99.99, 150.123456789, 0.01, 1234.5, 7.777777777, 3.51 * 211.37, 15.95 * 43.9]
		self.assertSameAsStock(1234.5678, line(amounts * 5))

	def test_credit_note_reduces_the_invoiced_amount(self):
		# SUM() over an invoice of 1000 and a credit note of -250.5 on the same line
		self.assertSameAsStock(749.5, line([100.0, 250.25, 300.0, 99.25, 50.0]))

	def test_net_credit_leaves_nothing_to_spread(self):
		self.assertSameAsStock(-120.0, line([100.0, 50.0]))

	def test_direct_billing_and_fifo_mix(self):
		rows = line([100.0, 100.0, 100.0, 100.0, 100.0])
		direct = {"DNI-00001": 100.0, "DNI-00003": 40.0}
		values = self.assertSameAsStock(150.0, rows, direct)
		self.assertEqual(values, [100.0, 100.0, 50.0, 40.0, 0])

	def test_direct_billing_of_zero_sum(self):
		self.assertSameAsStock(10.0, line([5.0, 5.0, 5.0]), {"DNI-00000": 0.0, "DNI-00001": None})

	def test_si_detail_rows_consume_the_invoiced_amount(self):
		self.assertSameAsStock(260.0, line([100.0, 100.0, 100.0, 100.0], si_detail={0, 2}))

	def test_over_billing(self):
		values = self.assertSameAsStock(1000.0, line([100.0, 200.0, 300.0]))
		self.assertEqual(values, [100.0, 200.0, 300.0])

	def test_zero_and_negative_amount_rows(self):
		self.assertSameAsStock(75.0, line([0.0, -20.0, 50.0, 0.0, 60.0]))

	def test_amounts_above_the_round_trip_band(self):
		big = fifo.EXACT_ROUND_TRIP
		self.assertSameAsStock(
			big * 1.5 + 0.123, line([big + 0.1, big * 0.75 + 0.333333333, 12.5, big * 2.0])
		)

	def test_random_lines(self):
		rnd = random.Random(3268)
		for case in range(3000):
			n = rnd.randint(1, 60)
			scale = rnd.choice([1, 100, 10_000, 1_000_000, 2**23, 2**24])
			decimals = rnd.randint(0, 9)
			amounts = [round(rnd.uniform(-0.05, 1.0) * scale, decimals) for _ in range(n)]
			si_detail = {i for i in range(n) if rnd.random() < 0.05}
			direct = {
				f"DNI-{i:05d}": round(rnd.uniform(0, 1.1) * a, decimals)
				for i, a in enumerate(amounts)
				if rnd.random() < 0.2
			}
			total = sum(amounts)
			billed = rnd.choice([None, 0.0, round(rnd.uniform(-0.1, 1.2) * total, decimals)])
			with self.subTest(case=case):
				self.assertSameAsStock(billed, line(amounts, si_detail), direct)


class TestWriteSkip(unittest.TestCase):
	def test_needs_write(self):
		band = fifo.EXACT_ROUND_TRIP
		self.assertFalse(fifo.needs_write(12.5, 12.5))
		self.assertFalse(fifo.needs_write(0.0, 0))
		self.assertFalse(fifo.needs_write(-(band - 1.0), -(band - 1.0)))
		self.assertTrue(fifo.needs_write(12.5, 12.25))
		self.assertTrue(fifo.needs_write(None, 0))
		self.assertTrue(fifo.needs_write(float(band), float(band)))  # stock's rewrite may move it
		self.assertTrue(fifo.needs_write(-float(band), -float(band)))

	def test_changed_rows_are_the_rows_whose_value_differs(self):
		amounts = [100.0] * 6 + [fifo.EXACT_ROUND_TRIP + 1.5]
		stored = [100.0, 100.0, 100.0, 0.0, 0.0, 0.0, fifo.EXACT_ROUND_TRIP + 1.5]
		rows = [Row(r) for r in line(amounts, stored=stored)]
		values = fifo.stock_values(450.0, rows, {}, frappe_flt)
		changed = fifo.changed_rows(rows, values)
		# row 3 becomes 100, row 4 the new frontier (50), row 6 sits in the band and is always written
		self.assertEqual(
			[(r.name, v) for r, v in changed], [("DNI-00003", 100.0), ("DNI-00004", 50.0), ("DNI-00006", 0)]
		)

	def test_after_stock_ran_only_band_rows_change(self):
		rnd = random.Random(23)
		amounts = [round(rnd.uniform(0, 5e6), 2) for _ in range(200)] + [9e6 + 0.25]
		rows = [Row(r) for r in line(amounts)]
		values = fifo.stock_values(sum(amounts) * 0.6, rows, {}, frappe_flt)
		for row, value in zip(rows, values, strict=True):
			row["billed_amt"] = value
		changed = fifo.changed_rows(rows, values)
		self.assertEqual(
			[r.name for r, _v in changed],
			[r.name for r in rows if abs(r.billed_amt) >= fifo.EXACT_ROUND_TRIP],
		)

	def test_round_trip_band_under_a_decimal_model(self):
		"""The rationale for 2**23 under a model of the database (DECIMAL(21,9), the float rounded
		half-even to 9 places): below it, a stored decimal whose float equals the new value is exactly
		what writing that value stores; above it that can fail. The lab's ROUNDTRIP evidence checked
		the real MariaDB path; this only guards the arithmetic."""
		q = Decimal("0.000000001")
		rnd = random.Random(9)
		broken_above = 0
		for _ in range(20000):
			for top in (2**23, 2**26):
				stored = Decimal(rnd.randrange(0, top * 10**9)) * q
				value = float(stored)
				rewritten = Decimal(value).quantize(q, rounding=ROUND_HALF_EVEN)
				if top == 2**23:
					self.assertEqual(rewritten, stored)
				elif rewritten != stored:
					broken_above += 1
		self.assertGreater(broken_above, 0)


if __name__ == "__main__":
	unittest.main()
