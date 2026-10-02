# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""The Delivery Note key-column step (fuelbuddy_crm.dn_key_columns), against a fake MariaDB.

FakeMariaDB keeps just what the step reads -- the key and stamp columns, the table's indexes, the
stamp's Custom Field -- and applies the three ALTER forms the step may issue the way MariaDB does:
IF NOT EXISTS on something present changes nothing, and a refused ALGORITHM, a lock-wait timeout or
a duplicate value fails having changed nothing. Any other statement fails the test, so the step can
never slip in a table rebuild. It records the session lock_wait_timeout each ALTER ran under.

TestPatches imports the app's patch modules against a stub of frappe, so it runs only where frappe
is not installed; on a site, test_dn_live_key runs the real patch.

Pure: python -m unittest fuelbuddy_crm.tests.test_dn_key_columns (from the app directory).
"""

import dataclasses
import importlib
import importlib.util
import pathlib
import re
import sys
import types
import unittest
from unittest import mock

from fuelbuddy_crm import dn_key_columns as dkc
from fuelbuddy_crm.dn_key_columns import Column, Index, KeyColumnsError

LIVE, QC = dkc.KEY_COLUMNS
STAMP = dkc.STAMP
SESSION_DEFAULT = 86400  # MariaDB's default lock_wait_timeout
STAMP_FIELD = {"name": "Delivery Note-custom_invoiced_item_id", "unique": 0, "search_index": 1}
APP = pathlib.Path(dkc.__file__).parent

_ALTER = re.escape(f"ALTER TABLE `{dkc.TABLE}` ")
# Only the no-rebuild algorithms: an ADD COLUMN with INPLACE, COPY or no ALGORITHM would rebuild the table.
ADD_COLUMN = re.compile(
	_ALTER + r"ADD COLUMN IF NOT EXISTS `(\w+)` varchar\(140\) DEFAULT NULL, ALGORITHM=(INSTANT|NOCOPY)"
)
ADD_INDEX = re.compile(
	_ALTER + r"ADD (UNIQUE )?INDEX IF NOT EXISTS `(\w+)` \(`(\w+)`\), ALGORITHM=INPLACE, LOCK=NONE"
)


class MariaDBError(Exception):
	"""Shaped like pymysql's errors: args = (errno, message)."""


class QueryTimeoutError(Exception):
	"""Shaped like Frappe's: QueryTimeoutError(<pymysql error>), raised from that error."""


def lock_wait_timeout():
	return MariaDBError(1205, "Lock wait timeout exceeded; try restarting transaction")


def varchar(name, **changes):
	return dataclasses.replace(Column(name, "varchar(140)", True, None, ""), **changes)


class FakeMariaDB:
	def __init__(self, columns=(), indexes=(), stamp_field=None):
		self.table = {column.name: column for column in columns}
		self.index = {index.name: index for index in indexes}
		self.stamp = dict(stamp_field) if stamp_field else None
		self.session_lock_wait = SESSION_DEFAULT
		self.lock_wait_set = []  # every SET SESSION lock_wait_timeout, in order
		self.alters = []  # (statement, the lock_wait_timeout it ran under), failed ones included
		self.fail = []  # [(text in the statement, exception)]: each raised once, first match first
		self.refused = set()  # ALGORITHMs MariaDB refuses for ADD COLUMN
		self.ineffective = set()  # texts of statements that succeed but change nothing
		self.duplicated = set()  # columns holding one value on two rows

	# ---- what ensure() calls ----------------------------------------------------------------------
	def stamp_field(self):
		return dict(self.stamp) if self.stamp else None

	def flag_stamp_search_index(self, name):
		assert name == self.stamp["name"], name
		self.stamp["search_index"] = 1

	def columns(self, names):
		return {
			name: dataclasses.replace(column, key=self._key(name))
			for name, column in self.table.items()
			if name in names
		}

	def indexes(self):
		return list(self.index.values())

	def get_lock_wait_timeout(self):
		return self.session_lock_wait

	def set_lock_wait_timeout(self, seconds):
		self.lock_wait_set.append(seconds)
		self.session_lock_wait = seconds

	def alter(self, statement):
		self.alters.append((statement, self.session_lock_wait))
		for i, (text, error) in enumerate(self.fail):
			if text in statement:
				del self.fail[i]
				raise error
		if any(text in statement for text in self.ineffective):
			return
		if match := ADD_COLUMN.fullmatch(statement):
			name, algorithm = match.groups()
			if name in self.table:
				return  # IF NOT EXISTS: a note, nothing changes
			if algorithm in self.refused:
				raise MariaDBError(
					1845, f"ALGORITHM={algorithm} is not supported for this operation. Try ALGORITHM=INPLACE"
				)
			self.table[name] = varchar(name)
		elif match := ADD_INDEX.fullmatch(statement):
			unique, name, column = match.groups()
			if name in self.index:
				return
			if unique and column in self.duplicated:
				raise MariaDBError(1062, f"Duplicate entry 'k-1' for key '{name}'")
			self.index[name] = Index(name, bool(unique), (column,))
		else:
			raise AssertionError(f"the step must not issue: {statement}")

	def _key(self, name):
		"""information_schema.columns.column_key: UNI for the sole column of a unique index, MUL
		for the first column of any other index."""
		if any(index.unique and index.columns == (name,) for index in self.index.values()):
			return "UNI"
		if any(index.columns[:1] == (name,) for index in self.index.values()):
			return "MUL"
		return ""


