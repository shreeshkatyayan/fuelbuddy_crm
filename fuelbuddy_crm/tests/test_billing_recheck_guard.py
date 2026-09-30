# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""The runtime upgrade guard's database driver check and method-owner check (IDEV-3268).

guard.py runs here on tests/fake_frappe.py, so no site and no bench are needed:

    python -m unittest fuelbuddy_crm.tests.test_billing_recheck_guard

A real site's driver is covered by the site tests, which expect guard.mismatches() == [] there.
"""

import pathlib
import sys
import types
import unittest
from unittest import mock

from fuelbuddy_crm.billing_recheck import fingerprint as fp
from fuelbuddy_crm.tests import fake_frappe as ff

GUARD = pathlib.Path(fp.__file__).with_name("guard.py")
PINS = {"runtime": {"db_driver": {"PyMySQL": ["1.1.1"]}}}
MYSQLCLIENT = "db driver mysqlclient 2.2.7 not pinned (pinned: PyMySQL 1.1.1)"


def connection(module):
	"""A connection object whose class is defined in ``module``, as a driver's own class is."""
	return type("Connection", (), {"__module__": module})()


def load_guard(fake):
	"""guard.py with ``fake`` as its frappe. Its observe import gets a stub so that nothing imports the
	real observe here (under bench run-tests the real one, already imported, is bound instead)."""
	stub = types.ModuleType("fuelbuddy_crm.billing_recheck.observe")
	stub.warn_once = lambda key, message: None
	with mock.patch.dict(sys.modules, {"fuelbuddy_crm.billing_recheck.observe": stub}):
		return ff.load(str(GUARD), fake)


class GuardCase(unittest.TestCase):
	def setUp(self):
		self.frappe = ff.make()
		self.guard = load_guard(self.frappe)
		# the driver packages as PyMySQL 1.1.1 and mysqlclient 2.2.7 show their versions
		self.drivers = {
			"pymysql": types.SimpleNamespace(VERSION_STRING="1.1.1", __version__="1.4.6"),
			"MySQLdb": types.SimpleNamespace(version_info=(2, 2, 7, "final", 0)),
		}
		for patcher in (
			mock.patch.object(self.guard, "sys", types.SimpleNamespace(modules=self.drivers)),
			mock.patch.object(fp, "load_pins", return_value=PINS),
		):
			patcher.start()
			self.addCleanup(patcher.stop)

	def use(self, conn):
		"""This site's frappe.db, connected through ``conn``."""
		self.frappe.local.db = types.SimpleNamespace(_conn=conn)


