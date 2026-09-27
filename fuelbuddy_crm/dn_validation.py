# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Delivery Note application-level dedup.

`custom_invoiced_item_id` links a DN to its Hasura invoiced_item. It is deliberately
NOT a unique DB index on prod: a versioned amendment (cancel the original, create a
"+1" version) reuses the SAME id across the cancelled original and the live version,
which a single-column unique index would reject.

So enforce the true invariant in application code instead: at most ONE non-cancelled
Delivery Note (docstatus < 2) may exist per custom_invoiced_item_id. Cancelled DNs
(docstatus 2) don't count, so versioning is allowed; a genuine duplicate — two live
DNs for one invoiced_item — is blocked.

enforce_single_active_dn is a plain read at validate time, which two concurrent inserts
both pass: Frappe takes the naming-series row lock before validate, so the second insert
waits for the first to commit, then validates against the snapshot its transaction opened
before that commit (REPEATABLE READ) and does not see the first DN. That produced 607 twin
drafts in production between 1 Jul and 26 Sep 2026 (IDEV-3269). The database therefore holds
the invariant too: custom_live_invoiced_item_id (set_live_invoiced_item_key) carries
custom_invoiced_item_id only while the DN is live and is uniquely indexed, so the second
live DN is refused by MariaDB. enforce_single_active_dn stays for the clear message on a
sequential duplicate and for DNs made before the key existed (their key is NULL).
"""

import frappe
from frappe import _
from frappe.utils import flt

STAMP_FIELD = "custom_invoiced_item_id"
# Unique; STAMP_FIELD while the DN is live (docstatus < 2, not a return), NULL otherwise.
# Created by patches/add_dn_live_invoiced_item_key.py.
LIVE_KEY_FIELD = "custom_live_invoiced_item_id"


def enforce_single_active_dn(doc, method=None):
	iid = doc.get("custom_invoiced_item_id")
	if not iid:
		return  # hand-built / non-punch DNs without the link are not deduped here

	if doc.get("is_return"):
		# A Sales Return against the DN copies custom_invoiced_item_id (the field is not
		# no_copy) but is a negative-qty companion doc, not a second punch — never a duplicate.
		return

	existing = frappe.db.get_value(
		"Delivery Note",
		{
			"custom_invoiced_item_id": iid,
			"name": ["!=", doc.name or ""],
			"docstatus": ["<", 2],  # exclude cancelled -> versioned amendments are allowed
			"is_return": ["!=", 1],  # a live return must not block a legitimate re-punch/amend
		},
		"name",
	)
	if existing:
		frappe.throw(
			_("A non-cancelled Delivery Note ({0}) already exists for invoiced_item {1}.").format(
				existing, iid
			),
			title=_("Duplicate Delivery Note"),
		)


def set_live_invoiced_item_key(doc, method=None):
	"""Delivery Note validate / before_cancel -> LIVE_KEY_FIELD = STAMP_FIELD while the DN is
	live, else None.

	validate covers insert, draft saves and submit (docstatus 0 or 1: keyed); before_cancel
	runs after Frappe set docstatus 2 and before it writes the row, so a cancel frees the key
	in the same UPDATE, and a versioned amendment inserted afterwards -- even in the same
	transaction -- takes it. A deleted draft takes its key with it. Returns are never keyed.
	Recomputed every time, so a value copied by Amend / copy_doc (no_copy is ignored there)
	never survives."""
	iid = doc.get(STAMP_FIELD)
	live = iid and doc.docstatus < 2 and not doc.get("is_return")
	doc.set(LIVE_KEY_FIELD, iid if live else None)


def _so_details(doc):
	"""Distinct so_detail row names referenced by this Delivery Note."""
	return {row.so_detail for row in doc.items if row.get("so_detail")}


def _drafted_qty(so_detail, exclude_dn=None):
	"""Total qty (in the SO line's transaction UOM) reserved by DRAFT Delivery Notes
	against one Sales Order Item, optionally ignoring one DN."""
	return flt(
		frappe.db.sql(
			"""
			select coalesce(sum(dni.qty), 0)
			from `tabDelivery Note Item` dni
			inner join `tabDelivery Note` dn on dn.name = dni.parent
			where dni.so_detail = %s and dn.docstatus = 0 and dn.name != %s
			""",
			(so_detail, exclude_dn or ""),
		)[0][0]
	)


def sync_draft_reservation(doc, method=None):
	"""Delivery Note on_update / on_submit / on_cancel / on_trash -> keep
	`Sales Order Item.custom_delivery_note_qty_in_draft` equal to the qty actually held by
	draft Delivery Notes.

	The allocator (erp-functions `remainingLitres`) computes headroom as
	`qty - delivered_qty - custom_delivery_note_qty_in_draft + returned_qty`, so a draft
	DN that doesn't reserve is invisible and the next punch sees the full order again.

	This replaces the "Sales Order updated with Draft qty of delivery note" Server Script,
	which ran on After Save ONLY -- so it never released the reservation when a draft DN
	was cancelled or deleted, and left SO lines reserved forever."""
	for so_detail in _so_details(doc):
		# This DN's own rows are excluded from the SQL and added back only while it is
		# still a live draft -- on_trash runs before the rows are gone, and on_submit /
		# on_cancel run before/after a docstatus change the SQL may not see yet.
		drafted = _drafted_qty(so_detail, exclude_dn=doc.name)
		if doc.docstatus == 0 and method != "on_trash":
			drafted += flt(sum(flt(r.qty) for r in doc.items if r.get("so_detail") == so_detail))
		frappe.db.set_value(
			"Sales Order Item", so_detail, "custom_delivery_note_qty_in_draft", drafted,
			update_modified=False,
		)


def _own_drafted_qty(dn_name, so_detail):
	"""Qty this Delivery Note ALREADY has persisted against one SO line (0 when new)."""
	if not dn_name:
		return 0.0
	return flt(
		frappe.db.sql(
			"""
			select coalesce(sum(qty), 0) from `tabDelivery Note Item`
			where parent = %s and so_detail = %s
			""",
			(dn_name, so_detail),
		)[0][0]
	)


def enforce_so_headroom(doc, method=None):
	"""Delivery Note validate -> refuse to consume more Sales Order qty than is left.

	ERPNext's own over-delivery gate is effectively off here (Stock Settings
	`over_delivery_receipt_allowance` = 1000, i.e. 1000% is tolerated), and the allocator's
	headroom check is client-side and racy, so this is the authoritative guard.

	Only the INCREASE in consumption is checked, against the headroom as it stands right
	now (all live drafts, this one included). So a brand-new punch is checked in full, a
	draft whose qty is raised is checked on the delta -- and an existing draft that is
	merely being submitted consumes nothing new and passes. That last case matters: the
	site is sitting on a ~55k draft-DN backlog, ~17k of which already over-subscribe their
	Sales Order; this guard exists to stop NEW over-punching, not to wedge the drain.
	Returns are negative-qty companion docs and are never capped."""
	if doc.get("is_return"):
		return

	wanted = {}
	for row in doc.items:
		if row.get("so_detail"):
			wanted[row.so_detail] = wanted.get(row.so_detail, 0) + flt(row.qty)

	for so_detail, punching in wanted.items():
		increase = punching - _own_drafted_qty(doc.name, so_detail)
		# 0.001 slack: qty is a Float and litres/conversion_factor rarely divides evenly.
		if increase <= 0.001:
			continue  # consuming nothing new (submit of an existing draft, or a reduction)

		so_item = frappe.db.get_value(
			"Sales Order Item",
			so_detail,
			["parent", "item_code", "qty", "delivered_qty", "returned_qty", "uom"],
			as_dict=True,
		)
		if not so_item:
			continue  # dangling link; enforce_single_active_dn / ERPNext handle that

		available = (
			flt(so_item.qty)
			- flt(so_item.delivered_qty)
			+ flt(so_item.returned_qty)
			- _drafted_qty(so_detail)
		)
		if increase - available > 0.001:
			frappe.throw(
				_(
					"Delivery Note qty {0} {1} for {2} exceeds what Sales Order {3} has left"
					" ({4} {1}: ordered {5}, delivered {6}, already held by draft Delivery"
					" Notes {7})."
				).format(
					increase,
					so_item.uom,
					so_item.item_code,
					so_item.parent,
					available,
					flt(so_item.qty),
					flt(so_item.delivered_qty),
					_drafted_qty(so_detail),
				),
				title=_("Delivery Note qty exceeds Sales Order"),
			)