def production():
	"""Delivery Note as production has it (review of 29 Sep): the stamp flagged and indexed, no key column."""
	return FakeMariaDB(
		columns=[varchar(STAMP)],
		indexes=[Index("PRIMARY", True, ("name",)), Index(dkc.STAMP_INDEX, False, (STAMP,))],
		stamp_field=STAMP_FIELD,
	)


def ready():
	db = production()
	for name in (LIVE, QC):
		db.table[name] = varchar(name)
		db.index[name] = Index(name, True, (name,))
	return db


def statements(db):
	return [statement for statement, _lock_wait in db.alters]


class StepTest(unittest.TestCase):
	def ensure(self, db, **kwargs):
		self.log, self.sleeps = [], []
		return dkc.ensure(db, log=self.log.append, sleep=self.sleeps.append, **kwargs)

	def assertReady(self, db):
		self.assertEqual(dkc.unready(dkc.read_state(db)), [])

	def assertEveryAlterCapped(self, db, seconds=dkc.LOCK_WAIT_SECONDS):
		self.assertTrue(db.alters)
		self.assertEqual({lock_wait for _statement, lock_wait in db.alters}, {seconds})
		self.assertEqual(db.session_lock_wait, SESSION_DEFAULT, "the session's own value is back")


class TestEnsure(StepTest):
	def test_adds_both_key_columns_online_each_alter_under_the_capped_lock_wait(self):
		db = production()
		ran = self.ensure(db)

		self.assertEqual(
			ran,
			[
				dkc.add_column_sql(LIVE, "INSTANT"),
				dkc.add_unique_index_sql(LIVE),
				dkc.add_column_sql(QC, "INSTANT"),
				dkc.add_unique_index_sql(QC),
			],
		)
		self.assertEqual(statements(db), ran)
		self.assertEveryAlterCapped(db)
		self.assertEqual(db.lock_wait_set, [dkc.LOCK_WAIT_SECONDS, SESSION_DEFAULT])
		self.assertEqual(self.sleeps, [])
		self.assertReady(db)

	def test_a_ready_table_is_only_read(self):
		db = ready()
		self.assertEqual(self.ensure(db), [])
		self.assertEqual(db.alters, [])
		self.assertEqual(db.lock_wait_set, [], "not even the session's lock wait is touched")

	def test_a_second_run_changes_nothing(self):
		db = production()
		self.ensure(db)
		before = list(db.alters)

		self.assertEqual(self.ensure(db), [])
		self.assertEqual(db.alters, before)

	def test_a_run_stopped_after_a_column_is_completed_by_the_next(self):
		db = production()
		db.table[LIVE] = varchar(LIVE)  # the column went in, then the run stopped

		self.assertEqual(
			self.ensure(db),
			[dkc.add_unique_index_sql(LIVE), dkc.add_column_sql(QC, "INSTANT"), dkc.add_unique_index_sql(QC)],
		)
		self.assertReady(db)

	def test_a_key_column_frappe_already_built_is_left_as_it_is(self):
		db = production()  # e.g. IDEV-3266's patch ran first, through Frappe (varchar(140) unique)
		db.table[QC] = varchar(QC)
		db.index[QC] = Index(QC, True, (QC,))

		self.assertEqual(
			self.ensure(db), [dkc.add_column_sql(LIVE, "INSTANT"), dkc.add_unique_index_sql(LIVE)]
		)
		self.assertReady(db)

	def test_the_cap_and_the_tries_can_be_set(self):
		db = production()
		self.ensure(db, lock_wait=10, attempts=2)
		self.assertEveryAlterCapped(db, seconds=10)

		for bad in ({"lock_wait": 0}, {"attempts": 0}):
			with self.subTest(bad), self.assertRaises(ValueError):
				self.ensure(production(), **bad)


