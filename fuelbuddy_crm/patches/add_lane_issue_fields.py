# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""The stock lane's Issue fields, Issue Types, SLAs and the lane-owned Delivery Note flag (IDEV-3201).

See fuelbuddy_crm.lane_issue for what each piece is for. In order:

0. A read-only check. The lane SLAs only work with Support Settings > Track Service Level Agreement
   on, and ERPNext refuses to save an enabled Issue SLA while it is off. Turning it on also starts
   every other enabled Issue SLA: a default one applies to every Issue, and one with no customer
   scope to every Issue its condition matches. So where tracking is off and any enabled Issue SLA
   other than the lane's exists, the patch stops here, changing nothing, and names them. A person
   decides; bench migrate then runs the patch again.
1. The columns, the add_app_op_key way: Issue and Delivery Note are large and written all day, and
   create_custom_fields would add the columns (and the unique key) in one ALTER that may rebuild the
   table, behind an uncapped metadata-lock wait. Instead each table gets its missing columns in one
   ALGORITHM=INSTANT statement, then the unique index on custom_app_issue_key is built online
   (ALGORITHM=INPLACE, LOCK=NONE). Every statement waits at most LOCK_WAIT_SECONDS for the table's
   metadata lock and otherwise fails having changed nothing. Existing rows hold NULL (or 0), so
   nothing needs a backfill, and NULL keys never clash.
2. The Custom Fields, without Frappe's table sync (the columns already match them). No field is
   registered until every column and the unique index exist, so no save ever meets a field without
   its column. custom_held_changes is a Table field: its rows live in `tabLane Held Change`, which
   the model sync built before this post_model_sync patch.
3. Issue Priority Medium (an ERPNext setup record, created only if missing), the four lane Issue
   Types, the empty Holiday List "FuelBuddy Lane 24x7", tracking on, and one SLA per lane Issue
   Type. A record that already exists is left as it is, so a time a person has tuned is kept.

Every step checks first, so the patch is safe to re-run: after a failure, bench migrate finishes
what is left.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field, create_custom_fields

from fuelbuddy_crm import lane_issue as lane

LOCK_WAIT_SECONDS = 5
HOUR = 3600

ISSUE_FIELDS = [
	{
		"fieldname": lane.LANE_TEAM_FIELD,
		"fieldtype": "Select",
		"label": "Lane Team",
		"options": "\n" + "\n".join(lane.LANE_TEAMS),
		"insert_after": "issue_type",
		"read_only": 1,
		"description": "The team that resolves this stock lane Issue (IDEV-3201).",
	},
	{
		"fieldname": lane.ISSUE_KEY_FIELD,
		"fieldtype": "Data",
		"label": "App Issue Key",
		"insert_after": lane.LANE_TEAM_FIELD,
		"unique": 1,
		"no_copy": 1,
		"read_only": 1,
		"print_hide": 1,
		"description": "Key of the FuelBuddy stock lane cause behind this Issue. ERP keeps one Issue "
		"per key; the app reopens it rather than raise another (IDEV-3201).",
	},
	{
		"fieldname": lane.ROW_KIND_FIELD,
		"fieldtype": "Data",
		"label": "App Row Kind",
		"insert_after": lane.ISSUE_KEY_FIELD,
		"read_only": 1,
		"description": "DELIVERY_NOTE, GRN, TRANSFER, MATERIAL_ISSUE or AMEND.",
	},
	{
		"fieldname": lane.ROW_ID_FIELD,
		"fieldtype": "Data",
		"label": "App Row ID",
		"insert_after": lane.ROW_KIND_FIELD,
		"read_only": 1,
	},
	{
		"fieldname": lane.HELD_COUNT_FIELD,
		"fieldtype": "Int",
		"label": "Held Changes",
		"insert_after": lane.ROW_ID_FIELD,
		"read_only": 1,
		"description": "How many later app changes this Issue's request holds back.",
	},
	{
		"fieldname": lane.HELD_CHANGES_FIELD,
		"fieldtype": "Table",
		"label": "Held Changes List",
		"options": lane.HELD_CHANGE_DOCTYPE,
		"insert_after": lane.HELD_COUNT_FIELD,
		"read_only": 1,
	},
]

