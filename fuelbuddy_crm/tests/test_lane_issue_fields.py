# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""The stock lane's Issue setup on a site (IDEV-3201).

bench --site <site> run-tests --app fuelbuddy_crm --module fuelbuddy_crm.tests.test_lane_issue_fields

Needs a site where patches/add_lane_issue_fields has run (bench migrate). Everything a test writes is
rolled back.
"""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, get_datetime, now_datetime, time_diff_in_seconds

from fuelbuddy_crm import lane_issue as lane
from fuelbuddy_crm.patches import add_lane_issue_fields as patch

HOUR = 3600


def _table(doctype):
	if frappe.db.db_type == "mariadb":
		from frappe.database.mariadb.schema import MariaDBTable as Table
	else:
		from frappe.database.postgres.schema import PostgresTable as Table
	table = Table(doctype)
	table.setup_table_columns()
	return table


class TestLaneIssueSetup(FrappeTestCase):
	def test_frappes_sync_finds_nothing_to_change_in_the_lane_columns(self):
		for doctype, columns in patch.COLUMNS.items():
			table = _table(doctype)
			for fieldname, _ in columns:
				with self.subTest(doctype=doctype, column=fieldname):
					column = table.columns[fieldname]
					current = table.current_columns.get(fieldname)
					self.assertIsNotNone(current, "column missing")
					column.build_for_alter_table(current)
			for pending in (
				"add_column",
				"change_type",
				"add_unique",
				"drop_unique",
				"add_index",
				"set_default",
			):
				with self.subTest(doctype=doctype, pending=pending):
					self.assertEqual([c.fieldname for c in getattr(table, pending)], [])

	def test_the_issue_key_has_its_unique_index(self):
		index = frappe.db.get_column_index("tabIssue", lane.ISSUE_KEY_FIELD, unique=True)
		self.assertIsNotNone(index)

	def test_the_fields(self):
		issue = frappe.get_meta("Issue", cached=False)
		key = issue.get_field(lane.ISSUE_KEY_FIELD)
		self.assertEqual((key.fieldtype, key.unique, key.no_copy, key.read_only), ("Data", 1, 1, 1))
		team = issue.get_field(lane.LANE_TEAM_FIELD)
		self.assertEqual(team.options.split("\n"), ["", *lane.LANE_TEAMS])
		self.assertEqual(issue.get_field(lane.HELD_COUNT_FIELD).fieldtype, "Int")
		held = issue.get_field(lane.HELD_CHANGES_FIELD)
		self.assertEqual((held.fieldtype, held.options), ("Table", lane.HELD_CHANGE_DOCTYPE))
		self.assertTrue(frappe.get_meta(lane.HELD_CHANGE_DOCTYPE).istable)

		owned = frappe.get_meta("Delivery Note", cached=False).get_field(lane.LANE_OWNED_FIELD)
		self.assertEqual((owned.fieldtype, owned.no_copy, owned.read_only), ("Check", 1, 1))

	def test_issue_types_calendar_and_slas(self):
		self.assertTrue(frappe.db.get_single_value("Support Settings", "track_service_level_agreement"))
		self.assertTrue(
			frappe.db.get_single_value("Support Settings", "allow_resetting_service_level_agreement")
		)
		self.assertEqual(frappe.db.count("Holiday", {"parent": lane.HOLIDAY_LIST}), 0)
		for issue_type, (response_hours, resolution_hours) in lane.ISSUE_TYPES.items():
			with self.subTest(issue_type=issue_type):
				self.assertTrue(frappe.db.exists("Issue Type", issue_type))
				sla = frappe.get_doc("Service Level Agreement", f"SLA-Issue-{issue_type}")
				self.assertEqual((sla.enabled, sla.default_service_level_agreement), (1, 0))
				self.assertEqual(sla.condition, lane.sla_condition(issue_type))
				self.assertEqual(sla.holiday_list, lane.HOLIDAY_LIST)
				self.assertEqual(len(sla.support_and_resolution), 7)
				self.assertEqual(sorted(r.status for r in sla.sla_fulfilled_on), ["Closed", "Resolved"])
				(priority,) = sla.priorities
				self.assertEqual(priority.priority, lane.SLA_PRIORITY)
				self.assertEqual(priority.response_time, response_hours * HOUR)
				self.assertEqual(priority.resolution_time, resolution_hours * HOUR)

	def test_a_lane_issue_takes_its_types_sla(self):
		issue = self._issue("Lane Approval Pending", "lane-approval-erp-fr-test-1")
		issue.reload()

		self.assertEqual(issue.service_level_agreement, "SLA-Issue-Lane Approval Pending")
		self.assertEqual(issue.priority, lane.SLA_PRIORITY)
		start = get_datetime(issue.service_level_agreement_creation or issue.creation)
		# 24/7 calendar: the deadline is 5 h of wall time away (each day ends at 23:59:59, so a
		# deadline that crosses midnight may move by a second).
		self.assertAlmostEqual(time_diff_in_seconds(issue.sla_resolution_by, start), 5 * HOUR, delta=2)
		self.assertAlmostEqual(time_diff_in_seconds(issue.response_by, start), 1 * HOUR, delta=2)
		self.assertEqual(issue.get(lane.HELD_COUNT_FIELD), 2)
		self.assertEqual(
			[(row.app_row_kind, row.app_row_id) for row in issue.get(lane.HELD_CHANGES_FIELD)],
			[("DELIVERY_NOTE", "dn-1"), ("MATERIAL_ISSUE", "task-1")],
		)

	def test_a_reopened_lane_issue_gets_fresh_sla_times_through_reset(self):
		# upsertErpLaneIssue's reopen path: status back to Open, then ERPNext's
		# reset_service_level_agreement, which refuses unless the patch turned resetting on.
		from erpnext.support.doctype.service_level_agreement.service_level_agreement import (
			reset_service_level_agreement,
		)

		issue = self._issue("Lane ERP Refusal", "lane-refusal-erp-dn-test-reset")
		self.assertEqual(issue.service_level_agreement, "SLA-Issue-Lane ERP Refusal")
		# The Issue was raised two days ago, so its 8 h resolution deadline is long past.
		frappe.db.set_value(
			"Issue",
			issue.name,
			{
				"service_level_agreement_creation": add_days(now_datetime(), -2),
				"opening_date": add_days(now_datetime(), -2),
			},
			update_modified=False,
		)
		issue.reload()
		issue.status = "Resolved"
		issue.save(ignore_permissions=True)
		issue.reload()
		issue.status = "Open"
		issue.save(ignore_permissions=True)
		issue.reload()
		self.assertLess(get_datetime(issue.sla_resolution_by), now_datetime())

		reset_service_level_agreement("Issue", issue.name, "lane test reopen", "Administrator")
		issue.reload()

		self.assertEqual(issue.service_level_agreement, "SLA-Issue-Lane ERP Refusal")
		start = get_datetime(issue.service_level_agreement_creation)
		self.assertLess(abs(time_diff_in_seconds(now_datetime(), start)), 120)
		self.assertAlmostEqual(time_diff_in_seconds(issue.sla_resolution_by, start), 8 * HOUR, delta=2)
		self.assertGreater(get_datetime(issue.sla_resolution_by), now_datetime())

	def test_an_issue_of_another_type_takes_no_lane_sla(self):
		issue = self._issue(None, None)
		self.assertNotIn(issue.service_level_agreement or "", [f"SLA-Issue-{t}" for t in lane.ISSUE_TYPES])

	def test_one_issue_per_key(self):
		self._issue("Lane ERP Refusal", "lane-refusal-erp-dn-test-2")
		with self.assertRaises((frappe.UniqueValidationError, frappe.DuplicateEntryError)):
			self._issue("Lane ERP Refusal", "lane-refusal-erp-dn-test-2")

	def test_running_the_patch_again_changes_nothing(self):
		before = {
			doctype: frappe.db.count(doctype)
			for doctype in ("Custom Field", "Issue Type", "Holiday List", "Service Level Agreement")
		}
		patch.execute()
		after = {doctype: frappe.db.count(doctype) for doctype in before}
		self.assertEqual(after, before)

	def _issue(self, issue_type, key):
		doc = {"doctype": "Issue", "subject": f"lane test {key}", "issue_type": issue_type}
		if key:
			doc.update(
				{
					lane.ISSUE_KEY_FIELD: key,
					lane.LANE_TEAM_FIELD: "Purchase",
					lane.ROW_KIND_FIELD: "GRN",
					lane.ROW_ID_FIELD: "fr-1",
					lane.HELD_COUNT_FIELD: 2,
					lane.HELD_CHANGES_FIELD: [
						{
							"app_row_kind": "DELIVERY_NOTE",
							"app_row_id": "dn-1",
							"fill_time": "2026-10-01 08:00:00",
						},
						{
							"app_row_kind": "MATERIAL_ISSUE",
							"app_row_id": "task-1",
							"fill_time": "2026-10-01 09:00:00",
						},
					],
				}
			)
		return frappe.get_doc(doc).insert(ignore_permissions=True)