class TestLockWaits(StepTest):
	def test_a_lock_wait_timeout_changes_nothing_and_is_retried_after_a_pause(self):
		db = production()
		db.fail = [(f"UNIQUE INDEX IF NOT EXISTS `{LIVE}`", lock_wait_timeout()) for _ in range(2)]

		self.ensure(db)

		self.assertEqual(statements(db).count(dkc.add_unique_index_sql(LIVE)), 3)
		self.assertEqual(self.sleeps, [2, 4])
		self.assertEveryAlterCapped(db)
		self.assertReady(db)

	def test_it_gives_up_after_the_last_try_leaving_a_valid_table_that_a_rerun_completes(self):
		db = production()
		db.fail = [(f"UNIQUE INDEX IF NOT EXISTS `{LIVE}`", lock_wait_timeout()) for _ in range(dkc.ATTEMPTS)]

		with self.assertRaises(KeyColumnsError) as stopped:
			self.ensure(db)

		message = str(stopped.exception)
		self.assertIn(f"Gave up after {dkc.ATTEMPTS} tries of: {dkc.add_unique_index_sql(LIVE)}", message)
		self.assertIn(dkc.add_column_sql(LIVE, "INSTANT"), message)  # what this run did
		self.assertIn("innodb_trx", message)
		self.assertEqual(self.sleeps, [2, 4, 8, 16])
		self.assertEqual(db.session_lock_wait, SESSION_DEFAULT)
		# Valid: the new column is in (unused until its Custom Field exists), IDEV-3266's not started.
		self.assertEqual(set(db.table), {STAMP, LIVE})
		self.assertNotIn(LIVE, db.index)

		self.assertEqual(
			self.ensure(db),
			[dkc.add_unique_index_sql(LIVE), dkc.add_column_sql(QC, "INSTANT"), dkc.add_unique_index_sql(QC)],
		)
		self.assertReady(db)

	def test_the_pause_doubles_up_to_a_limit(self):
		db = production()
		db.fail = [(f"ADD COLUMN IF NOT EXISTS `{LIVE}`", lock_wait_timeout()) for _ in range(7)]
		self.ensure(db, attempts=8)
		self.assertEqual(self.sleeps, [2, 4, 8, 16, 30, 30, 30])

	def test_a_deadlock_is_retried_like_a_lock_wait(self):
		db = production()
		db.fail = [
			(f"ADD COLUMN IF NOT EXISTS `{QC}`", MariaDBError(1213, "Deadlock found when trying to get lock"))
		]
		self.ensure(db)
		self.assertEqual(self.sleeps, [2])
		self.assertReady(db)

	def test_frappes_wrapped_lock_wait_timeout_is_recognised(self):
		inner = lock_wait_timeout()
		wrapped = QueryTimeoutError(inner)
		wrapped.__cause__ = inner
		db = production()
		db.fail = [(f"ADD COLUMN IF NOT EXISTS `{LIVE}`", wrapped)]

		self.ensure(db)

		self.assertEqual(self.sleeps, [2])
		self.assertReady(db)


