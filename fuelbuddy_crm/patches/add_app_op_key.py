# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Purchase Receipt and Stock Entry.custom_app_op_key, uniquely indexed (IDEV-3201).

The key ERP accepts once per app operation (see fuelbuddy_crm.op_key). Both tables are large and
written all day, so the schema change is made here step by step rather than by create_custom_fields,
whose single ALTER adds the column and its unique key together (a table rebuild) and whose table
sync waits for the metadata lock as long as it takes:

1. The column goes in alone with ALGORITHM=INSTANT, a metadata-only change. Where MariaDB cannot do
   that, the statement fails instead of rebuilding the table.
2. The unique index is built online (ALGORITHM=INPLACE, LOCK=NONE), so posting carries on while it
   builds. Existing rows hold NULL and NULLs never clash, so nothing needs a backfill.
3. Each statement waits at most LOCK_WAIT_SECONDS for the table's metadata lock. While an ALTER
   waits, every new query on that table queues behind it; past the cap the ALTER gives up having
   changed nothing, and the patch fails.
4. The Custom Field records go in last, without Frappe's table sync: the column and index already
   match the field, and the sync would also apply any other pending schema change on these tables,
   uncapped. No field is registered until both indexes exist, so ERP never takes a key it cannot
   keep unique.

Every step checks information_schema first, so the patch is safe to re-run: after a failure,
re-running bench migrate finishes what is left. The index is named after the field, as Frappe
names the unique indexes it makes, so a later sync of these tables finds it and leaves it alone.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field, create_custom_fields

from fuelbuddy_crm.op_key import OP_KEY_DOCTYPES, OP_KEY_FIELD

LOCK_WAIT_SECONDS = 5

FIELD = {
	"fieldname": OP_KEY_FIELD,
	"fieldtype": "Data",
	"label": "App Op Key",
	"insert_after": "remarks",
	"unique": 1,
	"no_copy": 1,
	"read_only": 1,
	"print_hide": 1,
	"description": "Key of the FuelBuddy app operation that posted this document. ERP accepts each "
	"key once; an amendment starts without it (IDEV-3201).",
}


def execute():
	if frappe.db.db_type != "mariadb":
		create_custom_fields({doctype: [FIELD] for doctype in OP_KEY_DOCTYPES}, ignore_validate=True)
		return

	previous = frappe.db.sql("select @@session.lock_wait_timeout")[0][0]
	frappe.db.sql("set session lock_wait_timeout = %s", LOCK_WAIT_SECONDS)
	try:
		for doctype in OP_KEY_DOCTYPES:
			_add_column(f"tab{doctype}")
			_add_unique_index(f"tab{doctype}")
	finally:
		frappe.db.sql("set session lock_wait_timeout = %s", previous)

	_register_fields()


def _add_column(table):
	if _has_column(table):
		return
	_ddl(
		table,
		f"alter table `{table}` add column if not exists `{OP_KEY_FIELD}` varchar({frappe.db.VARCHAR_LEN}), "
		"algorithm=instant",
	)


def _add_unique_index(table):
	if _has_unique_index(table):
		return
	_ddl(
		table,
		f"alter table `{table}` add unique index if not exists `{OP_KEY_FIELD}` (`{OP_KEY_FIELD}`), "
		"algorithm=inplace, lock=none",
	)
	if not _has_unique_index(table):
		# IF NOT EXISTS matches on the name: an older index called this is kept as it is.
		frappe.throw(
			f"{table} has an index named {OP_KEY_FIELD} that is not a unique index on {OP_KEY_FIELD} "
			"alone. Drop or rename it, then re-run bench migrate."
		)


def _ddl(table, statement):
	try:
		frappe.db.sql_ddl(statement)
	except Exception:
		print(
			f"add_app_op_key: {statement!r} failed and changed nothing (lock wait capped at "
			f"{LOCK_WAIT_SECONDS}s). The patch is safe to re-run: run bench migrate again."
		)
		raise
	finally:
		frappe.cache.hdel("table_columns", table)


def _has_column(table):
	return bool(
		frappe.db.sql(
			"""select 1 from information_schema.columns
			where table_schema = database() and table_name = %s and column_name = %s""",
			(table, OP_KEY_FIELD),
		)
	)


def _has_unique_index(table):
	"""A unique index on the key column alone."""
	return bool(
		frappe.db.sql(
			"""select index_name from information_schema.statistics
			where table_schema = database() and table_name = %s
			group by index_name
			having max(non_unique) = 0 and count(*) = 1 and max(column_name) = %s""",
			(table, OP_KEY_FIELD),
		)
	)


def _register_fields():
	# With in_create_custom_fields set, Custom Field.on_update skips frappe.db.updatedb, as it does
	# inside create_custom_fields until that function's own closing sync, which is left out here.
	previous = frappe.flags.in_create_custom_fields
	frappe.flags.in_create_custom_fields = True
	try:
		for doctype in OP_KEY_DOCTYPES:
			# A no-op where the Custom Field is already there (a re-run).
			create_custom_field(doctype, {**FIELD, "owner": "Administrator"}, ignore_validate=True)
	finally:
		frappe.flags.in_create_custom_fields = previous
	for doctype in OP_KEY_DOCTYPES:
		frappe.clear_cache(doctype=doctype)