DN_FIELDS = [
	{
		"fieldname": lane.LANE_OWNED_FIELD,
		"fieldtype": "Check",
		"label": "App Lane Owned",
		"insert_after": "custom_invoiced_item_id",
		"no_copy": 1,
		"read_only": 1,
		"print_hide": 1,
		"description": "Made by the FuelBuddy stock lane, which drafts and submits it in turn. Other "
		"writers leave it alone (IDEV-3201).",
	},
]

CUSTOM_FIELDS = {"Issue": ISSUE_FIELDS, "Delivery Note": DN_FIELDS}

# Column definitions as Frappe builds them for these fieldtypes (frappe.database.schema), so its later
# syncs of these tables find nothing to change. The Table field has no column.
VARCHAR = "varchar({})"
COLUMNS = {
	"Issue": [
		(lane.LANE_TEAM_FIELD, VARCHAR),
		(lane.ISSUE_KEY_FIELD, VARCHAR),
		(lane.ROW_KIND_FIELD, VARCHAR),
		(lane.ROW_ID_FIELD, VARCHAR),
		(lane.HELD_COUNT_FIELD, "int(11) not null default 0"),
	],
	"Delivery Note": [(lane.LANE_OWNED_FIELD, "int(1) not null default 0")],
}
UNIQUE_INDEXES = {"Issue": [lane.ISSUE_KEY_FIELD]}


def execute():
	_check_sla_tracking()

	if frappe.db.db_type != "mariadb":
		create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
	else:
		previous = frappe.db.sql("select @@session.lock_wait_timeout")[0][0]
		frappe.db.sql("set session lock_wait_timeout = %s", LOCK_WAIT_SECONDS)
		try:
			for doctype, columns in COLUMNS.items():
				table = f"tab{doctype}"
				_add_columns(table, columns)
				for fieldname in UNIQUE_INDEXES.get(doctype, ()):
					_add_unique_index(table, fieldname)
		finally:
			frappe.db.sql("set session lock_wait_timeout = %s", previous)
		_register_fields()

	_ensure_issue_priority()
	_ensure_issue_types()
	_ensure_holiday_list()
	_enable_sla_tracking()
	_ensure_slas()


# 0. The read-only check


def other_enabled_issue_slas():
	"""Enabled Issue SLAs that are not the lane's, which turning tracking on would start applying."""
	lane_levels = set(lane.ISSUE_TYPES)
	return [
		sla.name
		for sla in frappe.get_all(
			"Service Level Agreement",
			filters={"document_type": "Issue", "enabled": 1},
			fields=["name", "service_level"],
			order_by="name asc",
		)
		if sla.service_level not in lane_levels
	]


def _check_sla_tracking():
	if frappe.db.get_single_value("Support Settings", "track_service_level_agreement"):
		return
	if others := other_enabled_issue_slas():
		frappe.throw(
			"add_lane_issue_fields stopped before changing anything. The lane's Issue SLAs need Support "
			"Settings > Track Service Level Agreement on, and it is off while these enabled Issue SLAs "
			f"exist: {', '.join(others)}. Turning tracking on would start applying them to every Issue "
			"they match, not only the lane's. A person decides: disable them, or turn tracking on by "
			"hand knowing they will apply. Then run bench migrate again."
		)


# 1. The columns


def _add_columns(table, columns):
	missing = [(name, definition) for name, definition in columns if not _has_column(table, name)]
	if not missing:
		return
	clauses = ", ".join(
		f"add column if not exists `{name}` {definition.format(frappe.db.VARCHAR_LEN)}"
		for name, definition in missing
	)
	_ddl(table, f"alter table `{table}` {clauses}, algorithm=instant")


def _add_unique_index(table, fieldname):
	if _has_unique_index(table, fieldname):
		return
	_ddl(
		table,
		f"alter table `{table}` add unique index if not exists `{fieldname}` (`{fieldname}`), "
		"algorithm=inplace, lock=none",
	)
	if not _has_unique_index(table, fieldname):
		# IF NOT EXISTS matches on the name: an older index called this is kept as it is.
		frappe.throw(
			f"{table} has an index named {fieldname} that is not a unique index on {fieldname} "
			"alone. Drop or rename it, then re-run bench migrate."
		)