class TestNeverARebuild(StepTest):
	def test_instant_refused_falls_back_to_nocopy(self):
		db = production()
		db.refused = {"INSTANT"}

		ran = self.ensure(db)

		self.assertEqual(ran[0], dkc.add_column_sql(LIVE, "NOCOPY"))
		self.assertEqual(ran[2], dkc.add_column_sql(QC, "NOCOPY"))
		self.assertReady(db)

	def test_when_both_fast_algorithms_are_refused_it_stops_without_adding_the_column(self):
		db = production()
		db.refused = {"INSTANT", "NOCOPY"}

		with self.assertRaises(KeyColumnsError) as stopped:
			self.ensure(db)

		self.assertIn("only by rebuilding the table", str(stopped.exception))
		self.assertIn("innodb_instant_alter_column_allowed", str(stopped.exception))
		self.assertEqual(
			statements(db), [dkc.add_column_sql(LIVE, "INSTANT"), dkc.add_column_sql(LIVE, "NOCOPY")]
		)
		self.assertNotIn(LIVE, db.table)
		self.assertEqual(db.session_lock_wait, SESSION_DEFAULT)
		self.assertEqual(self.sleeps, [])


class TestOtherFailures(StepTest):
	def test_any_other_error_stops_it_at_once_with_the_session_restored(self):
		db = production()
		db.fail = [(f"UNIQUE INDEX IF NOT EXISTS `{LIVE}`", MariaDBError(2013, "Lost connection to server"))]

		with self.assertRaises(MariaDBError):
			self.ensure(db)

		self.assertEqual(self.sleeps, [])
		self.assertEqual(db.session_lock_wait, SESSION_DEFAULT)

	def test_an_interrupt_restores_the_session_too(self):
		db = production()
		db.fail = [(f"UNIQUE INDEX IF NOT EXISTS `{LIVE}`", KeyboardInterrupt())]

		with self.assertRaises(KeyboardInterrupt):
			self.ensure(db)

		self.assertEqual(db.session_lock_wait, SESSION_DEFAULT)

	def test_a_restore_that_fails_does_not_hide_the_first_error(self):
		db = production()
		db.fail = [(f"UNIQUE INDEX IF NOT EXISTS `{LIVE}`", MariaDBError(2013, "Lost connection to server"))]
		set_lock_wait = db.set_lock_wait_timeout

		def fail_after_the_first(seconds):
			if db.lock_wait_set:
				raise MariaDBError(2006, "MySQL server has gone away")
			set_lock_wait(seconds)

		db.set_lock_wait_timeout = fail_after_the_first

		with self.assertRaises(MariaDBError) as failed:
			self.ensure(db)

		self.assertEqual(failed.exception.args[0], 2013)
		self.assertTrue(any("could not set lock_wait_timeout back" in line for line in self.log))

	def test_a_duplicate_value_stops_the_unique_index_saying_how_to_find_it(self):
		db = production()
		db.table[LIVE] = varchar(LIVE)
		db.duplicated = {LIVE}

		with self.assertRaises(KeyColumnsError) as stopped:
			self.ensure(db)

		self.assertIn(f"{LIVE} holds one value on two rows", str(stopped.exception))
		self.assertIn("HAVING COUNT(*) > 1", str(stopped.exception))
		self.assertNotIn(LIVE, db.index)
		self.assertEqual(self.sleeps, [])

	def test_an_alter_that_did_not_take_is_reported_not_trusted(self):
		db = production()
		db.ineffective = {f"UNIQUE INDEX IF NOT EXISTS `{QC}`"}

		with self.assertRaises(KeyColumnsError) as stopped:
			self.ensure(db)

		self.assertIn(f"{QC}: no unique index", str(stopped.exception))


