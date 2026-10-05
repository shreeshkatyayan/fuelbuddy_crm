# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""check_correction_raise (IDEV-3266) as plain Python: no bench, no site.

qty_correction is loaded against a stand-in for the few frappe calls the raise check makes, so its
own logic runs here: which Delivery Notes it checks, that its INVOICED test is the amend's
(dn_invoice_link.covering_invoices), which returns count, which code wins and the answer's shape.
The SQL behind covering_invoices and the returns filter runs on a real site in
test_qty_correction.TestCheckCorrectionRaise.

    python3 -m unittest fuelbuddy_crm.tests.test_raise_check_pure     (from the app directory)

It also runs under bench run-tests: the module is loaded into a private package object and
sys.modules is restored straight after, so nothing else in the process sees the stand-in.
"""

import importlib
import pathlib
import sys
import types
import unittest
from unittest.mock import patch

APP = pathlib.Path(__file__).resolve().parents[1]
IID = "0b7c7d1e-5a0e-4a3f-9d7e-1f2a3b4c5d6e"
LIVE_FILTERS = {"custom_invoiced_item_id": IID, "docstatus": ["<", 2], "is_return": 0}


class _dict(dict):
	"""frappe._dict: keys read as attributes, a missing one as None."""

	__getattr__ = dict.get


class _NoDatabase:
	"""The raise check reads through get_all / get_doc only (covering_invoices is patched)."""

	def __getattr__(self, name):
		raise AssertionError(f"check_correction_raise used frappe.db.{name}")


class FakeFrappe(types.ModuleType):
	"""frappe, as far as qty_correction's imports and check_correction_raise reach."""

	_dict = _dict

	def __init__(self):
		super().__init__("frappe")
		self.permitted = True
		self.delivery_notes = []  # rows: name, creation, docstatus, is_return, iid, return_against
		self.get_all_calls = []
		self.messages_cleared = 0
		self.db = _NoDatabase()

	@staticmethod
	def _(text, *args, **kwargs):
		return text

	@staticmethod
	def whitelist(*args, **kwargs):
		return lambda fn: fn

	def has_permission(self, doctype, ptype="read", *args, **kwargs):
		return self.permitted

	def get_all(self, doctype, filters=None, fields=None, order_by=None, pluck=None, **kwargs):
		if doctype != "Delivery Note":
			raise AssertionError(f"unexpected get_all on {doctype}")
		self.get_all_calls.append(
			{"filters": filters, "fields": fields, "order_by": order_by, "pluck": pluck}
		)
		rows = [row for row in self.delivery_notes if _matches(row, filters or {})]
		if order_by:
			field, direction = order_by.split()
			rows.sort(key=lambda row: row[field], reverse=direction.lower() == "desc")
		if pluck:
			return [row[pluck] for row in rows]
		return [_dict({field: row.get(field) for field in fields or ["name"]}) for row in rows]

	def get_doc(self, doctype, name):
		row = next(row for row in self.delivery_notes if row["name"] == name)
		return _dict(row, doctype=doctype)

	def clear_messages(self):
		self.messages_cleared += 1

	def add_dn(self, name, creation, docstatus=1, is_return=0, iid=IID, return_against=None):
		self.delivery_notes.append(
			{
				"name": name,
				"creation": creation,
				"docstatus": docstatus,
				"is_return": is_return,
				"custom_invoiced_item_id": iid,
				"return_against": return_against,
			}
		)


def _matches(row, filters):
	for field, condition in filters.items():
		value = row.get(field)
		if isinstance(condition, list | tuple):
			operator, operand = condition
			if operator == "<":
				ok = value is not None and value < operand
			elif operator == "in":
				ok = value in operand
			else:
				raise AssertionError(f"filter operator {operator!r} is not faked")
		else:
			ok = value == condition
		if not ok:
			return False
	return True


def _package(name, path=None):
	module = types.ModuleType(name)
	module.__path__ = [str(path)] if path else []
	return module


