# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""What ERP holds for the FuelBuddy stock lane's Issues and lane-made Delivery Notes (IDEV-3201).

The stock lane posts delivery notes, receipts, transfers and own-use issues to ERP one at a time, in
fill-time order. When a change needs a person (an approval that is late, a write ERP refused for
good, a turn held past its target, or a daily check that found ERP and the app apart), erp-functions
upserts one Issue for it, keyed by custom_app_issue_key, and routes it to a team through
custom_lane_team. Each lane Issue type has its own Service Level Agreement, on a 24/7 calendar.

A Delivery Note the lane made carries custom_app_lane_owned = 1, so other writers (the draft drain)
can leave it alone.

patches/add_lane_issue_fields sets all of this up. This module only names it, so the patch, the
tests and any later code agree.
"""

ISSUE_KEY_FIELD = "custom_app_issue_key"
LANE_TEAM_FIELD = "custom_lane_team"
ROW_KIND_FIELD = "custom_app_row_kind"
ROW_ID_FIELD = "custom_app_row_id"
HELD_COUNT_FIELD = "custom_held_count"
HELD_CHANGES_FIELD = "custom_held_changes"
LANE_OWNED_FIELD = "custom_app_lane_owned"

HELD_CHANGE_DOCTYPE = "Lane Held Change"
LANE_TEAMS = ("Purchase", "Finance", "Tech")

# Issue Type -> (response hours, resolution hours) on its SLA.
# Lane Approval Pending resolves in 5 h: its Issue opens 1 h after capture, so that is 6 h from capture
# (owner rule). The other times are engineering defaults, to be tuned after go-live.
ISSUE_TYPES = {
	"Lane Approval Pending": (1, 5),
	"Lane ERP Refusal": (1, 8),
	"Lane Past Target": (1, 4),
	"Lane Check Mismatch": (4, 24),
}
ISSUE_TYPE_DESCRIPTIONS = {
	"Lane Approval Pending": "A captured receipt or transfer is waiting for approval and holds the stock lane.",
	"Lane ERP Refusal": "ERP refused a stock lane write for good; a person fixes the cause, then re-queues it.",
	"Lane Past Target": "A stock lane turn or a correction amend is past its target time.",
	"Lane Check Mismatch": "The daily check found a submitted document that differs from the app.",
}

SLA_PRIORITY = "Medium"
SLA_FULFILLED_ON = ("Resolved", "Closed")
HOLIDAY_LIST = "FuelBuddy Lane 24x7"
HOLIDAY_LIST_FROM = "2026-01-01"
HOLIDAY_LIST_TO = "2099-12-31"
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# Frappe needs start < end on a Service Day, so a day runs to 23:59:59.
DAY_START, DAY_END = "00:00:00", "23:59:59"


def sla_condition(issue_type):
	"""The SLA's condition: it applies to Issues of this type only."""
	return f"doc.issue_type == {issue_type!r}"