class TestRefusedBeforeAnyChange(StepTest):
	def assertRefusedUntouched(self, db, reason):
		stamp = db.stamp_field()
		with self.assertRaises(KeyColumnsError) as stopped:
			self.ensure(db)
		self.assertIn("Nothing was changed", str(stopped.exception))
		self.assertIn(reason, str(stopped.exception))
		self.assertEqual(db.alters, [])
		self.assertEqual(db.lock_wait_set, [])
		self.assertEqual(db.stamp_field(), stamp)

	def test_a_key_column_of_another_type(self):
		db = production()
		db.table[LIVE] = varchar(LIVE, type="varchar(255)")
		self.assertRefusedUntouched(db, f"{LIVE}: it is varchar(255), not varchar(140)")

	def test_a_not_null_key_column(self):
		db = production()
		db.table[QC] = varchar(QC, nullable=False)
		self.assertRefusedUntouched(db, f"{QC}: it is NOT NULL")

	def test_a_key_column_with_a_default(self):
		db = production()
		db.table[QC] = varchar(QC, default="''")
		self.assertRefusedUntouched(db, f"{QC}: it has the default ''")

	def test_a_plain_index_on_a_key_column(self):
		db = production()
		db.table[LIVE] = varchar(LIVE)
		db.index[f"{LIVE}_index"] = Index(f"{LIVE}_index", False, (LIVE,))
		self.assertRefusedUntouched(db, f"it has the plain index {LIVE}_index")

	def test_a_stamp_flagged_unique_without_a_unique_index(self):
		db = production()
		db.stamp.update(unique=1, search_index=0)
		self.assertRefusedUntouched(db, "Custom Field says Unique but the column has no unique index")


class TestStamp(StepTest):
	def test_flags_the_stamp_and_builds_its_index_under_the_cap_where_it_is_missing(self):
		db = production()  # the lab's state: no index, flag off
		db.stamp["search_index"] = 0
		del db.index[dkc.STAMP_INDEX]

		ran = self.ensure(db)

		self.assertEqual(db.stamp["search_index"], 1)
		self.assertEqual(ran[-1], dkc.add_stamp_index_sql())
		self.assertEveryAlterCapped(db)
		self.assertReady(db)

	def test_flags_a_stamp_whose_index_exists_so_frappe_keeps_it(self):
		db = ready()
		db.stamp["search_index"] = 0

		self.assertEqual(self.ensure(db), [])

		self.assertEqual(db.stamp["search_index"], 1)

	def test_any_plain_index_led_by_the_stamp_counts(self):
		db = ready()
		del db.index[dkc.STAMP_INDEX]
		db.index["stamp_and_status"] = Index("stamp_and_status", False, (STAMP, "status"))

		self.assertEqual(self.ensure(db), [])

	def test_a_unique_stamp_with_its_unique_index_is_left_alone(self):
		db = ready()
		db.stamp.update(unique=1, search_index=0)
		del db.index[dkc.STAMP_INDEX]
		db.index[STAMP] = Index(STAMP, True, (STAMP,))

		self.assertEqual(self.ensure(db), [])

		self.assertEqual(db.stamp["search_index"], 0)

	def test_a_stamp_field_without_its_column_only_gets_its_flag(self):
		db = production()
		db.stamp["search_index"] = 0
		del db.table[STAMP]
		del db.index[dkc.STAMP_INDEX]

		self.assertEqual(len(self.ensure(db)), 4)  # the key columns, no stamp index

		self.assertEqual(db.stamp["search_index"], 1)

	def test_a_site_without_the_stamp_field_gets_only_the_key_columns(self):
		db = production()
		db.stamp = None
		del db.index[dkc.STAMP_INDEX]

		self.assertEqual(len(self.ensure(db)), 4)
		self.assertNotIn(dkc.STAMP_INDEX, db.index)


class TestReadyMeansFrappeAltersNothing(unittest.TestCase):
	"""unready() == [] must mean Frappe's sync of the table has nothing to do for the key fields. The
	cases follow the review's replay of frappe 15.121.1 (review/idev3269/bench/raw/replay_frappe_diff.txt):
	R1 the split pre-create -> 0 statements; R2 no column -> ADD COLUMN ... unique; R4 column without
	its unique index -> ADD UNIQUE INDEX; R5 varchar(255) -> MODIFY."""

	def unready(self, db):
		return dkc.unready(dkc.read_state(db))

	def test_r1_what_ensure_leaves_is_ready(self):
		self.assertEqual(self.unready(ready()), [])

	def test_r2_no_column_is_not(self):
		self.assertEqual(self.unready(production()), [f"{LIVE}: missing", f"{QC}: missing"])

	def test_r4_a_column_without_its_unique_index_is_not(self):
		db = ready()
		del db.index[LIVE]
		self.assertEqual(self.unready(db), [f"{LIVE}: no unique index"])

	def test_r5_a_wider_column_is_not(self):
		db = ready()
		db.table[LIVE] = varchar(LIVE, type="varchar(255)")
		(problem,) = self.unready(db)
		self.assertTrue(problem.startswith(f"{LIVE}: it is varchar(255), not varchar(140)"), problem)


