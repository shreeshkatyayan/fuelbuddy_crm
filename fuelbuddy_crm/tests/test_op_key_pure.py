# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""The app op key on Purchase Receipt and Stock Entry, without a site (IDEV-3201).

Runs as plain Python from the app root (``python3 -m unittest discover -s fuelbuddy_crm/tests -p
"test_*_pure.py"``) and under ``bench run-tests``. The modules under test are loaded against a small
fake frappe that records the SQL they send; it is not MariaDB. What MariaDB does with those
statements (INSTANT column add, online unique index, the metadata-lock cap) needs a site.
"""

import contextlib
import importlib.util
import io
import itertools
import os
import re
import sys
import types
import unittest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATCH = os.path.join(APP, "patches", "add_app_op_key.py")
OP_KEY = os.path.join(APP, "op_key.py")
HOOKS = os.path.join(APP, "hooks.py")
PATCHES_TXT = os.path.join(APP, "patches.txt")

FIELD = "custom_app_op_key"
PR, SE = "tabPurchase Receipt", "tabStock Entry"
SERVER_LOCK_WAIT = 86400  # MariaDB's default lock_wait_timeout, in seconds
_SEQ = itertools.count()


class QueryTimeoutError(Exception):
	pass


class ValidationError(Exception):
	pass


class Flags(dict):
	"""frappe.flags: keys read as attributes, a missing one as None."""

	__getattr__ = dict.get

	def __setattr__(self, key, value):
		self[key] = value


class Table:
	def __init__(self, column=False, index=None):
		self.column = column
		self.index = index  # None, "unique", or "plain" (a non-unique index named after the field)


class FakeDB:
	VARCHAR_LEN = 140

	def __init__(self, tables, db_type="mariadb", fail_on=None):
		self.tables = tables
		self.db_type = db_type
		self.fail_on = fail_on  # (table, statement start): that DDL times out on the metadata lock
		self.lock_wait = SERVER_LOCK_WAIT
		self.ddl = []  # (table, statement, lock_wait_timeout in force)

	def sql(self, query, values=None, *args, **kwargs):
		q = " ".join(query.split()).lower()
		if q == "select @@session.lock_wait_timeout":
			return ((self.lock_wait,),)
		if q.startswith("set session lock_wait_timeout = %s"):
			self.lock_wait = values[0] if isinstance(values, tuple | list) else values
			return ()
		if "from information_schema.columns" in q:
			table, column = values
			return ((1,),) if column == FIELD and self.tables[table].column else ()
		if "from information_schema.statistics" in q:
			table, column = values
			return ((FIELD,),) if column == FIELD and self.tables[table].index == "unique" else ()
		raise AssertionError(f"unexpected sql: {query}")

	def sql_ddl(self, query):
		statement = " ".join(query.split())
		match = re.fullmatch(r"alter table `([^`]+)` (.+)", statement)
		table_name, body = match.group(1), match.group(2)
		self.ddl.append((table_name, statement, self.lock_wait))
		if self.fail_on and self.fail_on[0] == table_name and body.startswith(self.fail_on[1]):
			raise QueryTimeoutError("(1205, 'Lock wait timeout exceeded; try restarting transaction')")
		table = self.tables[table_name]
		if body.startswith("add column if not exists"):
			table.column = True
		elif body.startswith("add unique index if not exists"):
			assert table.column, "an index on a column that is not there"
			if table.index is None:  # IF NOT EXISTS matches the name; a same-named index stays as it is
				table.index = "unique"
		else:
			raise AssertionError(f"unexpected ddl: {statement}")


class Site:
	"""A fake frappe (and its custom_field module) and the modules under test loaded against it."""

	def __init__(self, tables=None, db_type="mariadb", fail_on=None):
		self.tables = tables if tables is not None else {PR: Table(), SE: Table()}
		self.db = FakeDB(self.tables, db_type=db_type, fail_on=fail_on)
		self.frappe = types.ModuleType("frappe")
		self.frappe.db = self.db
		self.frappe.flags = Flags()
		self.frappe.QueryTimeoutError = QueryTimeoutError
		self.frappe.ValidationError = ValidationError
		self.cache_cleared = []  # table_columns entries dropped
		self.frappe.cache = types.SimpleNamespace(
			hdel=lambda key, field: self.cache_cleared.append((key, field))
		)
		self.meta_cleared = []
		self.frappe.clear_cache = lambda doctype=None: self.meta_cleared.append(doctype)

		def throw(message, exc=ValidationError):
			raise exc(message)

		self.frappe.throw = throw

		self.custom_fields = {}  # (doctype, fieldname) -> the df it was made from
		self.created = []  # (doctype, df, ignore_validate, frappe.flags.in_create_custom_fields then)
		self.bulk = []  # create_custom_fields calls
		self.custom_field = types.ModuleType("frappe.custom.doctype.custom_field.custom_field")
		self.custom_field.create_custom_field = self._create_custom_field
		self.custom_field.create_custom_fields = lambda fields, ignore_validate=False, update=True: (
			self.bulk.append((fields, ignore_validate))
		)
		self.patch = self.load(PATCH)

	def _create_custom_field(self, doctype, df, ignore_validate=False, is_system_generated=True):
		key = (doctype, df["fieldname"])
		if key in self.custom_fields:
			return None
		self.custom_fields[key] = dict(df)
		self.created.append((doctype, dict(df), ignore_validate, self.frappe.flags.in_create_custom_fields))
		return key

	def load(self, path):
		fakes = {
			"frappe": self.frappe,
			"frappe.custom": types.ModuleType("frappe.custom"),
			"frappe.custom.doctype": types.ModuleType("frappe.custom.doctype"),
			"frappe.custom.doctype.custom_field": types.ModuleType("frappe.custom.doctype.custom_field"),
			"frappe.custom.doctype.custom_field.custom_field": self.custom_field,
		}
		saved = {name: sys.modules.get(name) for name in fakes}
		sys.modules.update(fakes)
		try:
			spec = importlib.util.spec_from_file_location(f"_op_key_pure_{next(_SEQ)}", path)
			module = importlib.util.module_from_spec(spec)
			spec.loader.exec_module(module)
		finally:
			for name, value in saved.items():
				if value is None:
					sys.modules.pop(name, None)
				else:
					sys.modules[name] = value
		return module

	def statements(self, table=None):
		return [statement for name, statement, _ in self.db.ddl if table in (None, name)]


def load_plain(path):
	spec = importlib.util.spec_from_file_location(f"_op_key_pure_{next(_SEQ)}", path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


class Doc:
	"""Just enough of a Document for the hook: get and set."""

	def __init__(self, **fields):
		self.fields = dict(fields)

	def get(self, key, default=None):
		return self.fields.get(key, default)

	def set(self, key, value):
		self.fields[key] = value


class TestClearOnAmend(unittest.TestCase):
	def setUp(self):
		self.op_key = load_plain(OP_KEY)

	def test_an_amendment_starts_without_the_key(self):
		doc = Doc(amended_from="MAT-PRE-2026-00012", custom_app_op_key="erp-grn-1")
		self.op_key.clear_on_amend(doc, "before_insert")
		self.assertIsNone(doc.get(FIELD))

	def test_a_first_post_keeps_its_key(self):
		doc = Doc(amended_from=None, custom_app_op_key="erp-transfer-7")
		self.op_key.clear_on_amend(doc, "before_insert")
		self.assertEqual(doc.get(FIELD), "erp-transfer-7")

	def test_an_amendment_without_a_key_is_left_alone(self):
		doc = Doc(amended_from="MAT-STE-2026-00003")
		self.op_key.clear_on_amend(doc, "before_insert")
		self.assertNotIn(FIELD, doc.fields)

	def test_only_before_insert_is_hooked_so_cancelling_keeps_the_key(self):
		hooks = load_plain(HOOKS)
		for doctype in ("Purchase Receipt", "Stock Entry"):
			with self.subTest(doctype=doctype):
				self.assertEqual(
					hooks.doc_events[doctype], {"before_insert": "fuelbuddy_crm.op_key.clear_on_amend"}
				)
		self.assertEqual(self.op_key.OP_KEY_DOCTYPES, ("Purchase Receipt", "Stock Entry"))
		self.assertEqual(self.op_key.OP_KEY_FIELD, FIELD)


class TestAddAppOpKeyPatch(unittest.TestCase):
	def test_adds_the_column_instantly_then_the_unique_index_online(self):
		site = Site()
		site.patch.execute()

		self.assertEqual(
			site.statements(),
			[
				f"alter table `{PR}` add column if not exists `{FIELD}` varchar(140), algorithm=instant",
				f"alter table `{PR}` add unique index if not exists `{FIELD}` (`{FIELD}`), "
				"algorithm=inplace, lock=none",
				f"alter table `{SE}` add column if not exists `{FIELD}` varchar(140), algorithm=instant",
				f"alter table `{SE}` add unique index if not exists `{FIELD}` (`{FIELD}`), "
				"algorithm=inplace, lock=none",
			],
		)
		# The column alone, never "unique" in the same statement: that combination rebuilds the table.
		for statement in site.statements():
			if "add column" in statement:
				self.assertNotIn("unique", statement)
		self.assertTrue(site.tables[PR].column and site.tables[SE].column)
		self.assertEqual((site.tables[PR].index, site.tables[SE].index), ("unique", "unique"))

	def test_every_statement_waits_at_most_the_cap_and_the_session_is_put_back(self):
		site = Site()
		site.patch.execute()

		self.assertEqual({wait for _, _, wait in site.db.ddl}, {site.patch.LOCK_WAIT_SECONDS})
		self.assertLessEqual(site.patch.LOCK_WAIT_SECONDS, 10)
		self.assertEqual(site.db.lock_wait, SERVER_LOCK_WAIT)
		self.assertEqual(site.cache_cleared.count(("table_columns", PR)), 2)
		self.assertEqual(site.cache_cleared.count(("table_columns", SE)), 2)

	def test_registers_the_fields_without_frappes_table_sync(self):
		site = Site()
		site.patch.execute()

		self.assertEqual([doctype for doctype, *_ in site.created], ["Purchase Receipt", "Stock Entry"])
		for doctype, df, ignore_validate, in_create_custom_fields in site.created:
			with self.subTest(doctype=doctype):
				# Custom Field.on_update skips frappe.db.updatedb only while this flag is set.
				self.assertTrue(in_create_custom_fields)
				self.assertTrue(ignore_validate)
				self.assertEqual(df["owner"], "Administrator")
		self.assertIsNone(site.frappe.flags.in_create_custom_fields)
		self.assertEqual(site.bulk, [])  # create_custom_fields would sync the whole table
		self.assertEqual(site.meta_cleared, ["Purchase Receipt", "Stock Entry"])

	def test_the_field_is_unique_no_copy_and_read_only_data(self):
		field = Site().patch.FIELD
		self.assertEqual(field["fieldname"], FIELD)
		self.assertEqual(field["fieldtype"], "Data")
		self.assertEqual((field["unique"], field["no_copy"], field["read_only"]), (1, 1, 1))
		# The unique index serves the lookups; search_index would add a second index to the table.
		self.assertFalse(field.get("search_index"))
		self.assertFalse(field.get("allow_on_submit"))

	def test_a_second_run_changes_nothing(self):
		site = Site()
		site.patch.execute()
		ddl, created = len(site.db.ddl), len(site.created)

		site.patch.execute()

		self.assertEqual(len(site.db.ddl), ddl)
		self.assertEqual(len(site.created), created)
		self.assertEqual(site.db.lock_wait, SERVER_LOCK_WAIT)

	def test_picks_up_after_a_run_that_added_the_column_only(self):
		site = Site(tables={PR: Table(column=True), SE: Table()})
		site.patch.execute()

		(only,) = site.statements(PR)
		self.assertIn("add unique index", only)
		self.assertEqual(len(site.statements(SE)), 2)
		self.assertEqual(len(site.created), 2)

	def test_a_lock_wait_past_the_cap_fails_the_patch_before_any_field_is_registered(self):
		site = Site(fail_on=(PR, "add unique index"))

		with self.assertRaises(QueryTimeoutError), contextlib.redirect_stdout(io.StringIO()) as out:
			site.patch.execute()

		self.assertIn("safe to re-run", out.getvalue())
		self.assertEqual(site.db.lock_wait, SERVER_LOCK_WAIT)  # the session is put back regardless
		self.assertEqual(site.created, [])  # no field until both tables hold the unique index
		self.assertEqual(site.statements(SE), [])  # stops at the first failure
		self.assertTrue(site.tables[PR].column)

		site.db.fail_on = None  # the lock is free on the next bench migrate
		site.patch.execute()

		self.assertEqual((site.tables[PR].index, site.tables[SE].index), ("unique", "unique"))
		self.assertEqual(len(site.created), 2)
		self.assertEqual(
			sum("add column" in statement for statement in site.statements(PR)), 1
		)  # the column was not added twice

	def test_refuses_to_trust_an_older_index_that_only_shares_the_name(self):
		site = Site(tables={PR: Table(column=True, index="plain"), SE: Table()})

		with self.assertRaises(ValidationError) as caught:
			site.patch.execute()

		self.assertIn("Drop or rename it", str(caught.exception))
		self.assertEqual(site.created, [])
		self.assertEqual(site.db.lock_wait, SERVER_LOCK_WAIT)

	def test_other_databases_take_the_standard_path(self):
		site = Site(db_type="postgres")
		site.patch.execute()

		self.assertEqual(site.db.ddl, [])
		self.assertEqual(len(site.bulk), 1)
		fields, ignore_validate = site.bulk[0]
		self.assertEqual(sorted(fields), ["Purchase Receipt", "Stock Entry"])
		self.assertTrue(ignore_validate)

	def test_runs_after_the_model_sync(self):
		with open(PATCHES_TXT) as handle:
			text = handle.read()
		post_model_sync = text.split("[post_model_sync]", 1)[1]
		self.assertIn("fuelbuddy_crm.patches.add_app_op_key", post_model_sync.split())


if __name__ == "__main__":
	unittest.main()
