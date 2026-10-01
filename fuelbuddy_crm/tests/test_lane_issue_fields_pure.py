# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""The stock lane's Issue fields, Issue Types and SLAs, and the lane-owned DN flag, without a site (IDEV-3201).

Runs as plain Python from the app root (``python3 -m unittest discover -s fuelbuddy_crm/tests -p
"test_*_pure.py"``) and under ``bench run-tests``. The patch is loaded against a small fake frappe
that records the SQL it sends and the records it inserts; it is not MariaDB or ERPNext. The fake
keeps the one ERPNext rule the patch's order depends on: an enabled Issue SLA can't be saved while
SLA tracking is off. test_lane_issue_fields checks the rest on a site.
"""

import contextlib
import importlib.util
import io
import itertools
import json
import os
import re
import sys
import types
import unittest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATCH = os.path.join(APP, "patches", "add_lane_issue_fields.py")
LANE = os.path.join(APP, "lane_issue.py")
PATCHES_TXT = os.path.join(APP, "patches.txt")
MODULES_TXT = os.path.join(APP, "modules.txt")
CHILD_JSON = os.path.join(APP, "fuelbuddy_crm", "doctype", "lane_held_change", "lane_held_change.json")

ISSUE, DN = "tabIssue", "tabDelivery Note"
KEY = "custom_app_issue_key"
ISSUE_COLUMNS = (
	"custom_lane_team",
	"custom_app_issue_key",
	"custom_app_row_kind",
	"custom_app_row_id",
	"custom_held_count",
)
LANE_TYPES = ("Lane Approval Pending", "Lane ERP Refusal", "Lane Past Target", "Lane Check Mismatch")
SERVER_LOCK_WAIT = 86400
_SEQ = itertools.count()


class QueryTimeoutError(Exception):
	pass


class ValidationError(Exception):
	pass


class Flags(dict):
	__getattr__ = dict.get

	def __setattr__(self, key, value):
		self[key] = value


class Table:
	def __init__(self, columns=(), index=None):
		self.columns = set(columns)
		self.index = index  # None, "unique", or "plain" (a non-unique index named after the key)


class Records:
	"""Just enough of ERPNext's records for the patch: exists, get_all, insert, singles."""

	def __init__(self, site, tracking=False, existing=None):
		self.site = site
		self.docs = {}  # doctype -> {name: doc dict}
		self.singles = {("Support Settings", "track_service_level_agreement"): int(tracking)}
		self.inserted = []  # (doctype, name)
		self.single_writes = []
		for doctype, doc in existing or ():
			self.docs.setdefault(doctype, {})[self.name_of(doctype, doc)] = dict(doc)

	@staticmethod
	def name_of(doctype, doc):
		if doctype == "Service Level Agreement":
			return f"SLA-{doc['document_type']}-{doc['service_level']}"
		if doctype == "Holiday List":
			return doc["holiday_list_name"]
		return doc["name"]

	def matches(self, doc, filters):
		return all(doc.get(key) == value for key, value in filters.items())

	def exists(self, doctype, name_or_filters):
		docs = self.docs.get(doctype, {})
		if isinstance(name_or_filters, dict):
			return next((name for name, doc in docs.items() if self.matches(doc, name_or_filters)), None)
		return name_or_filters if name_or_filters in docs else None

	def get_all(self, doctype, filters=None, fields=None, order_by=None, **kwargs):
		rows = [
			Flags({"name": name, **doc})
			for name, doc in sorted(self.docs.get(doctype, {}).items())
			if self.matches(doc, filters or {})
		]
		return rows

	def insert(self, doctype, doc):
		if doctype == "Service Level Agreement":
			# ERPNext's ServiceLevelAgreement.validate_doc and check_priorities.
			tracking = self.singles[("Support Settings", "track_service_level_agreement")]
			if doc.get("enabled") and doc["document_type"] == "Issue" and not tracking:
				raise ValidationError("Track Service Level Agreement is not enabled in Support Settings")
			for row in doc["priorities"]:
				assert row["response_time"] <= row["resolution_time"]
		name = self.name_of(doctype, doc)
		assert name not in self.docs.get(doctype, {}), f"duplicate {doctype} {name}"
		self.docs.setdefault(doctype, {})[name] = dict(doc)
		self.inserted.append((doctype, name))