class TestErrorCode(unittest.TestCase):
	def test_reads_pymysql_and_frappe_shapes(self):
		inner = MariaDBError(1205, "Lock wait timeout exceeded")
		chained = RuntimeError("wrapped")
		chained.__cause__ = MariaDBError(1213, "Deadlock found")
		cases = [
			(inner, 1205),
			(QueryTimeoutError(inner), 1205),
			(chained, 1213),
			(ValueError("no code"), None),
			(Exception(True), None),
			(Exception(), None),
		]
		for error, code in cases:
			with self.subTest(error=repr(error)):
				self.assertEqual(dkc.error_code(error), code)


class TestStatements(unittest.TestCase):
	def test_the_online_forms(self):
		self.assertEqual(
			dkc.add_column_sql(LIVE, "INSTANT"),
			"ALTER TABLE `tabDelivery Note` ADD COLUMN IF NOT EXISTS `custom_live_invoiced_item_id` "
			"varchar(140) DEFAULT NULL, ALGORITHM=INSTANT",
		)
		self.assertEqual(
			dkc.add_unique_index_sql(QC),
			"ALTER TABLE `tabDelivery Note` ADD UNIQUE INDEX IF NOT EXISTS `custom_qc_idempotency_key` "
			"(`custom_qc_idempotency_key`), ALGORITHM=INPLACE, LOCK=NONE",
		)
		self.assertEqual(
			dkc.add_stamp_index_sql(),
			"ALTER TABLE `tabDelivery Note` ADD INDEX IF NOT EXISTS `custom_invoiced_item_id_index` "
			"(`custom_invoiced_item_id`), ALGORITHM=INPLACE, LOCK=NONE",
		)


class TestRun(unittest.TestCase):
	"""bench execute retries a method that raises Exception by eval()-ing its dotted path, which for
	an app module ends in a NameError that buries the reason. run() leaves with SystemExit instead."""

	def test_a_stop_exits_non_zero_with_the_reason(self):
		with (
			mock.patch.object(dkc, "FrappeDB"),
			mock.patch.object(dkc, "ensure", side_effect=KeyColumnsError("Gave up after 5 tries")),
		):
			with self.assertRaises(SystemExit) as exited:
				dkc.run()
		self.assertNotIsInstance(exited.exception, Exception)
		self.assertIn("Gave up after 5 tries", str(exited.exception.code))

	def test_an_unexpected_error_prints_its_traceback_then_exits_non_zero(self):
		with (
			mock.patch.object(dkc, "FrappeDB"),
			mock.patch.object(dkc, "ensure", side_effect=MariaDBError(2013, "Lost connection")),
			mock.patch.object(dkc.traceback, "print_exc") as print_exc,
		):
			with self.assertRaises(SystemExit) as exited:
				dkc.run()
		print_exc.assert_called_once()
		self.assertIn("STOPPED", str(exited.exception.code))

	def test_bench_kwargs_reach_the_step(self):
		with mock.patch.object(dkc, "FrappeDB"), mock.patch.object(dkc, "ensure") as ensure:
			dkc.run(lock_wait="10", attempts="3")
		self.assertEqual(ensure.call_args.kwargs, {"lock_wait": 10, "attempts": 3})


