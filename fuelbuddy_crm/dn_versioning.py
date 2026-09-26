# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Delivery Note amendment versioning.

`custom_version` is no_copy=1 with default "1", so a native ERPNext UI amend
(cancel -> Amend) strips the field and the new "-1" doc falls back to "1" —
the version chain silently resets. The erp-functions backend amend path sets
the version explicitly, but the ERP must enforce the invariant for ALL sources:

    first DN of a chain -> "1"; every amendment -> parent's version + 1.

Enforced here on before_insert, recomputed from the amended_from parent even
when the caller supplied a value — the parent is the single source of truth,
so a backend stale read can't fork the chain either.
"""

import re

import frappe


def set_amended_version(doc, method=None):
	if not doc.get("amended_from"):
		return  # first DN of the chain: field default "1" applies

	parent_version = frappe.db.get_value("Delivery Note", doc.amended_from, "custom_version")
	# Lenient parse ("7abc" -> 7), matching the backend's tolerance for dirty data.
	match = re.match(r"\d+", str(parent_version or ""))
	prev = int(match.group()) if match else 1
	doc.custom_version = str(prev + 1)


# ---- quantity-correction idempotency key (IDEV-3266) ---------------------------------------------
# The episode key amend_delivery_note (fuelbuddy_crm.api.qty_correction) puts on the live Delivery
# Note it produces. Lives here, beside set_amended_version, because the hook below is a Delivery
# Note before_insert hook like it; api/qty_correction imports the field name from here.
QC_IDEMPOTENCY_KEY_FIELD = "custom_qc_idempotency_key"


def drop_copied_idempotency_key(doc, method=None):
	"""Delivery Note before_insert: only amend_delivery_note may put an episode key on a new
	Delivery Note. ERPNext's Amend copies no_copy fields too, so a UI amendment of a corrected
	Delivery Note would otherwise inherit its key and fail the unique index."""
	if doc.get(QC_IDEMPOTENCY_KEY_FIELD) and doc.flags.get("qc_idempotency_key") != doc.get(
		QC_IDEMPOTENCY_KEY_FIELD
	):
		doc.set(QC_IDEMPOTENCY_KEY_FIELD, None)