def _ddl(table, statement):
	try:
		frappe.db.sql_ddl(statement)
	except Exception:
		print(
			f"add_lane_issue_fields: {statement!r} failed and changed nothing (lock wait capped at "
			f"{LOCK_WAIT_SECONDS}s). The patch is safe to re-run: run bench migrate again."
		)
		raise
	finally:
		frappe.cache.hdel("table_columns", table)


def _has_column(table, column):
	return bool(
		frappe.db.sql(
			"""select 1 from information_schema.columns
			where table_schema = database() and table_name = %s and column_name = %s""",
			(table, column),
		)
	)


def _has_unique_index(table, column):
	"""A unique index on the column alone."""
	return bool(
		frappe.db.sql(
			"""select index_name from information_schema.statistics
			where table_schema = database() and table_name = %s
			group by index_name
			having max(non_unique) = 0 and count(*) = 1 and max(column_name) = %s""",
			(table, column),
		)
	)


# 2. The Custom Fields


def _register_fields():
	# With in_create_custom_fields set, Custom Field.on_update skips frappe.db.updatedb, as it does
	# inside create_custom_fields until that function's own closing sync, which is left out here.
	previous = frappe.flags.in_create_custom_fields
	frappe.flags.in_create_custom_fields = True
	try:
		for doctype, fields in CUSTOM_FIELDS.items():
			for field in fields:
				# A no-op where the Custom Field is already there (a re-run).
				create_custom_field(doctype, {**field, "owner": "Administrator"}, ignore_validate=True)
	finally:
		frappe.flags.in_create_custom_fields = previous
	for doctype in CUSTOM_FIELDS:
		frappe.clear_cache(doctype=doctype)


# 3. Issue Types, the calendar and the SLAs


def _insert_if_missing(doctype, name, doc):
	if frappe.db.exists(doctype, name):
		return False
	frappe.get_doc({"doctype": doctype, **doc}).insert(ignore_permissions=True)
	return True


def _ensure_issue_priority():
	_insert_if_missing("Issue Priority", lane.SLA_PRIORITY, {"name": lane.SLA_PRIORITY})


def _ensure_issue_types():
	for issue_type in lane.ISSUE_TYPES:
		_insert_if_missing(
			"Issue Type",
			issue_type,
			{"name": issue_type, "description": lane.ISSUE_TYPE_DESCRIPTIONS[issue_type]},
		)


def _ensure_holiday_list():
	_insert_if_missing(
		"Holiday List",
		lane.HOLIDAY_LIST,
		{
			"holiday_list_name": lane.HOLIDAY_LIST,
			"from_date": lane.HOLIDAY_LIST_FROM,
			"to_date": lane.HOLIDAY_LIST_TO,
			"holidays": [],
		},
	)


def _enable_sla_tracking():
	if not frappe.db.get_single_value("Support Settings", "track_service_level_agreement"):
		frappe.db.set_single_value("Support Settings", "track_service_level_agreement", 1)


def sla_doc(issue_type):
	response_hours, resolution_hours = lane.ISSUE_TYPES[issue_type]
	return {
		"service_level": issue_type,
		"document_type": "Issue",
		"enabled": 1,
		"default_service_level_agreement": 0,
		"default_priority": lane.SLA_PRIORITY,
		"holiday_list": lane.HOLIDAY_LIST,
		"condition": lane.sla_condition(issue_type),
		"apply_sla_for_resolution": 1,
		"priorities": [
			{
				"priority": lane.SLA_PRIORITY,
				"default_priority": 1,
				"response_time": response_hours * HOUR,
				"resolution_time": resolution_hours * HOUR,
			}
		],
		"support_and_resolution": [
			{"workday": day, "start_time": lane.DAY_START, "end_time": lane.DAY_END} for day in lane.WEEKDAYS
		],
		"sla_fulfilled_on": [{"status": status} for status in lane.SLA_FULFILLED_ON],
	}


def _ensure_slas():
	for issue_type in lane.ISSUE_TYPES:
		if frappe.db.exists(
			"Service Level Agreement", {"document_type": "Issue", "service_level": issue_type}
		):
			continue
		frappe.get_doc({"doctype": "Service Level Agreement", **sla_doc(issue_type)}).insert(
			ignore_permissions=True
		)