class TestFrappeDB(unittest.TestCase):
	"""The adapter's reading of information_schema, with frappe.db faked."""

	def setUp(self):
		self.frappe = types.ModuleType("frappe")
		self.frappe.db = mock.MagicMock()
		self.frappe.cache = mock.MagicMock()
		patcher = mock.patch.dict(sys.modules, {"frappe": self.frappe})
		patcher.start()
		self.addCleanup(patcher.stop)
		self.db = dkc.FrappeDB()

	def test_columns_read_a_null_default_either_way_and_the_key(self):
		self.frappe.db.sql.return_value = (
			(LIVE, "varchar(140)", "YES", "NULL", "UNI"),
			(QC, "VARCHAR(140)", "YES", None, ""),
			(STAMP, "varchar(140)", "NO", "'x'", "MUL"),
		)
		columns = self.db.columns((STAMP, LIVE, QC))
		self.assertEqual(columns[LIVE], Column(LIVE, "varchar(140)", True, None, "UNI"))
		self.assertEqual(columns[QC], Column(QC, "varchar(140)", True, None, ""))
		self.assertEqual(columns[STAMP], Column(STAMP, "varchar(140)", False, "'x'", "MUL"))
		query, values = self.frappe.db.sql.call_args.args
		self.assertIn("table_schema = database()", query)
		self.assertEqual(values, (dkc.TABLE, (STAMP, LIVE, QC)))

	def test_indexes_are_grouped_in_column_order(self):
		self.frappe.db.sql.return_value = (
			("PRIMARY", 0, "name"),
			("stamp_and_status", 1, STAMP),
			("stamp_and_status", 1, "status"),
			(LIVE, 0, LIVE),
		)
		self.assertEqual(
			self.db.indexes(),
			[
				Index("PRIMARY", True, ("name",)),
				Index("stamp_and_status", False, (STAMP, "status")),
				Index(LIVE, True, (LIVE,)),
			],
		)

	def test_alter_goes_through_sql_ddl_and_forgets_the_cached_columns(self):
		self.db.alter("ALTER TABLE x")
		self.frappe.db.sql_ddl.assert_called_once_with("ALTER TABLE x")
		self.frappe.cache.hdel.assert_called_once_with("table_columns", dkc.TABLE)

	def test_session_lock_wait(self):
		self.frappe.db.sql.return_value = ((86400,),)
		self.assertEqual(self.db.get_lock_wait_timeout(), 86400)
		self.db.set_lock_wait_timeout(5)
		self.frappe.db.sql.assert_called_with("set session lock_wait_timeout = 5")


class TestPatchesTxt(unittest.TestCase):
	def test_the_step_runs_before_model_sync_so_before_every_key_field_patch(self):
		sections, section = {}, None
		for line in (APP / "patches.txt").read_text().splitlines():
			line = line.strip()
			if line.startswith("[") and line.endswith("]"):
				section = line[1:-1]
			elif line and not line.startswith("#"):
				sections.setdefault(section, []).append(line.split()[0])

		self.assertIn("fuelbuddy_crm.patches.precreate_dn_key_columns", sections["pre_model_sync"])
		self.assertIn("fuelbuddy_crm.patches.add_dn_live_invoiced_item_key", sections["post_model_sync"])
		for patches in sections.values():  # IDEV-3266's, once merged
			if "fuelbuddy_crm.patches.add_dn_qc_idempotency_key" in patches:
				self.assertIs(patches, sections["post_model_sync"])