class TestDriver(GuardCase):
	def test_pinned_driver(self):
		self.use(connection("pymysql.connections"))
		self.assertEqual(
			self.guard.driver_result(), {"driver": "PyMySQL", "version": "1.1.1", "mismatch": []}
		)

	def test_mysqlclient_is_not_pinned(self):
		self.use(connection("MySQLdb.connections"))
		self.assertEqual(
			self.guard.driver_result(),
			{"driver": "mysqlclient", "version": "2.2.7", "mismatch": [MYSQLCLIENT]},
		)

	def test_unpinned_pymysql_version(self):
		self.drivers["pymysql"].VERSION_STRING = "1.1.2"
		self.use(connection("pymysql.connections"))
		self.assertEqual(
			self.guard.driver_result()["mismatch"],
			["db driver PyMySQL 1.1.2 not pinned (pinned: PyMySQL 1.1.1)"],
		)

	def test_unknown_driver_or_no_connection(self):
		self.use(connection("psycopg2.extensions"))
		self.assertEqual(
			self.guard.driver_result()["mismatch"], ["db driver unknown: psycopg2.extensions.Connection"]
		)
		self.use(None)
		self.assertEqual(self.guard.driver_result()["mismatch"], ["db driver unknown: no connection"])
		self.guard.reset()
		self.frappe.local.pop("db")  # no frappe.db at all
		self.assertEqual(self.guard.driver_result()["mismatch"], ["db driver unknown: no connection"])

	def test_checked_once_per_connection_class(self):
		with mock.patch.object(self.guard, "_check_driver", wraps=self.guard._check_driver) as check:
			first = connection("pymysql.connections")
			self.use(first)
			self.guard.driver_result()
			self.use(type(first)())  # another connection of the same driver
			self.assertEqual(self.guard.driver_result()["mismatch"], [])
			self.assertEqual(check.call_count, 1)
			self.use(connection("MySQLdb.connections"))  # the site now connects with mysqlclient
			self.assertEqual(self.guard.driver_result()["mismatch"], [MYSQLCLIENT])
			self.assertEqual(check.call_count, 2)
			self.guard.reset()
			self.guard.driver_result()
			self.assertEqual(check.call_count, 3)

	def test_errors_mean_a_mismatch(self):
		self.use(connection("pymysql.connections"))
		with mock.patch.object(fp, "connection_driver", side_effect=RuntimeError("boom")):
			self.assertEqual(
				self.guard.driver_result(),
				{"driver": None, "version": None, "mismatch": ["error:RuntimeError('boom')"]},
			)

	def test_mismatches_include_the_driver(self):
		warned = []
		with (
			mock.patch.object(self.guard, "code_result", return_value={"mismatch": [], "versions": {}}),
			mock.patch.object(self.guard, "class_result", return_value=[]),
			mock.patch.object(
				self.guard,
				"observe",
				types.SimpleNamespace(warn_once=lambda key, message: warned.append(key)),
			),
		):
			self.use(connection("pymysql.connections"))
			self.assertEqual(self.guard.mismatches(), [])
			self.assertTrue(self.guard.ok())
			self.use(connection("MySQLdb.connections"))
			self.assertEqual(self.guard.mismatches(), [MYSQLCLIENT])
			self.assertFalse(self.guard.ok())
		self.assertEqual(warned, [f"guard:{MYSQLCLIENT}"] * 2)


class TestOwners(GuardCase):
	"""Every method the re-check relies on, line_guard's premise included, must be defined by the class
	that was hashed (an override would run code the fingerprint never saw)."""

	def stock(self):
		class StatusUpdater:
			def update_prevdoc_status(self): ...
			def update_qty(self): ...
			def _update_children(self): ...
			def _update_percent_field(self): ...
			def _update_modified(self): ...
			def set_status(self): ...

		class StockController(StatusUpdater):
			def update_billing_percentage(self): ...

		class DeliveryNote(StockController):
			def update_billing_status(self): ...
			def on_submit(self): ...
			def on_cancel(self): ...

		class SalesInvoice(StockController):
			def update_billing_status_in_dn(self): ...
			def on_submit(self): ...
			def on_cancel(self): ...

		return {cls.__name__: cls for cls in (StatusUpdater, StockController, DeliveryNote, SalesInvoice)}

	def test_line_guard_premise_is_checked(self):
		for owners in (self.guard._DN_OWNERS, self.guard._SI_OWNERS):
			with self.subTest(owners=owners):
				self.assertIn(("update_qty", "StatusUpdater"), owners)
				self.assertIn(("_update_children", "StatusUpdater"), owners)

	def test_stock_classes_pass(self):
		expected = self.stock()
		for cls, owners in (("DeliveryNote", self.guard._DN_OWNERS), ("SalesInvoice", self.guard._SI_OWNERS)):
			with self.subTest(cls=cls):
				self.assertEqual(self.guard.owner_mismatches(expected[cls], owners, expected), [])

	def test_an_override_of_the_premise_fails(self):
		expected = self.stock()

		class CustomDeliveryNote(expected["DeliveryNote"]):  # an app's extend_doctype_class, say
			def _update_children(self): ...

		class CustomSalesInvoice(expected["SalesInvoice"]):
			def update_qty(self): ...

		for cls, owners, attr in (
			(CustomDeliveryNote, self.guard._DN_OWNERS, "_update_children"),
			(CustomSalesInvoice, self.guard._SI_OWNERS, "update_qty"),
		):
			with self.subTest(cls=cls.__name__):
				name = cls.__qualname__
				self.assertEqual(
					self.guard.owner_mismatches(cls, owners, expected),
					[f"mro:{name}.{attr} defined by {name}, expected StatusUpdater"],
				)


if __name__ == "__main__":
	unittest.main()