def _stubs(fake):
	utils = types.ModuleType("frappe.utils")
	utils.flt = lambda value, precision=None: float(value or 0)
	utils.get_system_timezone = lambda: "Asia/Dubai"
	utils.strip_html = lambda text: text
	utils.escape_html = lambda text: text
	fake.utils = utils

	# qty_correction imports qc_wallet (IDEV-3266 wallet overshoot), which imports assign_to. The
	# raise check never reaches the wallet: a call here is a test failure.
	def _no_assign(*args, **kwargs):
		raise AssertionError("check_correction_raise reached qc_wallet's assign_to.add")

	assign_to = types.ModuleType("frappe.desk.form.assign_to")
	assign_to.add = _no_assign
	form = _package("frappe.desk.form")
	form.assign_to = assign_to
	desk = _package("frappe.desk")
	desk.form = form
	fake.desk = desk

	accounting_period = types.ModuleType("erpnext.accounts.doctype.accounting_period.accounting_period")
	accounting_period.ClosedAccountingPeriod = type("ClosedAccountingPeriod", (Exception,), {})
	accounting_period.validate_accounting_period_on_doc_save = lambda doc: None

	parser = types.ModuleType("dateutil.parser")
	parser.isoparse = lambda text: None
	dateutil = _package("dateutil")
	dateutil.parser = parser

	return {
		"frappe": fake,
		"frappe.utils": utils,
		"frappe.desk": desk,
		"frappe.desk.form": form,
		"frappe.desk.form.assign_to": assign_to,
		"erpnext": _package("erpnext"),
		"erpnext.accounts": _package("erpnext.accounts"),
		"erpnext.accounts.doctype": _package("erpnext.accounts.doctype"),
		"erpnext.accounts.doctype.accounting_period": _package("erpnext.accounts.doctype.accounting_period"),
		"erpnext.accounts.doctype.accounting_period.accounting_period": accounting_period,
		"dateutil": dateutil,
		"dateutil.parser": parser,
		# A private copy of the app package: its submodules load against the stand-in and never
		# replace the real ones, nor become attributes of the real package.
		"fuelbuddy_crm": _package("fuelbuddy_crm", APP),
		"fuelbuddy_crm.api": _package("fuelbuddy_crm.api", APP / "api"),
	}


def load_qty_correction(fake):
	"""qty_correction executed with ``fake`` as its frappe; sys.modules is left as it was."""
	stubs = _stubs(fake)

	def ours(name):
		return name in stubs or name.startswith("fuelbuddy_crm.")

	saved = {name: module for name, module in sys.modules.items() if ours(name)}
	for name in saved:
		del sys.modules[name]
	sys.modules.update(stubs)
	try:
		return importlib.import_module("fuelbuddy_crm.api.qty_correction")
	finally:
		for name in [name for name in sys.modules if ours(name)]:
			del sys.modules[name]
		sys.modules.update(saved)


class RaiseCheckTestCase(unittest.TestCase):
	def setUp(self):
		self.frappe = FakeFrappe()
		self.qc = load_qty_correction(self.frappe)
		self.real_covering_invoices = self.qc.covering_invoices
		self.covering = {}  # DN name -> the invoices covering_invoices reports for it
		patcher = patch.object(
			self.qc, "covering_invoices", side_effect=lambda doc: set(self.covering.get(doc.name, ()))
		)
		self.covering_invoices = patcher.start()
		self.addCleanup(patcher.stop)

	def check(self, iid=IID):
		return self.qc.check_correction_raise(invoiced_item_id=iid)

	def assertAnswer(self, answer, **expected):
		shape = {
			"ok": True,
			"code": None,
			"no_dn": False,
			"delivery_notes": [],
			"invoices": [],
			"returns": [],
		}
		shape.update(expected)
		self.assertEqual({key: answer[key] for key in shape}, shape, answer)
		for key in ("delivery_notes", "invoices", "returns"):
			self.assertIsInstance(answer[key], list, key)  # a JSON array for erp-functions


