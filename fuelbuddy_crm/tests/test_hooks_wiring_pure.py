# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Delivery Note hook wiring that more than one change relies on, without a site.

Runs as plain Python from the app root (``python3 -m unittest discover -s fuelbuddy_crm/tests -p
"test_*_pure.py"``) and under ``bench run-tests``. hooks.py is plain dicts: a merge that writes an
event key twice keeps only the last value, and nothing else would notice the dropped handler.
"""

import ast
import importlib.util
import itertools
import os
import unittest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOKS = os.path.join(APP, "hooks.py")
_SEQ = itertools.count()


def load_plain(path):
	spec = importlib.util.spec_from_file_location(f"_hooks_wiring_pure_{next(_SEQ)}", path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def as_list(value):
	return [value] if isinstance(value, str) else list(value)


class TestDeliveryNoteHookWiring(unittest.TestCase):
	def setUp(self):
		self.dn = load_plain(HOOKS).doc_events["Delivery Note"]

	def test_before_cancel_runs_the_line_guard_then_releases_the_live_key(self):
		self.assertEqual(
			as_list(self.dn["before_cancel"]),
			[
				"fuelbuddy_crm.billing_recheck.line_guard.check",
				"fuelbuddy_crm.dn_validation.set_live_invoiced_item_key",
			],
		)

	def test_before_submit_takes_the_line_guard(self):
		self.assertIn("fuelbuddy_crm.billing_recheck.line_guard.check", as_list(self.dn["before_submit"]))

	def test_on_submit_keeps_the_reservation_sync_and_the_install_counter(self):
		on_submit = as_list(self.dn["on_submit"])
		self.assertIn("fuelbuddy_crm.dn_validation.sync_draft_reservation", on_submit)
		self.assertIn("fuelbuddy_crm.billing_recheck.install.count_not_installed", on_submit)

	def test_before_insert_keeps_versioning_and_drops_a_copied_qc_key(self):
		before_insert = as_list(self.dn["before_insert"])
		self.assertIn("fuelbuddy_crm.dn_versioning.set_amended_version", before_insert)
		self.assertIn("fuelbuddy_crm.dn_versioning.drop_copied_idempotency_key", before_insert)


class TestNoRepeatedHookKeys(unittest.TestCase):
	def test_no_dict_in_hooks_repeats_a_key(self):
		with open(HOOKS) as fh:
			tree = ast.parse(fh.read())
		repeated = []
		for node in ast.walk(tree):
			if isinstance(node, ast.Dict):
				keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
				repeated += sorted({k for k in keys if keys.count(k) > 1})
		self.assertEqual(repeated, [])


if __name__ == "__main__":
	unittest.main()
