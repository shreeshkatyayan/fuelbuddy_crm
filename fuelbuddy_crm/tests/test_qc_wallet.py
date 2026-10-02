# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""The quantity-correction amend and the ERP wallet (IDEV-3266): never blocked, and a correction that
leaves the wallet below zero raises one Issue per episode, assigned to finance.

Pure: frappe, erpnext and dateutil are stubbed, no site needed. From the app directory:

    python -m unittest fuelbuddy_crm.tests.test_qc_wallet

The stubs are in sys.modules only while fuelbuddy_crm/qc_wallet.py and api/qty_correction.py are
loaded under them, so this also runs beside the real frappe under ``bench run-tests``.
"""

import datetime
import importlib.util
import pathlib
import sys
import types
import unittest

# qty_correction imports zoneinfo, a C extension. Imported here, once, it stays in sys.modules;
# imported first inside a stubbed load, patch.dict would drop it and the next load would import it
# again, which crashes Python 3.11 at exit.
import zoneinfo
from unittest import mock

APP = pathlib.Path(__file__).parents[1]
KEY = "0b7e2c1a-5f7d-4a8e-9c3b-2d1e4f6a7b8c-1790000000000"
CUSTOMER = "CUST-QC"
DN = "MAT-DN-2026-00042"
AMENDED_DN = "MAT-DN-2026-00042-1"
WALLET = "WAL-0001"
AMAN = "aman.prabhaker@fuelbuddy.in"
SAHAL = "sahal.shamsudheen@fuelbuddy.ae"
FUTURE = "2099-01-01T05:00:00.000Z"


class _Dict(dict):
	"""frappe._dict: attribute access; a missing key reads as None."""

	def __getattr__(self, key):
		return self.get(key)

	def __setattr__(self, key, value):
		self[key] = value


def _html_escape(text):
	table = {"&": "&amp;", '"': "&quot;", "'": "&apos;", ">": "&gt;", "<": "&lt;"}
	return "".join(table.get(c, c) for c in text) if isinstance(text, str) else text


def _stub_frappe():
	frappe = types.ModuleType("frappe")
	frappe._dict = _Dict
	frappe._ = lambda text, *args, **kwargs: text
	frappe.db = mock.MagicMock(name="frappe.db")
	frappe.db.is_deadlocked.return_value = False
	frappe.db.is_timedout.return_value = False
	for name in ("get_doc", "new_doc", "copy_doc", "log_error", "clear_messages", "get_all", "delete_doc"):
		setattr(frappe, name, mock.MagicMock(name=f"frappe.{name}"))
	frappe.get_installed_apps = mock.MagicMock(
		return_value=["frappe", "erpnext", "fuelbuddy_crm", "fuelbuddy_wallet"]
	)
	frappe.has_permission = mock.MagicMock(return_value=True)
	frappe.whitelist = lambda *args, **kwargs: lambda fn: fn

	frappe.ValidationError = type("ValidationError", (Exception,), {})
	for name in ("UniqueValidationError", "TimestampMismatchError"):
		setattr(frappe, name, type(name, (frappe.ValidationError,), {}))
	frappe.DuplicateEntryError = type("DuplicateEntryError", (NameError,), {})
	for name in ("PermissionError", "QueryDeadlockError", "QueryTimeoutError"):
		setattr(frappe, name, type(name, (Exception,), {}))

	utils = types.ModuleType("frappe.utils")
	utils.flt = lambda value, precision=None: (
		round(float(value or 0), precision) if precision is not None else float(value or 0)
	)
	utils.escape_html = _html_escape
	utils.strip_html = lambda text: text
	utils.get_system_timezone = lambda: "Asia/Dubai"

	desk = types.ModuleType("frappe.desk")
	form = types.ModuleType("frappe.desk.form")
	assign_to = types.ModuleType("frappe.desk.form.assign_to")
	assign_to.add = mock.MagicMock(name="assign_to.add")
	frappe.utils, frappe.desk, desk.form, form.assign_to = utils, desk, form, assign_to
	modules = {
		"frappe": frappe,
		"frappe.utils": utils,
		"frappe.desk": desk,
		"frappe.desk.form": form,
		"frappe.desk.form.assign_to": assign_to,
	}
	return frappe, modules


def _exec(name, path, modules):
	with mock.patch.dict(sys.modules, modules):
		spec = importlib.util.spec_from_file_location(name, path)
		module = importlib.util.module_from_spec(spec)
		sys.modules[name] = module
		spec.loader.exec_module(module)
	return module


def _load_qc_wallet():
	"""A fresh fuelbuddy_crm/qc_wallet.py bound to a fresh stub frappe."""
	frappe, modules = _stub_frappe()
	return frappe, _exec("fuelbuddy_crm.qc_wallet", APP / "qc_wallet.py", modules)


def _stub_module(name, **attrs):
	module = types.ModuleType(name)
	module.__dict__.update(attrs)
	return module


def _load_qty_correction():
	"""A fresh api/qty_correction.py, with the qc_wallet it calls, on one fresh stub frappe.

	so_allocator is the real (pure) module; dn_invoice_link, dn_validation, dn_versioning, erpnext's
	accounting period and dateutil are stubs."""
	frappe, modules = _stub_frappe()
	qc_wallet = _exec("fuelbuddy_crm.qc_wallet", APP / "qc_wallet.py", modules)
	so_allocator = _exec("fuelbuddy_crm.so_allocator", APP / "so_allocator.py", {})
	package = _stub_module(
		"fuelbuddy_crm", __path__=[str(APP)], qc_wallet=qc_wallet, so_allocator=so_allocator
	)

	period = "erpnext.accounts.doctype.accounting_period.accounting_period"
	closed = type("ClosedAccountingPeriod", (frappe.ValidationError,), {})
	parser = _stub_module(
		"dateutil.parser",
		isoparse=lambda text: datetime.datetime.fromisoformat(text.replace("Z", "+00:00")),
	)
	modules.update(
		{
			"fuelbuddy_crm": package,
			"fuelbuddy_crm.qc_wallet": qc_wallet,
			"fuelbuddy_crm.so_allocator": so_allocator,
			"fuelbuddy_crm.dn_invoice_link": _stub_module(
				"fuelbuddy_crm.dn_invoice_link",
				LINK_FIELD="custom_sales_invoice",
				QTY_FIELD="custom_sales_invoice_qty",
				covering_invoices=mock.MagicMock(return_value=set()),
				window_invoices=mock.MagicMock(return_value=[]),
			),
			"fuelbuddy_crm.dn_validation": _stub_module(
				"fuelbuddy_crm.dn_validation",
				qty_by_so_line=mock.MagicMock(return_value={}),
				so_headroom_shortfalls=mock.MagicMock(return_value=[]),
			),
			"fuelbuddy_crm.dn_versioning": _stub_module(
				"fuelbuddy_crm.dn_versioning", QC_IDEMPOTENCY_KEY_FIELD="custom_qc_idempotency_key"
			),
			"erpnext": _stub_module("erpnext"),
			"erpnext.accounts": _stub_module("erpnext.accounts"),
			"erpnext.accounts.doctype": _stub_module("erpnext.accounts.doctype"),
			"erpnext.accounts.doctype.accounting_period": _stub_module(
				"erpnext.accounts.doctype.accounting_period"
			),
			period: _stub_module(
				period,
				ClosedAccountingPeriod=closed,
				validate_accounting_period_on_doc_save=mock.MagicMock(),
			),
			"dateutil": _stub_module("dateutil", parser=parser),
			"dateutil.parser": parser,
		}
	)
	qty_correction = _exec("fuelbuddy_crm.api.qty_correction", APP / "api" / "qty_correction.py", modules)
	return frappe, qc_wallet, qty_correction


class FakeIssue:
	def __init__(self, data):
		self.data = data
		self.name = None

	def insert(self, ignore_permissions=False):
		self.name = "ISS-2026-00077"
		self.ignore_permissions = ignore_permissions
		return self


def _correction(**overrides):
	fields = {
		"customer": CUSTOMER,
		"delivery_note": DN,
		"result": "AMENDED",
		"new_delivery_note": AMENDED_DN,
		"from_qty": 1000.0,
		"target_qty": 1200.0,
	}
	fields.update(overrides)
	return _Dict(fields)


# ---- qc_wallet -----------------------------------------------------------------------------------
class QcWalletTestCase(unittest.TestCase):
	"""Customer CUST-QC; its wallet WAL-0001 reads -150.50 after the change unless a test says
	otherwise. Both assignees have enabled ERP users."""

	def setUp(self):
		self.frappe, self.qc_wallet = _load_qc_wallet()
		self.db = self.frappe.db
		self.db.get_single_value.return_value = 1  # Fuelbuddy Settings.enable_wallet
		self.db.exists.return_value = True  # Issue Type "Error Log"
		self.wallet_row = _Dict(name=WALLET, amount_remaining=-150.5)
		self.existing_issue = None
		self.users = {AMAN: AMAN, SAHAL: SAHAL}
		self.lookups = []
		self.db.get_value.side_effect = self._get_value
		self.issues = []
		self.frappe.get_doc.side_effect = self._get_doc
		self.assign = self.frappe.desk.form.assign_to.add

	def _get_value(self, doctype, filters=None, fieldname=None, *args, **kwargs):
		self.lookups.append((doctype, filters, fieldname))
		if doctype == "Wallet":
			return self.wallet_row
		if doctype == "Issue":
			return self.existing_issue
		if doctype == "User":
			return self.users.get(filters["email"])
		raise AssertionError(f"unexpected get_value on {doctype}")

	def _get_doc(self, data):
		self.issues.append(FakeIssue(data))
		return self.issues[-1]

	def after_amend(self, **overrides):
		return self.qc_wallet.after_amend(KEY, _correction(**overrides))

	def issue(self):
		self.assertEqual(len(self.issues), 1, "exactly one Issue")
		return self.issues[0]


class TestContract(QcWalletTestCase):
	def test_flag_name_is_the_one_fuelbuddy_wallet_reads(self):
		self.assertEqual(self.qc_wallet.QC_AMEND_FLAG, "fb_qc_amend")

	def test_assignees_are_the_two_named_by_the_owners(self):
		self.assertEqual(self.qc_wallet.ASSIGNEES, (AMAN, SAHAL))

	def test_subject_carries_the_episode_key_and_is_never_translated(self):
		with mock.patch.object(self.qc_wallet, "_", lambda text, *a, **k: "TRANSLATED " + text):
			subject = self.qc_wallet.issue_subject(KEY)
		self.assertEqual(subject, f"Wallet below zero after quantity correction {KEY}")
		self.assertLessEqual(len(subject), 140, "Issue.subject is a Data field")


class TestWalletAfterAmend(QcWalletTestCase):
	def read(self):
		return self.qc_wallet.wallet_after_amend(CUSTOMER)

	def test_reads_amount_remaining_of_the_customers_wallet(self):
		wallet = self.read()
		self.assertEqual((wallet.name, wallet.balance, wallet.below_zero), (WALLET, -150.5, True))
		self.assertIn(
			("Wallet", {"customer": CUSTOMER, "payment_type": "Wallet"}, ["name", "amount_remaining"]),
			self.lookups,
		)
		self.db.get_single_value.assert_called_once_with("Fuelbuddy Settings", "enable_wallet")

	def test_zero_and_positive_are_not_below_zero(self):
		for remaining in (0.0, 0.004, 25.0, None):
			with self.subTest(remaining=remaining):
				self.wallet_row.amount_remaining = remaining
				self.assertFalse(self.read().below_zero)

	def test_below_zero_is_judged_to_the_fils(self):
		self.wallet_row.amount_remaining = -0.004  # float dust
		self.assertFalse(self.read().below_zero)
		self.wallet_row.amount_remaining = -0.01
		self.assertTrue(self.read().below_zero)

	def test_no_wallet_app_no_wallet(self):
		self.frappe.get_installed_apps.return_value = ["frappe", "erpnext", "fuelbuddy_crm"]
		self.assertIsNone(self.read())
		self.db.get_value.assert_not_called()

	def test_wallet_switched_off_no_wallet(self):
		self.db.get_single_value.return_value = 0  # hooks do nothing, amount_remaining is not kept up
		self.assertIsNone(self.read())
		self.db.get_value.assert_not_called()

	def test_customer_without_a_wallet(self):
		self.wallet_row = None
		self.assertIsNone(self.read())

	def test_no_customer(self):
		self.assertIsNone(self.qc_wallet.wallet_after_amend(None))


class TestAfterAmend(QcWalletTestCase):
	def test_wallet_not_below_zero_raises_nothing(self):
		self.wallet_row.amount_remaining = 10.0
		self.assertIs(self.after_amend(), False)
		self.frappe.get_doc.assert_not_called()
		self.db.savepoint.assert_not_called()

	def test_no_wallet_raises_nothing(self):
		self.db.get_single_value.return_value = 0
		self.assertIs(self.after_amend(), False)
		self.frappe.get_doc.assert_not_called()

	def test_below_zero_raises_one_issue_for_the_customer(self):
		self.assertIs(self.after_amend(), True)
		issue = self.issue()
		self.assertEqual(issue.data["doctype"], "Issue")
		self.assertEqual(issue.data["subject"], f"Wallet below zero after quantity correction {KEY}")
		self.assertEqual(issue.data["customer"], CUSTOMER)
		self.assertEqual(issue.data["issue_type"], "Error Log")
		self.assertTrue(issue.ignore_permissions)
		self.db.savepoint.assert_any_call("fb_qc_wallet_issue")
		self.db.release_savepoint.assert_any_call("fb_qc_wallet_issue")
		self.db.rollback.assert_not_called()

	def test_description_names_the_note_customer_correction_and_balance(self):
		self.after_amend()
		description = self.issue().data["description"]
		for text in (KEY, CUSTOMER, DN, AMENDED_DN, "1,000 L to 1,200 L", WALLET, "-150.50"):
			self.assertIn(text, description)
		self.assertNotIn("Not assigned", description)

	def test_description_says_what_happened_to_the_delivery_note(self):
		cases = {
			"AMENDED": f"cancelled and reissued as {AMENDED_DN}",
			"CANCELLED": "cancelled: corrected to zero",
			"DRAFT_DELETED": "draft deleted: corrected to zero",
			"DRAFT_UPDATED": "draft updated in place",
		}
		for result, text in cases.items():
			with self.subTest(result):
				self.issues.clear()
				self.after_amend(result=result, target_qty=0.0 if "zero" in text else 800.0)
				self.assertIn(f"{DN}, {text}", self.issue().data["description"])

	def test_description_escapes_html(self):
		self.after_amend(customer="A&B <Trading>")
		description = self.issue().data["description"]
		self.assertIn("A&amp;B &lt;Trading&gt;", description)
		self.assertNotIn("<Trading>", description)

	def test_litres_are_shown_without_trailing_zeros(self):
		litres = self.qc_wallet._litres
		self.assertEqual(
			(litres(1000.0), litres(1234.5678), litres(0), litres(None)), ("1,000", "1,234.568", "0", "0")
		)

	def test_assigned_to_both_users_by_email(self):
		self.after_amend()
		self.assertEqual([c.args[0]["assign_to"] for c in self.assign.call_args_list], [[AMAN], [SAHAL]])
		for call in self.assign.call_args_list:
			self.assertEqual(call.args[0]["doctype"], "Issue")
			self.assertEqual(call.args[0]["name"], "ISS-2026-00077")
			self.assertIs(call.kwargs["ignore_permissions"], True)
		user_lookups = [f for d, f, _ in self.lookups if d == "User"]
		self.assertEqual(user_lookups, [{"email": AMAN, "enabled": 1}, {"email": SAHAL, "enabled": 1}])

	def test_a_missing_user_is_left_out_and_named(self):
		del self.users[SAHAL]
		self.assertIs(self.after_amend(), True)
		self.assertEqual([c.args[0]["assign_to"] for c in self.assign.call_args_list], [[AMAN]])
		self.assertIn(
			f"Not assigned to {SAHAL}: no enabled ERP user has this email.", self.issue().data["description"]
		)

	def test_no_user_at_all_still_raises_the_issue(self):
		self.users.clear()
		self.assertIs(self.after_amend(), True)
		self.assign.assert_not_called()
		description = self.issue().data["description"]
		self.assertIn(f"Not assigned to {AMAN}", description)
		self.assertIn(f"Not assigned to {SAHAL}", description)

	def test_an_issue_already_raised_for_the_episode_is_not_raised_again(self):
		self.existing_issue = "ISS-2026-00012"  # any status: closed ones count too
		self.assertIs(self.after_amend(), True)
		self.assertEqual(self.issues, [])
		self.assign.assert_not_called()
		self.assertIn(
			("Issue", {"subject": f"Wallet below zero after quantity correction {KEY}"}, "name"), self.lookups
		)

	def test_the_issue_type_is_created_where_missing(self):
		self.db.exists.return_value = False
		issue_type = mock.MagicMock()
		self.frappe.new_doc.return_value = issue_type
		self.after_amend()
		self.db.exists.assert_called_with("Issue Type", "Error Log")
		self.frappe.new_doc.assert_called_once_with("Issue Type")
		self.assertEqual(issue_type.name, "Error Log")
		issue_type.insert.assert_called_once_with(ignore_permissions=True)

	def test_the_issue_type_is_reused_where_present(self):
		self.after_amend()
		self.frappe.new_doc.assert_not_called()


class TestAfterAmendNeverRefuses(QcWalletTestCase):
	def test_an_issue_that_cannot_be_raised_is_rolled_back_alone(self):
		self.frappe.get_doc.side_effect = self.frappe.ValidationError("Issue Type is mandatory")
		self.assertIs(self.after_amend(), True)
		self.db.rollback.assert_called_once_with(save_point="fb_qc_wallet_issue")
		self.assign.assert_not_called()
		self.frappe.log_error.assert_called_once()
		self.assertEqual(self.frappe.log_error.call_args.kwargs["reference_name"], DN)

	def test_a_failed_assignment_leaves_the_issue_and_the_other_assignee(self):
		self.assign.side_effect = [self.frappe.ValidationError("document sharing is disabled"), None]
		self.assertIs(self.after_amend(), True)
		self.assertEqual(self.assign.call_count, 2)
		self.db.rollback.assert_called_once_with(save_point="fb_qc_wallet_assign")
		self.db.release_savepoint.assert_any_call("fb_qc_wallet_assign")
		self.db.release_savepoint.assert_any_call("fb_qc_wallet_issue")
		self.assertEqual(self.frappe.log_error.call_args.kwargs["reference_name"], "ISS-2026-00077")

	def test_a_wallet_that_cannot_be_read_counts_as_not_below_zero(self):
		self.db.get_value.side_effect = RuntimeError("Table 'tabWallet' doesn't exist")
		self.assertIs(self.after_amend(), False)
		self.frappe.get_doc.assert_not_called()
		self.frappe.log_error.assert_called_once()

	def test_lock_errors_propagate_for_the_amend_to_retry(self):
		for where in ("read", "issue", "assign"):
			for name in ("QueryDeadlockError", "QueryTimeoutError"):
				with self.subTest(where=where, error=name):
					self.setUp()  # a fresh stub frappe per case
					error = getattr(self.frappe, name)
					if where == "read":
						self.db.get_value.side_effect = error("lock")
					elif where == "issue":
						self.frappe.get_doc.side_effect = error("lock")
					else:
						self.assign.side_effect = error("lock")
					with self.assertRaises(error):
						self.after_amend()
					self.db.rollback.assert_not_called()  # the savepoint may be gone with the deadlock
					self.frappe.log_error.assert_not_called()

	def test_a_driver_level_deadlock_propagates_too(self):
		self.db.is_deadlocked.return_value = True
		self.frappe.get_doc.side_effect = RuntimeError("(1213, 'Deadlock found')")
		with self.assertRaises(RuntimeError):
			self.after_amend()
		self.db.rollback.assert_not_called()


# ---- amend_delivery_note wiring --------------------------------------------------------------------
class Row(_Dict):
	"""A child row: attribute and .get access, like a Frappe child Document."""


class FakeDN:
	"""A Delivery Note that records the flags it carried at each save / insert / submit / cancel."""

	def __init__(self, name, docstatus=0, items=(), **fields):
		self.name = name
		self.doctype = "Delivery Note"
		self.docstatus = docstatus
		self.customer = CUSTOMER
		self.posting_date = "2026-08-15"
		self.posting_time = "10:30:00"
		self.grand_total = 105.0
		self.items = [Row(item) for item in items]
		self.flags = _Dict()
		self.calls = []
		self.__dict__.update(fields)

	def get(self, key, default=None):
		return getattr(self, key, default)

	def set(self, key, value):
		setattr(self, key, value)

	def append(self, key, value):
		row = value if isinstance(value, Row) else Row(value)
		getattr(self, key).append(row)
		return row

	def get_all_children(self):
		return list(self.items)

	def _record(self, method):
		self.calls.append((method, dict(self.flags)))
		return self

	def save(self):
		return self._record("save")

	def insert(self, **kwargs):
		return self._record("insert")

	def submit(self):
		self.docstatus = 1
		return self._record("submit")

	def cancel(self):
		self.docstatus = 2
		return self._record("cancel")


def _line(name, qty, so_detail="SOI-1", **extra):
	return {"name": name, "qty": qty, "uom": "Litre", "conversion_factor": 1, "so_detail": so_detail, **extra}


class QtyCorrectionTestCase(unittest.TestCase):
	def setUp(self):
		self.frappe, self.qc_wallet, self.qc = _load_qty_correction()

	def patch(self, **replacements):
		patcher = mock.patch.multiple(self.qc, **replacements)
		patched = patcher.start()
		self.addCleanup(patcher.stop)
		return patched


class TestTheFlag(QtyCorrectionTestCase):
	"""The Delivery Note ERP validates carries the flag when it is saved, inserted and submitted."""

	def test_a_draft_is_saved_with_the_flag(self):
		dn = FakeDN(DN, docstatus=0, items=[_line("row-1", 1000)])
		self.patch(
			_check_not_invoiced=mock.DEFAULT,
			_refuse_on_headroom=mock.DEFAULT,
			_reshape=mock.MagicMock(return_value=[_line("row-1", 800)]),
		)
		result = self.qc._amend_draft(dn, 800.0, KEY, {"SOI-1"})
		self.assertEqual(result["result"], "DRAFT_UPDATED")
		self.assertEqual(dn.calls, [("save", {"fb_qc_amend": True})])

	def test_the_amendment_is_inserted_and_submitted_with_the_flag(self):
		dn = FakeDN(DN, docstatus=1, items=[_line("row-1", 1000)])
		amendment = FakeDN(AMENDED_DN, docstatus=1, items=[_line("row-1", 1000)])
		self.frappe.copy_doc.return_value = amendment
		self.patch(
			_check_not_invoiced=mock.DEFAULT,
			_refuse_on_headroom=mock.DEFAULT,
			_reshape=mock.MagicMock(return_value=[_line("row-1", 1200)]),
		)
		result = self.qc._amend_submitted(dn, 1200.0, KEY, {"SOI-1"})
		self.assertEqual((result["result"], result["new_delivery_note"]), ("AMENDED", AMENDED_DN))
		self.assertEqual([method for method, _ in amendment.calls], ["insert", "submit"])
		for method, flags in amendment.calls:
			with self.subTest(method):
				self.assertIs(flags.get("fb_qc_amend"), True)
		self.assertEqual(amendment.flags.qc_idempotency_key, KEY)  # the existing flags are kept

	def test_the_flag_is_the_one_qc_wallet_declares(self):
		self.assertEqual(self.qc.qc_wallet.QC_AMEND_FLAG, "fb_qc_amend")


class TestAmendRecordsTheWallet(QtyCorrectionTestCase):
	"""_amend runs the wallet step after the change and before the log, and both say the answer."""

	def setUp(self):
		super().setUp()
		self.events = []
		self.dn = FakeDN(
			DN,
			docstatus=1,
			items=[_line("row-1", 600), _line("row-2", 90.9, uom="IG", conversion_factor=4.4)],
		)
		self.logs = []
		self.frappe.db.get_value.return_value = _Dict(docstatus=1, is_return=0)
		self.frappe.get_doc.side_effect = self._get_doc
		self.after_amend = mock.MagicMock(side_effect=lambda key, c: self.events.append("wallet") or True)
		mock.patch.object(self.qc.qc_wallet, "after_amend", self.after_amend).start()
		self.addCleanup(mock.patch.stopall)
		self.patch(
			_lock_so_lines=mock.DEFAULT,
			_read_log=mock.MagicMock(return_value=None),
			_check_locked=mock.DEFAULT,
			_check_sales_orders_open=mock.DEFAULT,
			_check_deadline=mock.MagicMock(side_effect=lambda cutoff: self.events.append("deadline")),
			_amend_submitted=mock.MagicMock(side_effect=self._amended),
		)

	def _get_doc(self, *args, **kwargs):
		if args == ("Delivery Note", DN):
			return self.dn
		log = mock.MagicMock(name="log")
		log.insert.side_effect = lambda **kw: self.events.append("log")
		self.logs.append(args[0])
		return log

	def _amended(self, doc, target, key, so_lines):
		self.events.append("amend")
		return self.qc._ok("AMENDED", "amended", AMENDED_DN, 2, 1260.0)

	def test_the_answer_and_the_log_say_the_wallet_went_below_zero(self):
		result = self.qc._amend(DN, 1200.0, KEY, datetime.datetime(2099, 1, 1), {"SOI-1"})
		self.assertIs(result["wallet_below_zero"], True)
		self.assertEqual(len(self.logs), 1)
		self.assertEqual(self.logs[0]["wallet_below_zero"], 1)
		self.assertEqual(self.logs[0]["idempotency_key"], KEY)

	def test_the_wallet_step_follows_the_change_and_precedes_the_log(self):
		self.qc._amend(DN, 1200.0, KEY, datetime.datetime(2099, 1, 1), {"SOI-1"})
		# the first deadline check is before the change; the last one stays just before the commit
		self.assertEqual(self.events, ["deadline", "amend", "wallet", "log", "deadline"])

	def test_the_wallet_step_gets_the_correction(self):
		self.qc._amend(DN, 1200.0, KEY, datetime.datetime(2099, 1, 1), {"SOI-1"})
		key, correction = self.after_amend.call_args.args
		self.assertEqual(key, KEY)
		self.assertEqual(
			(correction.customer, correction.delivery_note, correction.result, correction.new_delivery_note),
			(CUSTOMER, DN, "AMENDED", AMENDED_DN),
		)
		self.assertAlmostEqual(correction.from_qty, 600 + 90.9 * 4.4)  # litres, IG lines converted
		self.assertEqual(correction.target_qty, 1200.0)

	def test_not_below_zero_is_logged_as_zero(self):
		self.after_amend.side_effect = None
		self.after_amend.return_value = False
		result = self.qc._amend(DN, 1200.0, KEY, datetime.datetime(2099, 1, 1), {"SOI-1"})
		self.assertIs(result["wallet_below_zero"], False)
		self.assertEqual(self.logs[0]["wallet_below_zero"], 0)

	def test_an_episode_already_applied_answers_from_its_log_without_the_wallet_step(self):
		self.qc._read_log.return_value = _Dict(
			result="AMENDED",
			new_delivery_note=AMENDED_DN,
			custom_version=2,
			grand_total=1260.0,
			wallet_below_zero=1,
		)
		result = self.qc._amend(DN, 1200.0, KEY, datetime.datetime(2099, 1, 1), {"SOI-1"})
		self.assertIs(result["wallet_below_zero"], True)
		self.after_amend.assert_not_called()
		self.assertEqual(self.logs, [])


class TestAnswers(QtyCorrectionTestCase):
	def amend(self):
		return self.qc.amend_delivery_note(DN, "1200", KEY, FUTURE)

	def test_a_retry_answered_from_the_log_says_wallet_below_zero(self):
		for stored, expected in ((1, True), (0, False)):
			with self.subTest(stored=stored):
				read_log = mock.MagicMock(
					return_value=_Dict(
						result="AMENDED",
						new_delivery_note=AMENDED_DN,
						custom_version=2,
						grand_total=1260.0,
						wallet_below_zero=stored,
					)
				)
				with (
					mock.patch.object(self.qc, "_read_log", read_log),
					mock.patch.object(self.qc.qc_wallet, "after_amend") as after_amend,
				):
					result = self.amend()
				self.assertTrue(result["ok"], result)
				self.assertIs(result["wallet_below_zero"], expected)
				after_amend.assert_not_called()  # the Issue was raised with the change, never again

	def test_a_log_row_without_the_column_reads_as_not_below_zero(self):
		logged = _Dict(result="AMENDED", new_delivery_note=AMENDED_DN, custom_version=2, grand_total=1.0)
		self.assertIs(self.qc._logged(logged)["wallet_below_zero"], False)

	def test_a_refusal_says_nothing_about_the_wallet(self):
		self.frappe.db.sql.return_value = [[datetime.datetime(2026, 9, 30, 8, 0)]]  # _db_now
		self.patch(
			_read_log=mock.MagicMock(return_value=None),
			_so_lines_to_lock=mock.MagicMock(return_value=set()),
			_amend=mock.MagicMock(side_effect=self.frappe.QueryDeadlockError("deadlock")),
		)
		result = self.amend()
		self.assertEqual((result["ok"], result["code"], result["retryable"]), (False, "LOCK_RETRY", True))
		self.assertIsNone(result["wallet_below_zero"])

	def test_ok_defaults_to_not_below_zero(self):
		self.assertIs(self.qc._ok("DRAFT_DELETED", "deleted")["wallet_below_zero"], False)

	def test_the_log_is_read_with_the_wallet_column(self):
		self.frappe.db.sql.return_value = [_Dict(result="AMENDED", wallet_below_zero=1)]
		self.qc._read_log(KEY, for_update=True)
		query = self.frappe.db.sql.call_args.args[0]
		self.assertIn("wallet_below_zero", query)
		self.assertIn("for update", query)

	def test_the_plan_reports_the_logged_wallet_answer(self):
		self.patch(
			_read_log=mock.MagicMock(
				return_value=_Dict(
					result="AMENDED", new_delivery_note=AMENDED_DN, custom_version=2, wallet_below_zero=1
				)
			)
		)
		self.frappe.get_all.return_value = []  # the live DN is not looked at for this
		plan = self.qc.get_amendment_plan("ii-1", KEY, 1200)
		self.assertTrue(plan["ok"], plan)
		self.assertEqual(
			plan["amend_log"],
			{
				"result": "AMENDED",
				"new_delivery_note": AMENDED_DN,
				"custom_version": 2,
				"wallet_below_zero": True,
			},
		)


if __name__ == "__main__":
	unittest.main()