class FakeDB:
	VARCHAR_LEN = 140

	def __init__(self, tables, records, db_type="mariadb", fail_on=None):
		self.tables = tables
		self.records = records
		self.db_type = db_type
		self.fail_on = fail_on  # (table, statement body start): that DDL times out on the lock
		self.lock_wait = SERVER_LOCK_WAIT
		self.ddl = []  # (table, statement, lock wait in force)

	def sql(self, query, values=None, *args, **kwargs):
		q = " ".join(query.split()).lower()
		if q == "select @@session.lock_wait_timeout":
			return ((self.lock_wait,),)
		if q.startswith("set session lock_wait_timeout = %s"):
			self.lock_wait = values[0] if isinstance(values, tuple | list) else values
			return ()
		if "from information_schema.columns" in q:
			table, column = values
			return ((1,),) if column in self.tables[table].columns else ()
		if "from information_schema.statistics" in q:
			table, column = values
			return ((column,),) if column == KEY and self.tables[table].index == "unique" else ()
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
			assert body.endswith(", algorithm=instant"), body
			for name in re.findall(r"add column if not exists `([^`]+)`", body):
				table.columns.add(name)
		elif body.startswith("add unique index if not exists"):
			(name,) = re.findall(r"add unique index if not exists `([^`]+)`", body)
			assert name in table.columns, "an index on a column that is not there"
			if table.index is None:
				table.index = "unique"
		else:
			raise AssertionError(f"unexpected ddl: {statement}")

	def exists(self, doctype, name_or_filters):
		return self.records.exists(doctype, name_or_filters)

	def get_single_value(self, doctype, field):
		return self.records.singles.get((doctype, field))

	def set_single_value(self, doctype, field, value):
		self.records.singles[(doctype, field)] = value
		self.records.single_writes.append((doctype, field, value))


class FakeDoc:
	def __init__(self, records, doc):
		self.records = records
		self.doc = dict(doc)

	def insert(self, ignore_permissions=False):
		assert ignore_permissions
		doctype = self.doc.pop("doctype")
		self.records.insert(doctype, self.doc)
		return self


class Site:
	"""A fake frappe and the patch loaded against it."""

	def __init__(self, tables=None, db_type="mariadb", fail_on=None, tracking=False, existing=None):
		self.tables = tables if tables is not None else {ISSUE: Table(), DN: Table()}
		self.records = Records(self, tracking=tracking, existing=existing)
		self.db = FakeDB(self.tables, self.records, db_type=db_type, fail_on=fail_on)
		self.frappe = types.ModuleType("frappe")
		self.frappe.db = self.db
		self.frappe.flags = Flags()
		self.frappe.QueryTimeoutError = QueryTimeoutError
		self.frappe.ValidationError = ValidationError
		self.cache_cleared = []
		self.frappe.cache = types.SimpleNamespace(
			hdel=lambda key, field: self.cache_cleared.append((key, field))
		)
		self.meta_cleared = []
		self.frappe.clear_cache = lambda doctype=None: self.meta_cleared.append(doctype)
		self.frappe.get_all = self.records.get_all
		self.frappe.get_doc = lambda doc: FakeDoc(self.records, doc)

		def throw(message, exc=ValidationError):
			raise exc(message)

		self.frappe.throw = throw

		self.custom_fields = {}
		self.created = []  # (doctype, df, ignore_validate, in_create_custom_fields then)
		self.bulk = []
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
		package = types.ModuleType("fuelbuddy_crm")
		package.__path__ = [APP]
		package.lane_issue = load_plain(LANE)
		fakes = {
			"frappe": self.frappe,
			"frappe.custom": types.ModuleType("frappe.custom"),
			"frappe.custom.doctype": types.ModuleType("frappe.custom.doctype"),
			"frappe.custom.doctype.custom_field": types.ModuleType("frappe.custom.doctype.custom_field"),
			"frappe.custom.doctype.custom_field.custom_field": self.custom_field,
			"fuelbuddy_crm": package,
			"fuelbuddy_crm.lane_issue": package.lane_issue,
		}
		saved = {name: sys.modules.get(name) for name in fakes}
		sys.modules.update(fakes)
		try:
			spec = importlib.util.spec_from_file_location(f"_lane_issue_pure_{next(_SEQ)}", path)
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

	def docs(self, doctype):
		return self.records.docs.get(doctype, {})

	def run(self):
		self.patch.execute()
		return self


