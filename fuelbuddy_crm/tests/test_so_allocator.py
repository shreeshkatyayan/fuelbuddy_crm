# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""The Python SO allocator must reproduce erp-functions' JavaScript allocator exactly.

``fixtures/so_allocator_cases.json`` is the shared case set: inputs written by hand, every
``expected`` produced by executing the JavaScript (``fixtures/gen_so_allocator_cases.mjs``).
erp-functions replays a byte-identical copy (test/fixtures/so_allocator_cases.json, spec
test/soAllocatorCases.spec.js, which also pins the JS allocator's git blobs), so a rule changed in
one copy and not the other fails there or here. With ERP_FUNCTIONS set to an erp-functions
checkout, TestSharedCasesFile also checks the two copies are byte-identical. Regenerate after
changing the JavaScript (then copy the same file to erp-functions):

    ERP_FUNCTIONS=<erp-functions checkout> node fuelbuddy_crm/tests/fixtures/gen_so_allocator_cases.mjs \\
        > fuelbuddy_crm/tests/fixtures/so_allocator_cases.json

Pure: no site needed (``python -m unittest fuelbuddy_crm.tests.test_so_allocator`` from the
app directory), and it also runs under ``bench run-tests``.
"""

import json
import os
import pathlib
import unittest

from fuelbuddy_crm import so_allocator

CASES_FILE = pathlib.Path(__file__).parent / "fixtures" / "so_allocator_cases.json"
CASES = json.loads(CASES_FILE.read_text())


def _project(line):
	return {
		key: line.get(key) for key in ("against_sales_order", "so_detail", "qty", "uom", "conversion_factor")
	}


class TestSoAllocatorParity(unittest.TestCase):
	"""Exact equality on purpose: the port follows the JS operation for operation."""

	def test_epsilon(self):
		self.assertEqual(so_allocator.EPSILON, CASES["EPSILON"])

	def test_build_sales_order_query(self):
		for case in CASES["buildSalesOrderQuery"]:
			with self.subTest(case["name"]):
				self.assertEqual(so_allocator.build_sales_order_query(*case["args"]), case["expected"])

	def test_remaining_litres(self):
		for case in CASES["remainingLitres"]:
			with self.subTest(case["name"]):
				self.assertEqual(so_allocator.remaining_litres(case["soItem"]), case["expected"])

	def test_allocate(self):
		for case in CASES["allocate"]:
			with self.subTest(case["name"]):
				result = so_allocator.allocate(case["dispensedLitres"], case["candidates"])
				got = {
					"allocations": [
						{
							"so": a["so"]["name"],
							"so_detail": a["soItem"]["name"],
							"allocatedLitres": a["allocatedLitres"],
						}
						for a in result["allocations"]
					],
					"leftover": result["leftover"],
				}
				self.assertEqual(got, case["expected"])

	def test_reconcile_delivery_note_lines(self):
		for case in CASES["reconcileDeliveryNoteLines"]:
			with self.subTest(case["name"]):
				result = so_allocator.reconcile_delivery_note_lines(
					case["existingLines"], case["newQtyLitres"], case["deltaCandidates"]
				)
				got = {
					"changed": result["changed"],
					"error": result.get("error"),
					"leftover": result.get("leftover"),
					"lines": [_project(line) for line in result["lines"]] if result.get("lines") else None,
				}
				self.assertEqual(got, case["expected"])

	def test_case_file_covers_every_function(self):
		for key in ("buildSalesOrderQuery", "remainingLitres", "allocate", "reconcileDeliveryNoteLines"):
			self.assertTrue(CASES[key], key)


class TestSoAllocatorBehaviour(unittest.TestCase):
	"""Properties the amend relies on, beyond the shared cases."""

	def test_reconcile_does_not_mutate_its_input(self):
		lines = [{"so_detail": "a", "against_sales_order": "SO-A", "qty": 10, "conversion_factor": 1}]
		so_allocator.reconcile_delivery_note_lines(lines, 5)
		self.assertEqual(lines[0]["qty"], 10)

	def test_new_line_takes_asset_warehouse_and_cost_centre_from_the_last_line(self):
		lines = [
			{
				"so_detail": "a",
				"against_sales_order": "SO-A",
				"qty": 100,
				"conversion_factor": 1,
				"warehouse": "Truck 7 - FFSL",
				"cost_center": "Main - FFSL",
				"custom_customer_asset": "AS-1",
			}
		]
		candidates = [
			{"so": {"name": "SO-A"}, "soItem": {"name": "a", "qty": 100, "delivered_qty": 100}},
			{
				"so": {"name": "SO-B"},
				"soItem": {"name": "b", "qty": 50, "uom": "IG", "conversion_factor": 4.546, "rate": 16},
			},
		]
		result = so_allocator.reconcile_delivery_note_lines(lines, 145.46, candidates)
		new = result["lines"][1]
		self.assertEqual(
			(new["warehouse"], new["cost_center"], new["custom_customer_asset"]),
			("Truck 7 - FFSL", "Main - FFSL", "AS-1"),
		)
		self.assertEqual((new["uom"], new["rate"], new["against_sales_order"]), ("IG", 16, "SO-B"))
		self.assertAlmostEqual(new["qty"] * 4.546, 45.46, places=9)

	def test_number_coercion_matches_javascript(self):
		self.assertEqual(so_allocator._number(None, 0), 0)
		self.assertEqual(so_allocator._number("", 1), 1)
		self.assertEqual(so_allocator._number("  12.5 ", 0), 12.5)
		self.assertEqual(so_allocator._number("abc", 1), 1)
		self.assertEqual(so_allocator._number(0, 1), 1)
		self.assertEqual(so_allocator._number(True, 0), 1)


class TestSharedCasesFile(unittest.TestCase):
	"""The two copies of the case file must be the same bytes (skipped without ERP_FUNCTIONS)."""

	def test_identical_to_the_erp_functions_copy(self):
		root = os.environ.get("ERP_FUNCTIONS")
		if not root:
			self.skipTest("set ERP_FUNCTIONS to an erp-functions checkout to compare the case files")
		theirs = pathlib.Path(root) / "test" / "fixtures" / "so_allocator_cases.json"
		self.assertTrue(theirs.is_file(), f"{theirs} is missing")
		self.assertEqual(
			CASES_FILE.read_bytes(),
			theirs.read_bytes(),
			"so_allocator_cases.json differs from erp-functions' copy: regenerate once, copy to both",
		)
