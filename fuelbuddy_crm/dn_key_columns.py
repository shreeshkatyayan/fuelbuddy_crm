# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""The Delivery Note key columns, added online by a step that is safe to interrupt and re-run.

custom_live_invoiced_item_id (IDEV-3269) and custom_qc_idempotency_key (IDEV-3266) are unique Data
fields. Left to create_custom_fields, Frappe adds each one with a single ``ADD COLUMN ... unique``,
which rebuilds the whole table (17-33 s on a production-sized copy of ~640k rows), and it
commits the Custom Field before that ALTER starts. Every Delivery Note save from a worker that has
loaded the new field then fails with 1054 "Unknown column" until the rebuild ends -- and for good if
the ALTER is interrupted (a lock-wait timeout, a kill): a second migrate finds the field unchanged
and never adds the column.

ensure() does the table's part first, and the table is valid after every statement:

1. It reads information_schema and, before changing anything, refuses a table Frappe would still
   alter: a key column of another type, NOT NULL, with a default or with a plain index, or the
   stamp (custom_invoiced_item_id) flagged unique without a unique index, which Frappe's next sync
   of the table would try, and fail, to build over the stamps that repeat.
2. It adds each missing key column as ``varchar(140) DEFAULT NULL`` with ALGORITHM=INSTANT (NOCOPY
   if INSTANT is refused; never a table rebuild), then its unique index with ALGORITHM=INPLACE,
   LOCK=NONE, which builds while writes carry on (about 6 s at production size). As the IDEV-3269
   patch always did, it also flags the stamp's search_index and builds its plain index if missing.
3. Every ALTER runs under ``SET SESSION lock_wait_timeout = 5``. An ALTER holds the table's metadata
   lock exclusively for a moment at its start and end, and while it waits for that lock every
   Delivery Note read and write queues behind it. One open transaction on the table would otherwise
   freeze Delivery Notes for as long as it stays open. Capped, the freeze lasts 5 s at most, the
   ALTER fails with 1205 having changed nothing, and it is retried after 2, 4, 8 and 16 s.
4. It checks the columns are exactly what Frappe builds for a unique Data field, so the Custom
   Fields can follow: Frappe's sync then has nothing to change for them.

No Delivery Note save names a key column before its Custom Field exists, so what is added here is
inert until then. MariaDB applies each ALTER whole or not at all, so wherever a run stops, the
table is valid and the next run carries on.

Run by patches/precreate_dn_key_columns (pre_model_sync, so before any patch creates a key field)
and again by patches/add_dn_live_invoiced_item_key, where it only reads. To choose the maintenance
moment yourself, run it before the migrate with
``bench --site <site> execute fuelbuddy_crm.dn_key_columns.run`` (docs/dn-key-columns.md).