class TestNoLiveDeliveryNote(RaiseCheckTestCase):
	def test_no_delivery_note_is_ok_with_no_dn(self):
		answer = self.check()
		self.assertAnswer(answer, no_dn=True)
		self.assertIsNone(answer["message"])
		self.assertEqual(self.frappe.get_all_calls[0]["filters"], LIVE_FILTERS)
		self.assertEqual(len(self.frappe.get_all_calls), 1, "nothing else to look up")

	def test_cancelled_notes_and_returns_are_not_live(self):
		self.frappe.add_dn("DN-1", "2026-08-15 10:00", docstatus=2)
		self.frappe.add_dn("DN-RET-1", "2026-08-16 10:00", is_return=1, return_against="DN-1")
		self.frappe.add_dn("DN-9", "2026-08-15 10:00", iid="another-item")
		self.assertAnswer(self.check(), no_dn=True)
		self.covering_invoices.assert_not_called()


class TestInvoiced(RaiseCheckTestCase):
	def test_a_live_note_nothing_blocks_is_ok(self):
		for docstatus in (0, 1):
			with self.subTest(docstatus=docstatus):
				self.frappe.delivery_notes.clear()
				self.frappe.add_dn("DN-1", "2026-08-15 10:00", docstatus=docstatus)
				self.assertAnswer(self.check(), delivery_notes=["DN-1"])

	def test_invoiced_names_every_covering_invoice(self):
		self.frappe.add_dn("DN-1", "2026-08-15 10:00")
		self.covering["DN-1"] = {"ACC-SINV-2026-00002", "ACC-SINV-2026-00001"}

		answer = self.check()

		self.assertAnswer(
			answer,
			ok=False,
			code="INVOICED",
			delivery_notes=["DN-1"],
			invoices=["ACC-SINV-2026-00001", "ACC-SINV-2026-00002"],
		)
		self.assertEqual(
			answer["message"],
			"Delivery Note DN-1 is covered by Sales Invoice ACC-SINV-2026-00001, ACC-SINV-2026-00002",
		)
		self.assertEqual(self.frappe.messages_cleared, 1)

	def test_every_live_note_is_checked(self):
		"""Two live notes for one item (a duplicate the plan refuses later): either one invoiced
		refuses the raise, and the message names the one that is."""
		self.frappe.add_dn("DN-2", "2026-08-15 11:00")
		self.frappe.add_dn("DN-1", "2026-08-15 10:00")
		self.covering["DN-2"] = {"ACC-SINV-2026-00007"}

		answer = self.check()

		self.assertAnswer(
			answer,
			ok=False,
			code="INVOICED",
			delivery_notes=["DN-1", "DN-2"],
			invoices=["ACC-SINV-2026-00007"],
		)
		self.assertIn("Delivery Note DN-2 is covered", answer["message"])
		self.assertEqual(
			[call.args[0].name for call in self.covering_invoices.call_args_list], ["DN-1", "DN-2"]
		)

	def test_the_invoiced_rule_is_the_amends_own(self):
		"""One function decides INVOICED at raise and at amend: dn_invoice_link.covering_invoices,
		looked up as the same module-level name by both (the patch below reaches both)."""
		real = self.real_covering_invoices
		self.assertEqual(
			(real.__module__, real.__name__), ("fuelbuddy_crm.dn_invoice_link", "covering_invoices")
		)
		self.frappe.add_dn("DN-1", "2026-08-15 10:00")
		self.covering["DN-1"] = {"ACC-SINV-2026-00001"}
		self.assertEqual(self.check()["code"], "INVOICED")
		with self.assertRaises(self.qc.Refusal) as refused:
			self.qc._check_not_invoiced(self.frappe.get_doc("Delivery Note", "DN-1"))
		self.assertEqual(refused.exception.code, "INVOICED")


