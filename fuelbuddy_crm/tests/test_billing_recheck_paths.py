# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Which path the billing re-check takes, and what each path does (IDEV-3268).

The database is mocked: these check the decisions (switch, guard, fast path, fallbacks, FIFO writes,
return add-on gating, bulk refusal reasons, counters, install state, the runtime code guard) without a
site. They need frappe and erpnext importable (a bench, or a virtualenv with both installed) and skip
otherwise:

    python -m unittest fuelbuddy_crm.tests.test_billing_recheck_paths
"""

import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

HAVE_APPS = all(importlib.util.find_spec(app) for app in ("frappe", "erpnext"))

if HAVE_APPS:
	import frappe

	from fuelbuddy_crm.billing_recheck import bulk, config, fingerprint, guard, install, observe, walk


def row(name, parent, billed_amt=0.0):
	return frappe._dict(name=name, parent=parent, billed_amt=billed_amt)


_MISSING = object()
_LOCALS = ("site", "site_path", "conf", "flags", "session", "db", "billing_recheck_frames")


def _save_locals():
	return {name: getattr(frappe.local, name, _MISSING) for name in _LOCALS}


def _restore_locals(saved):
	"""Put frappe.local back as it was (under bench run-tests it holds the real site and connection)."""
	for name, value in saved.items():
		if value is _MISSING:
			try:
				delattr(frappe.local, name)
			except AttributeError:
				pass
		else:
			setattr(frappe.local, name, value)


@unittest.skipUnless(HAVE_APPS, "needs frappe and erpnext importable")
class PathCase(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.mkdtemp()
		self.addCleanup(shutil.rmtree, self.tmp)
		self.addCleanup(_restore_locals, _save_locals())
		frappe.local.site = "billing-recheck.test"
		frappe.local.site_path = self.tmp
		frappe.local.conf = frappe._dict()
		frappe.local.flags = frappe._dict()
		frappe.local.session = frappe._dict(user="tester@example.com")
		frappe.local.db = mock.MagicMock()
		self.stock = mock.MagicMock(return_value=["DN-A", "DN-B", "DN-A"])
		self.counted = []
		self.patches = [
			mock.patch.object(install, "stock_walk", return_value=self.stock),
			mock.patch.object(observe, "count", side_effect=lambda path, *a, **k: self.counted.append(path)),
			mock.patch.object(guard, "mismatches", return_value=[]),
		]
		for p in self.patches:
			p.start()
			self.addCleanup(p.stop)

	def conf(self, **values):
		with open(os.path.join(self.tmp, "site_config.json"), "w") as fh:
			json.dump(values, fh)

	def line(self, rows=10, parents=10, si_detail=0, nonzero=0):
		return mock.patch.object(
			walk,
			"line_stats",
			return_value=SimpleNamespace(rows=rows, parents=parents, si_detail=si_detail, nonzero=nonzero),
		)


class TestWalkPaths(PathCase):
	def test_switch_off_runs_stock(self):
		self.conf()
		self.assertEqual(walk.update_billed_amount_based_on_so("SOI-1"), ["DN-A", "DN-B", "DN-A"])
		self.stock.assert_called_once_with("SOI-1", True)
		self.assertEqual(self.counted, [])

	def test_switch_only_counts_from_site_config_json(self):
		frappe.local.conf = frappe._dict({config.WALK: 1})  # as if from common_site_config.json
		with mock.patch.object(observe, "warn_once") as warn:
			self.assertFalse(config.walk_enabled())
		warn.assert_called_once()
		self.conf(billing_recheck_walk=1)
		self.assertTrue(config.walk_enabled())

	def test_guard_mismatch_runs_stock_and_counts(self):
		self.conf(billing_recheck_walk=1)
		with mock.patch.object(guard, "mismatches", return_value=["erpnext:x"]):
			self.assertEqual(walk.update_billed_amount_based_on_so("SOI-1", False), ["DN-A", "DN-B", "DN-A"])
		self.stock.assert_called_once_with("SOI-1", False)
		self.assertEqual(self.counted, ["guard_disabled"])

	def test_si_detail_falls_back(self):
		self.conf(billing_recheck_walk=1)
		with self.line(si_detail=1):
			walk.update_billed_amount_based_on_so("SOI-1")
		self.stock.assert_called_once()
		self.assertEqual(self.counted, ["fallback_si_detail"])

	def test_fast_path(self):
		self.conf(billing_recheck_walk=1)
		with (
			self.line(rows=5, parents=4),  # even with a two-row DN: every row stays 0
			mock.patch.object(walk, "billed_against_so_of", return_value=0),
			mock.patch.object(walk, "has_direct_billing", return_value=False),
			mock.patch.object(walk, "changes") as changes,
		):
			self.assertEqual(walk.update_billed_amount_based_on_so("SOI-1"), [])
		changes.assert_not_called()
		self.stock.assert_not_called()
		frappe.db.set_value.assert_not_called()
		self.assertEqual(self.counted, ["fast"])

	def test_no_fast_path_when_anything_is_billed(self):
		self.conf(billing_recheck_walk=1)
		for billed, direct, nonzero in ((10.0, False, 0), (0, True, 0), (0, False, 3)):
			with self.subTest(billed=billed, direct=direct, nonzero=nonzero):
				self.counted.clear()
				with (
					self.line(nonzero=nonzero),
					mock.patch.object(walk, "billed_against_so_of", return_value=billed),
					mock.patch.object(walk, "has_direct_billing", return_value=direct),
					mock.patch.object(walk, "changes", return_value=[]),
				):
					walk.update_billed_amount_based_on_so("SOI-1")
				self.assertEqual(self.counted, ["fifo"])

	def test_two_row_delivery_note_falls_back(self):
		self.conf(billing_recheck_walk=1)
		with (
			self.line(rows=5, parents=4, nonzero=2),
			mock.patch.object(walk, "billed_against_so_of", return_value=1.0),
		):
			walk.update_billed_amount_based_on_so("SOI-1")
		self.stock.assert_called_once()
		self.assertEqual(self.counted, ["fallback_multi_item"])

	def test_fifo_writes_only_changed_rows_and_returns_their_parents(self):
		self.conf(billing_recheck_walk=1)
		changed = [(row("DNI-3", "DN-3"), 100.0), (row("DNI-4", "DN-4"), 50.0), (row("DNI-9", "DN-3"), 0)]
		with (
			self.line(nonzero=3),
			mock.patch.object(walk, "billed_against_so_of", return_value=450.0),
			mock.patch.object(walk, "changes", return_value=changed) as changes,
		):
			self.assertEqual(walk.update_billed_amount_based_on_so("SOI-1", False), ["DN-3", "DN-4"])
		changes.assert_called_once_with("SOI-1", 450.0)
		self.assertEqual(
			frappe.db.set_value.call_args_list,
			[
				mock.call("Delivery Note Item", "DNI-3", "billed_amt", 100.0, update_modified=False),
				mock.call("Delivery Note Item", "DNI-4", "billed_amt", 50.0, update_modified=False),
				mock.call("Delivery Note Item", "DNI-9", "billed_amt", 0, update_modified=False),
			],
		)
		self.stock.assert_not_called()
		self.assertEqual(self.counted, ["fifo"])


class TestReturnAddOn(PathCase):
	def doc(self, is_return=1, items=(("DNI-R1", "DNI-7"),)):
		return frappe._dict(
			name="RET-1",
			is_return=is_return,
			return_against="DN-7",
			items=[frappe._dict(name=n, dn_detail=d, so_detail="SOI-1") for n, d in items],
		)

	def run_wrapper(self, doc, walk_path):
		def original(self_, update_modified=True):
			walk._record("SOI-1", ["DN-1"] if walk_path == "fifo" else ["DN-1", "DN-7"], walk_path == "fifo")

		wrapped = walk.wrap_update_billing_status(original)
		with mock.patch.object(walk, "refresh_returned") as refresh:
			wrapped(doc)
		return refresh

	def test_refresh_after_a_reduced_walk_on_a_return(self):
		refresh = self.run_wrapper(self.doc(), "fifo")
		refresh.assert_called_once()
		_doc, frame, _um = refresh.call_args[0]
		self.assertEqual(frame, {"returned": {"DN-1"}, "reduced": {"SOI-1"}})

	def test_no_refresh_after_stock_walk_or_for_a_normal_dn(self):
		self.run_wrapper(self.doc(), "stock").assert_not_called()
		self.run_wrapper(self.doc(is_return=0), "fifo").assert_not_called()

	def test_refresh_returned_refreshes_only_uncovered_siblings(self):
		frame = {"returned": {"DN-1"}, "reduced": {"SOI-1"}}
		frappe.db.sql_list.side_effect = [["DN-7", "DN-1"], ["DN-7", "DN-1"]]
		fake_doc = mock.MagicMock()
		with mock.patch.object(frappe, "get_doc", return_value=fake_doc) as get_doc:
			todo = walk.refresh_returned(self.doc(items=(("DNI-R1", "DNI-7"), ("DNI-R2", "DNI-1"))), frame)
		self.assertEqual(todo, ["DN-7"])
		get_doc.assert_called_once_with("Delivery Note", "DN-7")
		fake_doc.update_billing_percentage.assert_called_once_with(update_modified=True)

	def test_nothing_to_refresh(self):
		frame = {"returned": {"DN-7"}, "reduced": {"SOI-1"}}
		frappe.db.sql_list.return_value = ["DN-7"]
		with mock.patch.object(frappe, "get_doc") as get_doc:
			self.assertEqual(walk.refresh_returned(self.doc(), frame), [])
		get_doc.assert_not_called()


class TestBulkDecisions(PathCase):
	def test_small_sets_use_stocks_loop(self):
		self.conf(billing_recheck_bulk_over=5)
		fake_doc = mock.MagicMock()
		with (
			mock.patch.object(frappe, "get_doc", return_value=fake_doc),
			mock.patch.object(bulk, "bulk_refresh") as br,
		):
			bulk.refresh({"DN-1", "DN-2"})
		br.assert_not_called()
		self.assertEqual(fake_doc.update_billing_percentage.call_count, 2)

	def test_refusal_reasons(self):
		self.conf(billing_recheck_bulk_over=1)
		names = {"DN-1", "DN-2", "DN-3"}
		with mock.patch.object(bulk, "safe_gap_reasons", return_value=[]):
			frappe.db.sql_list.return_value = ["DN-1", "DN-2", "DN-3"]
			self.assertIsNone(bulk.why_stock(names))
			frappe.db.sql_list.return_value = ["DN-1", "DN-2"]
			self.assertEqual(bulk.why_stock(names), "names")
			self.assertEqual(bulk.why_stock({"DN-1", None, "DN-3"}), "blank_name")
			frappe.local.flags.in_patch = True
			self.assertEqual(bulk.why_stock(names), "flag_in_patch")
			frappe.local.flags.in_patch = False
			with mock.patch.object(guard, "mismatches", return_value=["x"]):
				self.assertEqual(bulk.why_stock(names), "guard")
		with mock.patch.object(bulk, "safe_gap_reasons", return_value=["hook:Comment.validate:x.y"]):
			with mock.patch.object(observe, "warn_once"):
				self.assertEqual(bulk.why_stock(names), "gap")

	def test_gap_check_errors_mean_stock(self):
		with mock.patch.object(bulk, "gap_reasons", side_effect=RuntimeError("boom")):
			self.assertEqual(bulk.safe_gap_reasons(), ["error:RuntimeError('boom')"])

	def test_invoice_wrapper(self):
		original = mock.MagicMock()
		original.__name__ = original.__qualname__ = "update_billing_status_in_dn"
		wrapped = bulk.wrap_update_billing_status_in_dn(original)
		si = frappe._dict(
			name="SI-1",
			is_return=0,
			update_billed_amount_in_delivery_note=1,
			items=[
				frappe._dict(dn_detail="DNI-1", delivery_note="DN-1", so_detail="SOI-1"),
				frappe._dict(dn_detail=None, delivery_note=None, so_detail="SOI-2"),
			],
		)
		self.conf()
		wrapped(si)
		original.assert_called_once_with(si, True)

		original.reset_mock()
		self.conf(billing_recheck_bulk_over=10)
		with mock.patch.object(guard, "mismatches", return_value=["x"]):
			wrapped(si)
		original.assert_called_once()
		self.assertEqual(self.counted, ["guard_disabled"])

		original.reset_mock()
		frappe.db.sql.return_value = [(123.0,)]
		dn_mod, si_mod, _sc, _su = guard.modules()
		with (
			mock.patch.object(
				si_mod, "update_billed_amount_based_on_so", return_value=["DN-5"], create=True
			) as w,
			mock.patch.object(bulk, "refresh") as refresh,
		):
			wrapped(si, False)
		original.assert_not_called()
		frappe.db.set_value.assert_called_once_with(
			"Delivery Note Item", "DNI-1", "billed_amt", 123.0, update_modified=False
		)
		w.assert_called_once_with("SOI-2", False)
		refresh.assert_called_once_with({"DN-1", "DN-5"}, False)


class TestInstallAndGuard(PathCase):
	def test_install_is_idempotent_and_complete(self):
		install.install()
		install.install()
		dn_mod, si_mod, _sc, _su = guard.modules()
		self.assertIs(dn_mod.update_billed_amount_based_on_so, walk.update_billed_amount_based_on_so)
		self.assertIs(si_mod.update_billed_amount_based_on_so, walk.update_billed_amount_based_on_so)
		inner = dn_mod.DeliveryNote.update_billing_status.__wrapped__
		self.assertFalse(getattr(inner, "_billing_recheck", False))  # wrapped exactly once
		with mock.patch.object(install, "stock_walk", wraps=lambda: install._STATE["walk"]):
			self.assertTrue(install.installed())

	def test_not_installed_is_counted_when_switched_on(self):
		self.conf(billing_recheck_walk=1)
		with (
			mock.patch.object(install, "installed", return_value=False),
			mock.patch.object(install, "install") as inst,
		):
			install.count_not_installed(frappe._dict(doctype="Delivery Note", name="DN-1"), "on_submit")
		self.assertEqual(self.counted, ["not_installed"])
		inst.assert_called_once()

	def test_code_guard_on_this_interpreter(self):
		for p in self.patches:  # the real guard, with the real stock walk captured
			p.stop()
		install.install()
		import erpnext

		pins = fingerprint.load_pins()
		if erpnext.__version__ not in pins["erpnext"] or frappe.__version__ not in pins["frappe"]:
			self.skipTest("installed erpnext / frappe versions are not pinned")
		self.assertEqual(guard._check_code()["mismatch"], [])
		other_pins = json.loads(json.dumps(pins))
		other_pins["frappe"][frappe.__version__]["segments"]["document.Document.db_set"] = "0" * 64
		with mock.patch.object(fingerprint, "load_pins", return_value=other_pins):
			self.assertEqual(guard._check_code()["mismatch"], ["frappe:document.Document.db_set"])


if __name__ == "__main__":
	unittest.main()