Pure Python: only FrappeDB imports frappe, so the step logic is tested without a site
(tests/test_dn_key_columns.py)."""

import functools
import time
import traceback
from dataclasses import dataclass

TABLE = "tabDelivery Note"
STAMP = "custom_invoiced_item_id"
STAMP_INDEX = f"{STAMP}_index"  # the name Frappe gives a search_index field's index
COLUMN_TYPE = "varchar(140)"  # a Data field's column in Frappe (VARCHAR_LEN)

# The unique Data fields on Delivery Note. Each column gets a unique index named after it, as Frappe
# names one. The patch that creates each Custom Field must keep it Data and unique, with no length,
# default or search_index, or Frappe alters the column after all (tests/test_dn_key_columns.py
# checks every definition in the tree).
KEY_COLUMNS = (
	"custom_live_invoiced_item_id",  # IDEV-3269, patches/add_dn_live_invoiced_item_key
	"custom_qc_idempotency_key",  # IDEV-3266, patches/add_dn_qc_idempotency_key
)

LOCK_WAIT_SECONDS = 5
ATTEMPTS = 5
FIRST_BACKOFF_SECONDS = 2
MAX_BACKOFF_SECONDS = 30

ER_DUP_ENTRY = 1062
ER_LOCK_WAIT_TIMEOUT = 1205
ER_LOCK_DEADLOCK = 1213
ER_ALTER_OPERATION_NOT_SUPPORTED = 1845
ER_ALTER_OPERATION_NOT_SUPPORTED_REASON = 1846
# The ALTER waited for a lock and gave up: it changed nothing, so it is retried.
LOCK_ERRORS = frozenset({ER_LOCK_WAIT_TIMEOUT, ER_LOCK_DEADLOCK})
# MariaDB will not run the ALTER with the ALGORITHM asked for (and changed nothing).
REFUSED = frozenset({ER_ALTER_OPERATION_NOT_SUPPORTED, ER_ALTER_OPERATION_NOT_SUPPORTED_REASON})

DOCS = "docs/dn-key-columns.md"

_print = functools.partial(print, flush=True)


class KeyColumnsError(Exception):
	"""ensure() stopped. The table is valid: each ALTER applied whole or not at all, and a re-run
	carries on from where this one stopped."""


@dataclass(frozen=True)
class Column:
	name: str
	type: str  # information_schema column_type, e.g. "varchar(140)"
	nullable: bool
	default: str | None  # None when the default is NULL
	key: str  # information_schema column_key: "PRI", "UNI", "MUL" or ""


@dataclass(frozen=True)
class Index:
	name: str
	unique: bool
	columns: tuple[str, ...]


@dataclass(frozen=True)
class State:
	stamp_field: dict | None  # the stamp's Custom Field: name, unique, search_index
	columns: dict  # name -> Column, for STAMP and KEY_COLUMNS where they exist
	indexes: list  # every Index on the table


# ---- the statements: the online forms measured at production size -------------------------------
def add_column_sql(column, algorithm):
	return (
		f"ALTER TABLE `{TABLE}` ADD COLUMN IF NOT EXISTS `{column}` {COLUMN_TYPE} DEFAULT NULL, "
		f"ALGORITHM={algorithm}"
	)


def add_unique_index_sql(column):
	return (
		f"ALTER TABLE `{TABLE}` ADD UNIQUE INDEX IF NOT EXISTS `{column}` (`{column}`), "
		"ALGORITHM=INPLACE, LOCK=NONE"
	)


def add_stamp_index_sql():
	return (
		f"ALTER TABLE `{TABLE}` ADD INDEX IF NOT EXISTS `{STAMP_INDEX}` (`{STAMP}`), "
		"ALGORITHM=INPLACE, LOCK=NONE"
	)


# ---- reading the table ---------------------------------------------------------------------------
def read_state(db):
	return State(db.stamp_field(), db.columns((STAMP, *KEY_COLUMNS)), db.indexes())


def plain_index(indexes, column):
	"""The first non-unique index led by column: what Frappe counts as the column's search index."""
	return next((index for index in indexes if not index.unique and index.columns[:1] == (column,)), None)


def key_column_problems(current, indexes):
	"""Why a key column that exists is not what Frappe builds for a unique Data field, leaving out
	the unique index (ensure() adds that). Frappe would MODIFY or re-index such a column at its next
	sync of the table, and a NOT NULL one fails every insert that does not name it. The first, third
	and fourth checks mirror frappe 15.121.1 DbColumn.build_for_alter_table (type, default, index)."""
	problems = []
	if current.type != COLUMN_TYPE:
		problems.append(f"it is {current.type}, not {COLUMN_TYPE}: Frappe would MODIFY it (a table rebuild)")
	if not current.nullable:
		problems.append("it is NOT NULL: a save that does not name the column would fail")
	if current.default is not None:
		problems.append(f"it has the default {current.default}: Frappe would MODIFY it")
	if index := plain_index(indexes, current.name):
		problems.append(f"it has the plain index {index.name}: Frappe would drop it")
	return problems