@unittest.skipIf(
	importlib.util.find_spec("frappe") is not None,
	"frappe is installed: on a site, test_dn_live_key runs the real patches",
)
class TestPatches(unittest.TestCase):
	"""The patch modules, imported against a stub of frappe."""

	def setUp(self):
		stub = types.ModuleType("frappe")
		stub._ = lambda text, *args, **kwargs: text
		stub.db = mock.MagicMock()
		utils = types.ModuleType("frappe.utils")
		utils.flt = float
		utils.cint = int
		custom_field = types.ModuleType("frappe.custom.doctype.custom_field.custom_field")
		custom_field.create_custom_fields = self.create_custom_fields = mock.MagicMock()
		modules = {
			"frappe": stub,
			"frappe.utils": utils,
			"frappe.custom": types.ModuleType("frappe.custom"),
			"frappe.custom.doctype": types.ModuleType("frappe.custom.doctype"),
			"frappe.custom.doctype.custom_field": types.ModuleType("frappe.custom.doctype.custom_field"),
			"frappe.custom.doctype.custom_field.custom_field": custom_field,
		}
		patcher = mock.patch.dict(sys.modules, modules)
		patcher.start()
		self.addCleanup(patcher.stop)  # also drops every module imported against the stub
		for name in [name for name in sys.modules if name.startswith("fuelbuddy_crm.patches.")]:
			del sys.modules[name]
		for name in ("fuelbuddy_crm.dn_validation", "fuelbuddy_crm.dn_versioning"):
			sys.modules.pop(name, None)

	def module(self, name):
		return importlib.import_module(name)

	def test_the_live_key_patch_readies_the_table_before_it_creates_the_field(self):
		patch = self.module("fuelbuddy_crm.patches.add_dn_live_invoiced_item_key")
		db = production()
		order = mock.Mock()
		order.attach_mock(self.create_custom_fields, "create_custom_fields")
		ensure = dkc.ensure

		def step(db, **kwargs):
			order.ensure()
			return ensure(db, log=lambda *_: None, sleep=lambda _: None, **kwargs)

		with mock.patch.object(dkc, "FrappeDB", return_value=db), mock.patch.object(dkc, "ensure", step):
			patch.execute()

		self.assertEqual([call[0] for call in order.mock_calls], ["ensure", "create_custom_fields"])
		self.create_custom_fields.assert_called_once_with(patch.CUSTOM_FIELDS, ignore_validate=True)
		self.assertEqual(dkc.unready(dkc.read_state(db)), [])

	def test_a_step_that_stops_leaves_no_field_behind(self):
		patch = self.module("fuelbuddy_crm.patches.add_dn_live_invoiced_item_key")
		with (
			mock.patch.object(dkc, "FrappeDB", return_value=production()),
			mock.patch.object(dkc, "ensure", side_effect=KeyColumnsError("Gave up")),
		):
			with self.assertRaises(KeyColumnsError):
				patch.execute()
		self.create_custom_fields.assert_not_called()

	def test_the_live_key_patch_fails_loudly_if_the_field_sync_undid_the_columns(self):
		patch = self.module("fuelbuddy_crm.patches.add_dn_live_invoiced_item_key")
		db = ready()
		self.create_custom_fields.side_effect = lambda *args, **kwargs: db.index.pop(LIVE)
		with mock.patch.object(dkc, "FrappeDB", return_value=db), mock.patch.object(dkc, "ensure"):
			with self.assertRaises(KeyColumnsError) as stopped:
				patch.execute()
		self.assertIn(f"{LIVE}: no unique index", str(stopped.exception))

	def test_the_precreate_patch_runs_the_step(self):
		patch = self.module("fuelbuddy_crm.patches.precreate_dn_key_columns")
		db = production()
		with mock.patch.object(dkc, "FrappeDB", return_value=db), mock.patch.object(dkc, "ensure") as ensure:
			patch.execute()
		ensure.assert_called_once_with(db)

	def test_every_key_field_definition_is_what_the_step_builds(self):
		"""Data, unique, no length, default or search_index: anything else and Frappe alters the
		column the step made (IDEV-3266's patch is checked once it is in the tree)."""
		found = {}
		for name in ("add_dn_live_invoiced_item_key", "add_dn_qc_idempotency_key"):
			if importlib.util.find_spec(f"fuelbuddy_crm.patches.{name}") is None:
				continue
			for field in self.module(f"fuelbuddy_crm.patches.{name}").CUSTOM_FIELDS["Delivery Note"]:
				if field["fieldname"] in dkc.KEY_COLUMNS:
					found[field["fieldname"]] = field

		self.assertIn(LIVE, found)
		for fieldname, field in found.items():
			with self.subTest(fieldname):
				self.assertEqual(field["fieldtype"], "Data")
				self.assertTrue(field.get("unique"))
				for prop in ("length", "default", "search_index"):
					self.assertFalse(field.get(prop), prop)

	def test_the_column_names_are_the_apps(self):
		dn_validation = self.module("fuelbuddy_crm.dn_validation")
		self.assertEqual((dn_validation.STAMP_FIELD, dn_validation.LIVE_KEY_FIELD), (STAMP, LIVE))
		dn_versioning = self.module("fuelbuddy_crm.dn_versioning")
		if hasattr(dn_versioning, "QC_IDEMPOTENCY_KEY_FIELD"):  # IDEV-3266, once merged
			self.assertEqual(dn_versioning.QC_IDEMPOTENCY_KEY_FIELD, QC)


if __name__ == "__main__":
	unittest.main()