class TestHasReturn(RaiseCheckTestCase):
	def test_a_submitted_return_refuses_the_raise_and_is_named(self):
		self.frappe.add_dn("DN-1", "2026-08-15 10:00")
		self.frappe.add_dn("DN-RET-2", "2026-08-17 10:00", is_return=1, return_against="DN-1")
		self.frappe.add_dn("DN-RET-1", "2026-08-16 10:00", is_return=1, return_against="DN-1")

		answer = self.check()

		self.assertAnswer(
			answer, ok=False, code="HAS_RETURN", delivery_notes=["DN-1"], returns=["DN-RET-1", "DN-RET-2"]
		)
		self.assertEqual(answer["message"], "Delivery Note DN-1 has return DN-RET-1, DN-RET-2")
		self.assertEqual(
			self.frappe.get_all_calls[1]["filters"],
			{"return_against": ["in", ["DN-1"]], "is_return": 1, "docstatus": 1},
		)

	def test_a_draft_or_cancelled_return_does_not_block(self):
		self.frappe.add_dn("DN-1", "2026-08-15 10:00")
		self.frappe.add_dn("DN-RET-1", "2026-08-16 10:00", docstatus=0, is_return=1, return_against="DN-1")
		self.frappe.add_dn("DN-RET-2", "2026-08-16 11:00", docstatus=2, is_return=1, return_against="DN-1")
		self.assertAnswer(self.check(), delivery_notes=["DN-1"])

	def test_a_return_against_another_note_does_not_block(self):
		self.frappe.add_dn("DN-1", "2026-08-15 10:00")
		self.frappe.add_dn("DN-RET-9", "2026-08-16 10:00", is_return=1, return_against="DN-9", iid="other")
		self.assertAnswer(self.check(), delivery_notes=["DN-1"])

	def test_invoiced_wins_and_both_lists_come_back(self):
		self.frappe.add_dn("DN-1", "2026-08-15 10:00")
		self.frappe.add_dn("DN-RET-1", "2026-08-16 10:00", is_return=1, return_against="DN-1")
		self.covering["DN-1"] = {"ACC-SINV-2026-00001"}
		self.assertAnswer(
			self.check(),
			ok=False,
			code="INVOICED",
			delivery_notes=["DN-1"],
			invoices=["ACC-SINV-2026-00001"],
			returns=["DN-RET-1"],
		)


class TestRefusedInput(RaiseCheckTestCase):
	def test_a_missing_invoiced_item_is_erp_validation(self):
		for iid in (None, "", "   "):
			with self.subTest(iid=iid):
				answer = self.check(iid)
				self.assertAnswer(answer, ok=False, code="ERP_VALIDATION")
				self.assertIn("invoiced_item_id is required", answer["message"])
		self.assertEqual(self.frappe.get_all_calls, [])

	def test_no_read_permission_is_erp_validation(self):
		self.frappe.permitted = False
		self.frappe.add_dn("DN-1", "2026-08-15 10:00")
		answer = self.check()
		self.assertAnswer(answer, ok=False, code="ERP_VALIDATION")
		self.assertEqual(answer["message"], "Not permitted to read Delivery Notes")
		self.assertEqual(self.frappe.get_all_calls, [])

	def test_the_id_is_trimmed(self):
		self.frappe.add_dn("DN-1", "2026-08-15 10:00")
		self.assertAnswer(self.check(f"  {IID} "), delivery_notes=["DN-1"])


class TestLoader(unittest.TestCase):
	def test_loading_leaves_the_real_modules_and_package_untouched(self):
		import fuelbuddy_crm

		watched = [
			"frappe",
			"frappe.utils",
			"erpnext",
			"dateutil",
			"fuelbuddy_crm",
			"fuelbuddy_crm.api",
			"fuelbuddy_crm.api.qty_correction",
			"fuelbuddy_crm.dn_invoice_link",
			__name__,
		]
		modules_before = {name: sys.modules.get(name) for name in watched}
		package_before = dict(vars(fuelbuddy_crm))

		loaded = load_qty_correction(FakeFrappe())

		self.assertEqual({name: sys.modules.get(name) for name in watched}, modules_before)
		self.assertEqual(dict(vars(fuelbuddy_crm)), package_before)
		self.assertIsNot(sys.modules.get("fuelbuddy_crm.api.qty_correction"), loaded)
		self.assertIsInstance(loaded.frappe, FakeFrappe)


if __name__ == "__main__":
	unittest.main()