def stamp_problem(state):
	"""The stamp's Custom Field says unique but the column has no unique index. Frappe's next sync of
	the table (the Custom Field this release adds runs one) would build that index over the stamps
	that repeat -- a cancelled original and its amendment, a Delivery Note and its return share one --
	and fail on the duplicates."""
	field, column = state.stamp_field, state.columns.get(STAMP)
	if field and field["unique"] and column and column.key != "UNI":
		return (
			"its Custom Field says Unique but the column has no unique index, and the stamps repeat: "
			"Frappe's next sync of the table would try to build that index and fail"
		)
	return None


def stamp_wants_index(state):
	"""The stamp is indexed once ensure() has run: it flags search_index unless the field is unique."""
	field = state.stamp_field
	return bool(field and STAMP in state.columns and (field["search_index"] or not field["unique"]))


def unready(state):
	"""What still keeps the table from being ready for the key fields ([] when it is ready)."""
	problems = []
	for name in KEY_COLUMNS:
		current = state.columns.get(name)
		if current is None:
			problems.append(f"{name}: missing")
			continue
		problems += [f"{name}: {problem}" for problem in key_column_problems(current, state.indexes)]
		if current.key != "UNI":
			problems.append(f"{name}: no unique index")
	if stamp_wants_index(state) and not plain_index(state.indexes, STAMP):
		problems.append(f"{STAMP}: no plain index")
	return problems


def _describe(current):
	if current is None:
		return "missing"
	nullable = "NULL" if current.nullable else "NOT NULL"
	unique = "unique index" if current.key == "UNI" else "no unique index"
	return f"{current.type} {nullable}, {unique}"


def _describe_stamp(state):
	field = state.stamp_field
	if not field:
		return "no Custom Field on this site: left alone"
	if STAMP not in state.columns:
		return "a Custom Field but no column: only its search_index flag is looked after"
	index = plain_index(state.indexes, STAMP)
	return (
		f"Custom Field unique={int(bool(field['unique']))} search_index={int(bool(field['search_index']))}, "
		f"{f'plain index {index.name}' if index else 'no plain index'}"
	)


# ---- the step --------------------------------------------------------------------------------------
def ensure(db, *, lock_wait=LOCK_WAIT_SECONDS, attempts=ATTEMPTS, log=_print, sleep=time.sleep):
	"""Makes the Delivery Note table ready for the key fields (see the module docstring), or raises
	KeyColumnsError saying what stopped it; the table is valid either way. Returns the ALTER
	statements that changed the table: none when it was ready, and then it ran no ALTER at all."""
	lock_wait, attempts = int(lock_wait), int(attempts)
	if lock_wait < 1 or attempts < 1:
		raise ValueError("lock_wait and attempts must be at least 1")
	state = read_state(db)
	log(f"Delivery Note key columns ({TABLE}):")
	for name in KEY_COLUMNS:
		log(f"  {name}: {_describe(state.columns.get(name))}")
	log(f"  {STAMP}: {_describe_stamp(state)}")

	problems = [
		f"{name}: {problem}"
		for name in KEY_COLUMNS
		if name in state.columns
		for problem in key_column_problems(state.columns[name], state.indexes)
	]
	if problem := stamp_problem(state):
		problems.append(f"{STAMP}: {problem}")
	if problems:
		raise KeyColumnsError(
			"Nothing was changed. The table is in a state this step does not change by itself; a person "
			f"decides the fix ({DOCS}, 'If it stops'):\n- " + "\n- ".join(problems)
		)

	field = state.stamp_field
	flag_stamp = bool(field and not field["search_index"] and not field["unique"])
	statements = []
	for name in KEY_COLUMNS:
		current = state.columns.get(name)
		if current is None:
			statements.append((name, "column"))
		if current is None or current.key != "UNI":
			statements.append((name, "unique"))
	if stamp_wants_index(state) and not plain_index(state.indexes, STAMP):
		statements.append((STAMP, "index"))

	if flag_stamp:
		# Before the ALTERs, as Frappe drops the index of a field whose search_index is 0 at its next
		# sync. The first ALTER commits it (DDL commits the open transaction).
		db.flag_stamp_search_index(field["name"])
		log(f"  {STAMP}: search_index set on its Custom Field, so Frappe keeps its index")
	if not statements:
		log("  ready: nothing to change")
		return []

	ran = []
	run = {"lock_wait": lock_wait, "attempts": attempts, "log": log, "sleep": sleep, "ran": ran}
	previous = db.get_lock_wait_timeout()
	db.set_lock_wait_timeout(lock_wait)
	log(f"  lock_wait_timeout = {lock_wait} s for this session (was {previous})")
	try:
		for name, kind in statements:
			if kind == "column":
				_add_column(db, name, run)
			elif kind == "unique":
				_add_index(db, add_unique_index_sql(name), name, run)
			else:
				_add_index(db, add_stamp_index_sql(), name, run)
	except BaseException:
		_restore_lock_wait(db, previous, log)
		raise
	db.set_lock_wait_timeout(previous)
	log(f"  lock_wait_timeout back to {previous}")

	if still := unready(read_state(db)):
		raise KeyColumnsError(
			"The ALTERs ran but the table is not ready; nothing further was changed. Re-run, and if "
			f"this repeats, see {DOCS}:\n- " + "\n- ".join(still)
		)
	log("  ready: Frappe's sync will not alter these columns")
	return ran