def load_plain(path):
	spec = importlib.util.spec_from_file_location(f"_lane_issue_pure_{next(_SEQ)}", path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def other_sla(service_level, enabled=1, document_type="Issue", default=0):
	return (
		"Service Level Agreement",
		{
			"service_level": service_level,
			"document_type": document_type,
			"enabled": enabled,
			"default_service_level_agreement": default,
		},
	)


class TestColumns(unittest.TestCase):
	def test_adds_the_columns_instantly_then_the_unique_key_online(self):
		site = Site().run()

		columns = ", ".join(
			f"add column if not exists `{name}` {definition}"
			for name, definition in (
				("custom_lane_team", "varchar(140)"),
				("custom_app_issue_key", "varchar(140)"),
				("custom_app_row_kind", "varchar(140)"),
				("custom_app_row_id", "varchar(140)"),
				("custom_held_count", "int(11) not null default 0"),
			)
		)
		self.assertEqual(
			site.statements(),
			[
				f"alter table `{ISSUE}` {columns}, algorithm=instant",
				f"alter table `{ISSUE}` add unique index if not exists `{KEY}` (`{KEY}`), "
				"algorithm=inplace, lock=none",
				f"alter table `{DN}` add column if not exists `custom_app_lane_owned` int(1) not null "
				"default 0, algorithm=instant",
			],
		)
		# Never "unique" in a column add: that combination rebuilds the table.
		for statement in site.statements():
			if "add column" in statement:
				self.assertNotIn("unique", statement)
		self.assertEqual(site.tables[ISSUE].columns, set(ISSUE_COLUMNS))
		self.assertEqual(site.tables[ISSUE].index, "unique")
		self.assertEqual(site.tables[DN].columns, {"custom_app_lane_owned"})

	def test_column_definitions_are_the_ones_frappe_builds(self):
		# frappe.database.mariadb.database type_map and schema.DbColumn.get_definition (v15):
		# Data and Select are varchar(VARCHAR_LEN); Int is int(11) and Check int(1), both not null
		# default 0. A matching column gives Frappe's later table syncs nothing to change.
		expected = {
			"Data": "varchar(140)",
			"Select": "varchar(140)",
			"Int": "int(11) not null default 0",
			"Check": "int(1) not null default 0",
		}
		patch = Site().patch
		for doctype, fields in patch.CUSTOM_FIELDS.items():
			columns = {name: definition.format(140) for name, definition in patch.COLUMNS[doctype]}
			for field in fields:
				with self.subTest(field=field["fieldname"]):
					if field["fieldtype"] == "Table":
						self.assertNotIn(field["fieldname"], columns)
					else:
						self.assertEqual(columns[field["fieldname"]], expected[field["fieldtype"]])
			self.assertEqual(len(columns), sum(f["fieldtype"] != "Table" for f in fields))

	def test_every_statement_waits_at_most_the_cap_and_the_session_is_put_back(self):
		site = Site().run()

		self.assertEqual({wait for _, _, wait in site.db.ddl}, {site.patch.LOCK_WAIT_SECONDS})
		self.assertLessEqual(site.patch.LOCK_WAIT_SECONDS, 10)
		self.assertEqual(site.db.lock_wait, SERVER_LOCK_WAIT)
		self.assertEqual(site.cache_cleared.count(("table_columns", ISSUE)), 2)
		self.assertEqual(site.cache_cleared.count(("table_columns", DN)), 1)

	def test_picks_up_after_a_run_that_added_some_columns_only(self):
		site = Site(tables={ISSUE: Table(columns=("custom_lane_team", KEY)), DN: Table()}).run()

		first, second = site.statements(ISSUE)
		self.assertNotIn("custom_lane_team", first)
		self.assertNotIn(f"`{KEY}` varchar", first)
		self.assertIn("custom_app_row_kind", first)
		self.assertIn("add unique index", second)
		self.assertEqual(site.tables[ISSUE].columns, set(ISSUE_COLUMNS))

	def test_a_lock_wait_past_the_cap_fails_before_any_field_or_record(self):
		site = Site(fail_on=(ISSUE, "add unique index"))

		with self.assertRaises(QueryTimeoutError), contextlib.redirect_stdout(io.StringIO()) as out:
			site.run()

		self.assertIn("safe to re-run", out.getvalue())
		self.assertEqual(site.db.lock_wait, SERVER_LOCK_WAIT)
		self.assertEqual(site.created, [])
		self.assertEqual(site.records.inserted, [])
		self.assertEqual(site.statements(DN), [])  # stops at the first failure

		site.db.fail_on = None  # the lock is free on the next bench migrate
		site.run()

		self.assertEqual(site.tables[ISSUE].index, "unique")
		self.assertEqual(sum("add column" in s for s in site.statements(ISSUE)), 1)  # not added twice
		self.assertEqual(len(site.created), 7)
		self.assertEqual(len(site.docs("Service Level Agreement")), 4)

	def test_refuses_to_trust_an_older_index_that_only_shares_the_name(self):
		site = Site(tables={ISSUE: Table(columns=ISSUE_COLUMNS, index="plain"), DN: Table()})

		with self.assertRaises(ValidationError) as caught:
			site.run()

		self.assertIn("Drop or rename it", str(caught.exception))
		self.assertEqual(site.created, [])
		self.assertEqual(site.db.lock_wait, SERVER_LOCK_WAIT)


class TestFields(unittest.TestCase):
	def test_registers_the_fields_without_frappes_table_sync(self):
		site = Site().run()

		self.assertEqual(
			[(doctype, df["fieldname"]) for doctype, df, *_ in site.created],
			[
				("Issue", "custom_lane_team"),
				("Issue", "custom_app_issue_key"),
				("Issue", "custom_app_row_kind"),
				("Issue", "custom_app_row_id"),
				("Issue", "custom_held_count"),
				("Issue", "custom_held_changes"),
				("Delivery Note", "custom_app_lane_owned"),
			],
		)
		for _doctype, df, ignore_validate, in_create_custom_fields in site.created:
			with self.subTest(field=df["fieldname"]):
				self.assertTrue(in_create_custom_fields)  # Custom Field.on_update then skips updatedb
				self.assertTrue(ignore_validate)
				self.assertEqual(df["owner"], "Administrator")
		self.assertIsNone(site.frappe.flags.in_create_custom_fields)
		self.assertEqual(site.bulk, [])
		self.assertEqual(site.meta_cleared, ["Issue", "Delivery Note"])

	def test_each_field_is_registered_after_the_one_it_follows(self):
		# Custom Field.validate places a field by its insert_after only when that field already exists.
		site = Site().run()
		standard = {"Issue": "issue_type", "Delivery Note": "custom_invoiced_item_id"}
		seen = set()
		for doctype, df, *_ in site.created:
			with self.subTest(field=df["fieldname"]):
				self.assertTrue(df["insert_after"] == standard[doctype] or df["insert_after"] in seen)
			seen.add(df["fieldname"])

	def test_field_shapes(self):
		fields = {df["fieldname"]: df for doctype, df, *_ in Site().run().created}

		key = fields["custom_app_issue_key"]
		self.assertEqual(key["fieldtype"], "Data")
		self.assertEqual((key["unique"], key["no_copy"], key["read_only"]), (1, 1, 1))
		self.assertFalse(key.get("search_index"))  # the unique index serves the lookups

		team = fields["custom_lane_team"]
		self.assertEqual(team["fieldtype"], "Select")
		self.assertEqual(team["options"].split("\n"), ["", "Purchase", "Finance", "Tech"])

		self.assertEqual(fields["custom_app_row_kind"]["fieldtype"], "Data")
		self.assertEqual(fields["custom_app_row_id"]["fieldtype"], "Data")
		self.assertEqual(fields["custom_held_count"]["fieldtype"], "Int")
		held = fields["custom_held_changes"]
		self.assertEqual((held["fieldtype"], held["options"]), ("Table", "Lane Held Change"))

		owned = fields["custom_app_lane_owned"]
		self.assertEqual(owned["fieldtype"], "Check")
		self.assertEqual((owned["no_copy"], owned["read_only"]), (1, 1))
		self.assertFalse(owned.get("allow_on_submit"))
		# The app writes every lane field; a person never edits one in the form.
		for name, df in fields.items():
			with self.subTest(field=name):
				self.assertEqual(df.get("read_only"), 1)

	def test_other_databases_take_the_standard_path(self):
		site = Site(db_type="postgres").run()

		self.assertEqual(site.db.ddl, [])
		self.assertEqual(len(site.bulk), 1)
		fields, ignore_validate = site.bulk[0]
		self.assertEqual(sorted(fields), ["Delivery Note", "Issue"])
		self.assertTrue(ignore_validate)
		self.assertEqual(len(site.docs("Service Level Agreement")), 4)

	def test_the_held_change_child_doctype(self):
		with open(CHILD_JSON) as handle:
			doctype = json.load(handle)
		with open(MODULES_TXT) as handle:
			modules = handle.read().split("\n")

		self.assertEqual(doctype["name"], "Lane Held Change")
		self.assertEqual(doctype["istable"], 1)
		self.assertIn(doctype["module"], modules)
		self.assertEqual(doctype["permissions"], [])
		self.assertEqual(
			[(f["fieldname"], f["fieldtype"]) for f in doctype["fields"]],
			[
				("app_row_kind", "Data"),
				("app_row_id", "Data"),
				("fill_time", "Datetime"),
				("erp_document", "Data"),
			],
		)
		self.assertEqual(doctype["field_order"], [f["fieldname"] for f in doctype["fields"]])


class TestIssueTypesAndSlas(unittest.TestCase):
	def test_creates_the_issue_types_calendar_and_slas(self):
		site = Site().run()

		self.assertEqual(sorted(site.docs("Issue Type")), sorted(LANE_TYPES))
		self.assertIn("Medium", site.docs("Issue Priority"))
		holiday = site.docs("Holiday List")["FuelBuddy Lane 24x7"]
		self.assertEqual(holiday["holidays"], [])
		self.assertLess(holiday["from_date"], holiday["to_date"])
		self.assertEqual(site.records.singles[("Support Settings", "track_service_level_agreement")], 1)

		slas = site.docs("Service Level Agreement")
		self.assertEqual(sorted(slas), sorted(f"SLA-Issue-{t}" for t in LANE_TYPES))
		hours = {}
		for name, sla in slas.items():
			with self.subTest(sla=name):
				self.assertEqual(sla["document_type"], "Issue")
				self.assertEqual(sla["enabled"], 1)
				self.assertEqual(sla["default_service_level_agreement"], 0)
				self.assertEqual(sla["holiday_list"], "FuelBuddy Lane 24x7")
				self.assertEqual(sla["condition"], f"doc.issue_type == '{sla['service_level']}'")
				self.assertEqual(sla["default_priority"], "Medium")
				self.assertEqual(sla["apply_sla_for_resolution"], 1)
				(priority,) = sla["priorities"]
				self.assertEqual((priority["priority"], priority["default_priority"]), ("Medium", 1))
				self.assertLessEqual(priority["response_time"], priority["resolution_time"])
				hours[sla["service_level"]] = priority["resolution_time"] / 3600
				self.assertEqual(
					[(d["workday"], d["start_time"], d["end_time"]) for d in sla["support_and_resolution"]],
					[
						(day, "00:00:00", "23:59:59")
						for day in (
							"Monday",
							"Tuesday",
							"Wednesday",
							"Thursday",
							"Friday",
							"Saturday",
							"Sunday",
						)
					],
				)
				self.assertEqual([r["status"] for r in sla["sla_fulfilled_on"]], ["Resolved", "Closed"])
		# 6 h from capture: the Issue opens at 1 h.
		self.assertEqual(hours["Lane Approval Pending"], 5)

	def test_the_condition_picks_out_its_own_issue_type_only(self):
		lane = load_plain(LANE)
		for issue_type in LANE_TYPES:
			condition = lane.sla_condition(issue_type)
			for other in (*LANE_TYPES, None, "Delivery"):
				with self.subTest(sla=issue_type, issue=other):
					doc = types.SimpleNamespace(issue_type=other)
					self.assertEqual(eval(condition, {"doc": doc}), other == issue_type)

	def test_tracking_is_on_before_the_first_sla(self):
		# The fake refuses an enabled Issue SLA while tracking is off, as ERPNext does.
		site = Site(tracking=False).run()
		self.assertEqual(
			site.records.single_writes, [("Support Settings", "track_service_level_agreement", 1)]
		)

	def test_a_second_run_changes_nothing(self):
		site = Site().run()
		ddl, created, inserted = len(site.db.ddl), len(site.created), len(site.records.inserted)
		writes = len(site.records.single_writes)

		site.run()

		self.assertEqual(len(site.db.ddl), ddl)
		self.assertEqual(len(site.created), created)
		self.assertEqual(len(site.records.inserted), inserted)
		self.assertEqual(len(site.records.single_writes), writes)

	def test_records_already_there_are_left_as_they_are(self):
		tuned = {
			"service_level": "Lane Past Target",
			"document_type": "Issue",
			"enabled": 1,
			"default_service_level_agreement": 0,
			"note": "tuned by a person",
		}
		site = Site(
			tracking=True,
			existing=[
				("Issue Type", {"name": "Lane ERP Refusal", "description": "kept"}),
				("Issue Priority", {"name": "Medium"}),
				("Holiday List", {"holiday_list_name": "FuelBuddy Lane 24x7", "holidays": ["kept"]}),
				("Service Level Agreement", tuned),
			],
		).run()

		self.assertEqual(site.docs("Issue Type")["Lane ERP Refusal"]["description"], "kept")
		self.assertEqual(site.docs("Holiday List")["FuelBuddy Lane 24x7"]["holidays"], ["kept"])
		self.assertEqual(site.docs("Service Level Agreement")["SLA-Issue-Lane Past Target"], tuned)
		self.assertNotIn(("Issue Priority", "Medium"), site.records.inserted)
		self.assertEqual(len(site.docs("Service Level Agreement")), 4)
		self.assertEqual(site.records.single_writes, [])  # tracking was already on


class TestSlaTrackingGate(unittest.TestCase):
	def test_stops_before_any_change_when_tracking_would_start_another_sla(self):
		for default in (1, 0):
			with self.subTest(default=default):
				site = Site(tracking=False, existing=[other_sla("Standard", default=default)])

				with self.assertRaises(ValidationError) as caught:
					site.run()

				self.assertIn("SLA-Issue-Standard", str(caught.exception))
				self.assertIn("stopped before changing anything", str(caught.exception))
				self.assertEqual(site.db.ddl, [])
				self.assertEqual(site.created, [])
				self.assertEqual(site.records.inserted, [])
				self.assertEqual(site.records.single_writes, [])

	def test_goes_ahead_when_tracking_is_already_on(self):
		site = Site(tracking=True, existing=[other_sla("Standard", default=1)]).run()

		self.assertEqual(len(site.docs("Service Level Agreement")), 5)
		self.assertEqual(site.records.single_writes, [])

	def test_disabled_slas_and_other_doctypes_do_not_stop_it(self):
		site = Site(
			tracking=False,
			existing=[other_sla("Old", enabled=0), other_sla("Warranty", document_type="Warranty Claim")],
		).run()

		self.assertEqual(site.records.singles[("Support Settings", "track_service_level_agreement")], 1)
		self.assertEqual(len(site.docs("Service Level Agreement")), 6)

	def test_the_lanes_own_slas_do_not_stop_a_re_run(self):
		site = Site().run()
		site.records.singles[("Support Settings", "track_service_level_agreement")] = 0
		site.patch._check_sla_tracking()  # only the lane's SLAs are enabled: no stop


class TestPatchOrder(unittest.TestCase):
	def test_runs_last_after_the_model_sync_and_the_key_patches(self):
		with open(PATCHES_TXT) as handle:
			text = handle.read()
		post_model_sync = [
			line.strip()
			for line in text.split("[post_model_sync]", 1)[1].splitlines()
			if line.strip() and not line.strip().startswith("#")
		]
		self.assertEqual(post_model_sync[-1], "fuelbuddy_crm.patches.add_lane_issue_fields")
		# 3201's key patch is on this branch; 3266's and 3269's join in the combined release merge,
		# which must keep this patch after all three.
		self.assertIn("fuelbuddy_crm.patches.add_app_op_key", post_model_sync)
		for earlier in (
			"fuelbuddy_crm.patches.add_app_op_key",
			"fuelbuddy_crm.patches.add_dn_qc_idempotency_key",
			"fuelbuddy_crm.patches.add_dn_live_invoiced_item_key",
		):
			if earlier in post_model_sync:
				self.assertLess(
					post_model_sync.index(earlier),
					post_model_sync.index("fuelbuddy_crm.patches.add_lane_issue_fields"),
				)


if __name__ == "__main__":
	unittest.main()
