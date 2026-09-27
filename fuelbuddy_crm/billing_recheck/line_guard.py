"""One Sales Order line's billing events, each on a snapshot no older than the line (IDEV-3268).

Why. frappe runs a request in one REPEATABLE READ transaction. Its snapshot is taken at the first plain
read, long before a Delivery Note or Sales Invoice submit / cancel takes the Sales Order line lock
(ERPNext takes it in update_prevdoc_status). An event that started before another event on the same
line committed keeps reading the line from that older snapshot after it gets the lock, and three
read-then-write steps then overwrite the other event's result:

- ERPNext's billing walk, delivery_note.py:772 (update_billed_amount_based_on_so), and so the re-check
  that reproduces it (walk.changes);
- ERPNext's update_prevdoc_status, status_updater.py:438-455 (_update_children: a plain ``select
  sum(...)`` from the snapshot, then ``update ... set delivered_qty / billed_amt = <that literal>``);
- crm's Delivery Note -> Sales Invoice allocation (dn_invoice_link, plain reads by design).

Stock ERPNext has the same race, but its full-line refresh usually turns it into an error before the
late event commits: it locks every Delivery Note on the line (a deadlock with a Delivery Note event
that holds its own row), and it bumps every Delivery Note's ``modified`` (the late event's
check_if_latest then raises TimestampMismatchError). The re-check touches only the Delivery Notes whose
values change, so both accidents mostly stop happening: in the lab race harness (phase D) wrong billing
went from 6 of 10 rounds (stock) to 15 of 20 (re-check) for an invoice submit against a Delivery Note
cancel, and from 0 of 10 to 16 of 20 on a two-line invoice.

What. With a billing re-check switch on, a Delivery Note or Sales Invoice submit / cancel calls
``check`` from its before_submit / before_cancel hook, after validate and before it writes a row:

1. lock the document's Sales Order Item rows (``for update``, one statement, name order, the order
   dn_invoice_link.lock_so_lines uses);
2. compare them, column for column, with the same rows as this transaction's snapshot shows them.

Every committed billing event on a line writes that row (update_prevdoc_status sets delivered_qty /
billed_amt / returned_qty and ``modified``), so a difference means the snapshot misses a committed event
on the line. The event then stops with TimestampMismatchError, the error stock raises when its refresh
catches the race; nothing has been written, so the caller can simply retry. When the rows are equal the
snapshot already holds everything committed on the line, and the lock keeps it so until this
transaction ends. The lock is the one ERPNext takes later anyway, taken a little earlier.

Not covered: a change that does not write the Sales Order Item row (a credit note without
update_billed_amount_in_sales_order still enters the walk's invoiced sum; a direct SQL write), and a
draft invoice save (its allocation stays as dn_invoice_link documents it). Switched off, nothing here
runs and stock behaviour is unchanged.
"""

import frappe
from frappe import _

from fuelbuddy_crm.billing_recheck import config, observe

SOI = "Sales Order Item"
_ROWS = "select * from `tabSales Order Item` where name in %(names)s order by name"


def check(doc, method=None):
	"""Delivery Note / Sales Invoice before_submit and before_cancel (hooks.py)."""
	names = sorted({item.get("so_detail") for item in doc.get("items") or [] if item.get("so_detail")})
	if not names or not _active():
		return
	stale = stale_lines(names)
	if stale:
		observe.count("stale_line", ",".join(stale), doctype=doc.doctype, name=doc.name, method=method)
		frappe.throw(
			_(
				"Sales Order line {0} was changed by another transaction after this request started. "
				"Nothing was saved; please try again."
			).format(", ".join(stale)),
			frappe.TimestampMismatchError,
			title=_("Please retry"),
		)


def stale_lines(names):
	"""Lock the Sales Order Item rows ``names`` and return those whose latest committed version is not
	the one this transaction's snapshot shows."""
	params = {"names": tuple(sorted(names))}
	seen = {row.name: row for row in frappe.db.sql(_ROWS, params, as_dict=True)}
	latest = {row.name: row for row in frappe.db.sql(_ROWS + " for update", params, as_dict=True)}
	return sorted(name for name in set(seen) | set(latest) if seen.get(name) != latest.get(name))


def _active():
	if frappe.flags.in_install or frappe.flags.in_migrate:
		return False
	# the dubai back-dated drain switches ERPNext's billing recompute off; stay off with it, as
	# dn_invoice_link does
	if getattr(frappe.flags, "fb_skip_billing_status", False):
		return False
	try:
		return bool(config.walk_enabled() or config.bulk_over())
	except Exception:
		return False