def _restore_lock_wait(db, previous, log):
	"""On the way out of a failure: keep that failure, and only report a restore that fails too."""
	try:
		db.set_lock_wait_timeout(previous)
		log(f"  lock_wait_timeout back to {previous}")
	except Exception as e:
		log(f"  could not set lock_wait_timeout back to {previous}: {e}")


def _add_column(db, name, run):
	refusals = []
	for algorithm in ("INSTANT", "NOCOPY"):
		try:
			return _alter(db, add_column_sql(name, algorithm), run)
		except KeyColumnsError:
			raise
		except Exception as e:
			if error_code(e) not in REFUSED:
				raise
			refusals.append(f"{algorithm}: {e}")
			run["log"](f"    refused: {e}")
	raise KeyColumnsError(
		f"MariaDB would add {name} only by rebuilding the table ({'; '.join(refusals)}). Nothing was "
		f"changed for it. Check @@innodb_instant_alter_column_allowed ({DOCS}); do not add the column "
		"without an ALGORITHM clause on a busy table."
	)


def _add_index(db, statement, name, run):
	try:
		return _alter(db, statement, run)
	except KeyColumnsError:
		raise
	except Exception as e:
		code = error_code(e)
		if code == ER_DUP_ENTRY:
			raise KeyColumnsError(
				f"{name} holds one value on two rows, so it cannot be made unique ({e}). Nothing was "
				f"changed for the index. Find them with: SELECT `{name}`, COUNT(*) FROM `{TABLE}` WHERE "
				f"`{name}` IS NOT NULL GROUP BY 1 HAVING COUNT(*) > 1 ({DOCS})."
			) from e
		if code in REFUSED:
			raise KeyColumnsError(
				f"MariaDB will not build the index on {name} in place ({e}). Nothing was changed for it "
				f"({DOCS})."
			) from e
		raise


def _alter(db, statement, run):
	"""One ALTER, retried after a pause while it fails on a lock (it changed nothing then)."""
	log, attempts = run["log"], run["attempts"]
	log(f"  {statement}")
	for attempt in range(1, attempts + 1):
		started = time.monotonic()
		try:
			db.alter(statement)
		except Exception as e:
			code = error_code(e)
			if code not in LOCK_ERRORS:
				raise
			waited = time.monotonic() - started
			if attempt == attempts:
				done = "".join(f"\n  {earlier}" for earlier in run["ran"]) or " nothing"
				raise KeyColumnsError(
					f"Gave up after {attempts} tries of: {statement}\n"
					f"Each try waited up to {run['lock_wait']} s for the table's metadata lock (last: "
					f"MariaDB {code} after {waited:.1f} s) and changed nothing: a transaction kept "
					"Delivery Note open (a long request or job, an open console, a dump). Done in this "
					f"run:{done}\nRe-run when it is quiet; information_schema.innodb_trx lists the open "
					f"transactions ({DOCS})."
				) from e
			pause = min(FIRST_BACKOFF_SECONDS * 2 ** (attempt - 1), MAX_BACKOFF_SECONDS)
			log(
				f"    MariaDB {code} after {waited:.1f} s: nothing changed; try {attempt + 1} of "
				f"{attempts} in {pause} s"
			)
			run["sleep"](pause)
		else:
			log(f"    done in {time.monotonic() - started:.2f} s")
			run["ran"].append(statement)
			return statement


def error_code(error):
	"""MariaDB's error number behind an exception, or None. pymysql puts it first in args; Frappe
	raises lock-wait timeouts and deadlocks as QueryTimeoutError / QueryDeadlockError(<pymysql error>)."""
	seen = set()
	while error is not None and id(error) not in seen:
		seen.add(id(error))
		first = error.args[0] if error.args else None
		if isinstance(first, int) and not isinstance(first, bool):
			return first
		error = first if isinstance(first, BaseException) else error.__cause__
	return None


# ---- the site ---------------------------------------------------------------------------------------
class FrappeDB:
	"""ensure()'s view of the site's database, through frappe.db."""

	def __init__(self):
		import frappe

		self._frappe = frappe
		self._db = frappe.db

	def stamp_field(self):
		return self._db.get_value(
			"Custom Field",
			{"dt": "Delivery Note", "fieldname": STAMP},
			["name", "unique", "search_index"],
			as_dict=True,
		)

	def flag_stamp_search_index(self, name):
		self._db.set_value("Custom Field", name, "search_index", 1)

	def columns(self, names):
		rows = self._db.sql(
			"""select column_name, column_type, is_nullable, column_default, column_key
			from information_schema.columns
			where table_schema = database() and table_name = %s and column_name in %s""",
			(TABLE, tuple(names)),
		)
		return {
			name: Column(name, ctype.lower(), nullable == "YES", _default(default), key or "")
			for name, ctype, nullable, default, key in rows
		}

	def indexes(self):
		rows = self._db.sql(
			"""select index_name, non_unique, column_name
			from information_schema.statistics
			where table_schema = database() and table_name = %s
			order by index_name, seq_in_index""",
			(TABLE,),
		)
		found = {}
		for name, non_unique, column in rows:
			unique, columns = found.get(name, (not int(non_unique), ()))
			found[name] = (unique, (*columns, column))
		return [Index(name, unique, columns) for name, (unique, columns) in found.items()]

	def get_lock_wait_timeout(self):
		return int(self._db.sql("select @@session.lock_wait_timeout")[0][0])

	def set_lock_wait_timeout(self, seconds):
		self._db.sql(f"set session lock_wait_timeout = {int(seconds)}")

	def alter(self, statement):
		self._db.sql_ddl(statement)  # commits the open transaction first, as MariaDB does for DDL
		self._frappe.cache.hdel("table_columns", TABLE)  # Frappe's cached column list for the table


def _default(value):
	"""information_schema shows a NULL default as NULL or, from MariaDB 10.2.7, as the text 'NULL'."""
	return None if value is None or str(value).upper() == "NULL" else str(value)


def run(lock_wait=LOCK_WAIT_SECONDS, attempts=ATTEMPTS):
	"""bench --site <site> execute fuelbuddy_crm.dn_key_columns.run [--kwargs "{'lock_wait': 5, 'attempts': 5}"]

	ensure() at a maintenance moment of your choosing (docs/dn-key-columns.md). Exits non-zero with
	the reason when it stops. bench execute retries a method that raises by eval()-ing its path,
	which buries the reason under a NameError; SystemExit is not an Exception, so it gets out."""
	try:
		ensure(FrappeDB(), lock_wait=int(lock_wait), attempts=int(attempts))
	except KeyColumnsError as e:
		raise SystemExit(f"STOPPED: {e}") from None
	except Exception:
		traceback.print_exc()
		raise SystemExit(
			"STOPPED: the error above. The table is valid (MariaDB applies an ALTER whole or not at "
			f"all); check it, then re-run ({DOCS})."
		) from None
