# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Invoice hold while a quantity correction is open (IDEV-3266).

Called by erp-functions (Hasura actions ``openQcHold`` / ``closeQcHold``) for the
QuantityCorrectionWorkflow: it opens the hold right after a ticket episode's run starts and
closes it when the episode ends; the workflow's reconciler closes the holds a run could not.
fuelbuddy_crm.invoice_hold reads them: while a hold is Open and has not lapsed, a desk Sales
Invoice that would cover the held Delivery Note is refused, and the 12:00 auto-invoicing job
defers that Sales Order.

One ``QC Hold`` row per invoiced item (the Delivery Note stamp ``custom_invoiced_item_id``),
never deleted. Both methods lock the row first and are idempotent.

``open_hold(invoiced_item_id, episode_key, expires_at)``

    no row                          insert, Open                          OPENED
    an older episode (any status)   taken over: Open for this episode     OPENED
    this episode, Open              unchanged                             ALREADY_OPEN
    this episode, Closed            unchanged: an ended episode never     ALREADY_CLOSED
                                    reopens its hold
    a newer episode                 unchanged                             SUPERSEDED

``close_hold(invoiced_item_id, episode_key, reason)``

    this episode, Open              Closed                                CLOSED
    an older episode, Open          Closed, stamped with this episode     CLOSED
    an older episode, Closed        stamped with this episode             NOTHING_TO_CLOSE
    no row                          a Closed row for this episode         NOTHING_TO_CLOSE
    this episode, Closed            unchanged                             NOTHING_TO_CLOSE
    a newer episode                 unchanged                             NOTHING_TO_CLOSE

An old episode never closes a newer one's hold, and never takes the row back from it. A close
stamps the row with its episode, even when there was nothing to close, so an open for that
episode arriving late (a retry still in flight when the ticket ended) answers ALREADY_CLOSED
instead of holding invoices again.

Episode order: ``episode_key`` is ``{ticket_id}-{epoch ms of the raise}``
(quantity_correction_tickets.episode_key), and the app allows one open ticket per invoiced
item, so the digits after the last "-" order the episodes of one item. When either key does not
end in digits the order is unknown: an open takes the row over (the item stays held) and a close
touches only its own episode's row.

``expires_at`` is the ticket's ``apply_cutoff_at``: ERP refuses the amend after it, so the hold
stops holding invoices then even if it is never closed. An ISO timestamp; without an offset it
is read in the site's time zone. Stored, like every time here, as naive UTC and compared with
the database clock.

Answers mirror fuelbuddy_crm.api.qty_correction: ``{ok, code, message, retryable, result}``.

    ERP_VALIDATION   bad input, or not permitted to write Delivery Notes     final
    LOCK_RETRY       lock wait, deadlock, a concurrent first open            retry
"""

import re
from datetime import timezone
from zoneinfo import ZoneInfo

import frappe
from dateutil import parser as date_parser
from frappe import _
from frappe.utils import get_system_timezone, strip_html

DOCTYPE = "QC Hold"
OPEN = "Open"
CLOSED = "Closed"

OPENED = "OPENED"
ALREADY_OPEN = "ALREADY_OPEN"
ALREADY_CLOSED = "ALREADY_CLOSED"
SUPERSEDED = "SUPERSEDED"
CLOSED_NOW = "CLOSED"
NOTHING_TO_CLOSE = "NOTHING_TO_CLOSE"

RETRYABLE = frozenset({"LOCK_RETRY"})
# Data fields are varchar(140).
MAX_LENGTH = 140
_EPISODE_ORDER = re.compile(r"-(\d+)$")


class Refusal(Exception):
	"""A refusal: returned to the caller as ``ok: false`` with its code."""

	def __init__(self, code, message):
		super().__init__(message)
		self.code = code
		self.message = message


# ---- open ----------------------------------------------------------------------------------------
@frappe.whitelist(methods=["POST"])
def open_hold(invoiced_item_id, episode_key, expires_at):
	"""Hold the invoiced item's Delivery Note for this ticket episode. See the module docstring."""
	try:
		iid = _required(invoiced_item_id, "invoiced_item_id")
		episode = _required(episode_key, "episode_key")
		until = _parse_utc(expires_at, "expires_at")
		_check_permitted()
	except Refusal as refusal:
		return _refused(refusal)
	return _in_transaction(lambda: _open(iid, episode, until))


def _open(iid, episode, until):
	row = _lock(iid)
	if row and row.episode_key == episode:
		if row.status == OPEN:
			return _ok(ALREADY_OPEN, _("Hold on {0} is already open for episode {1}").format(iid, episode))
		return _ok(
			ALREADY_CLOSED,
			_("Episode {0} already closed its hold on {1}; an ended episode does not reopen it").format(
				episode, iid
			),
		)
	if row and _is_older(episode, row.episode_key):
		return _ok(
			SUPERSEDED,
			_("Hold on {0} belongs to newer episode {1}; episode {2} is over").format(
				iid, row.episode_key, episode
			),
		)

	fields = {
		"episode_key": episode,
		"status": OPEN,
		"opened_at": _db_now(),
		"expires_at": until,
		"closed_at": None,
		"close_reason": None,
	}
	if row:
		frappe.db.set_value(DOCTYPE, iid, fields)
		message = _("Hold on {0} opened for episode {1}, taken over from episode {2}").format(
			iid, episode, row.episode_key
		)
	else:
		frappe.get_doc({"doctype": DOCTYPE, "invoiced_item_id": iid, **fields}).insert(
			ignore_permissions=True
		)
		message = _("Hold on {0} opened for episode {1}").format(iid, episode)
	return _ok(OPENED, message)


# ---- close ---------------------------------------------------------------------------------------
@frappe.whitelist(methods=["POST"])
def close_hold(invoiced_item_id, episode_key, reason):
	"""Lift the hold when the ticket episode ends. See the module docstring."""
	try:
		iid = _required(invoiced_item_id, "invoiced_item_id")
		episode = _required(episode_key, "episode_key")
		why = _required(reason, "reason")
		_check_permitted()
	except Refusal as refusal:
		return _refused(refusal)
	return _in_transaction(lambda: _close(iid, episode, why))


def _close(iid, episode, reason):
	row = _lock(iid)
	if row and row.episode_key == episode and row.status == CLOSED:
		return _ok(NOTHING_TO_CLOSE, _("Hold on {0} is already closed for episode {1}").format(iid, episode))
	if row and row.episode_key != episode and not _is_older(row.episode_key, episode):
		return _ok(
			NOTHING_TO_CLOSE,
			_("Hold on {0} belongs to episode {1}, not to {2} or an earlier episode").format(
				iid, row.episode_key, episode
			),
		)

	now = _db_now()
	if not row:
		# Nothing was held. The Closed row keeps a late open for this episode from holding again.
		frappe.get_doc(
			{
				"doctype": DOCTYPE,
				"invoiced_item_id": iid,
				"episode_key": episode,
				"status": CLOSED,
				"expires_at": now,
				"closed_at": now,
				"close_reason": reason,
			}
		).insert(ignore_permissions=True)
		return _ok(NOTHING_TO_CLOSE, _("No hold on {0}; recorded episode {1} as ended").format(iid, episode))

	was_open = row.status == OPEN
	if row.episode_key != episode:
		# An earlier episode's row: the app allows one open ticket per item, so it ended before this
		# one was raised.
		reason = _("{0}; ends earlier episode {1}").format(reason, row.episode_key)
	frappe.db.set_value(
		DOCTYPE,
		iid,
		{"episode_key": episode, "status": CLOSED, "closed_at": now, "close_reason": reason[:MAX_LENGTH]},
	)
	if was_open:
		return _ok(CLOSED_NOW, _("Hold on {0} closed: {1}").format(iid, reason))
	return _ok(
		NOTHING_TO_CLOSE, _("Hold on {0} was not open; recorded episode {1} as ended").format(iid, episode)
	)


# ---- helpers -------------------------------------------------------------------------------------
def _in_transaction(step):
	"""Run one locked read-and-write and commit it, or roll back and answer with the refusal."""
	try:
		result = step()
		frappe.db.commit()
		return result
	except Exception as exc:
		frappe.db.rollback()
		refusal = _as_refusal(exc)
		if refusal is None:
			raise  # infrastructure: surfaces as an HTTP error, which erp-functions answers as retryable
		return _refused(refusal)


def _lock(iid):
	"""The item's row, locked; with no row, the locking read takes the gap lock, so two first opens
	of one item take turns (or one deadlocks and is retried)."""
	rows = frappe.db.sql(
		f"select name, episode_key, status from `tab{DOCTYPE}` where name = %s for update",
		iid,
		as_dict=True,
	)
	return rows[0] if rows else None


def episode_order(episode_key):
	"""The raise time (epoch ms) at the end of an episode key, or None when it has none."""
	match = _EPISODE_ORDER.search(str(episode_key or ""))
	return int(match.group(1)) if match else None


def _is_older(episode, than):
	"""True only when both keys carry a raise time and ``episode``'s is earlier."""
	mine, theirs = episode_order(episode), episode_order(than)
	return mine is not None and theirs is not None and mine < theirs


def _check_permitted():
	# The same gate as amend_delivery_note: the hold stands for the amend it protects.
	if not frappe.has_permission("Delivery Note", "write"):
		raise Refusal("ERP_VALIDATION", _("Not permitted to hold invoices for Delivery Notes"))


def _db_now():
	"""The database clock (UTC), the one the holds are compared with."""
	return frappe.db.sql("select utc_timestamp(6)")[0][0]


def _required(value, field):
	text = (str(value) if value is not None else "").strip()
	if not text:
		raise Refusal("ERP_VALIDATION", _("{0} is required").format(field))
	if len(text) > MAX_LENGTH:
		raise Refusal("ERP_VALIDATION", _("{0} must be at most {1} characters").format(field, MAX_LENGTH))
	return text


def _parse_utc(value, field):
	"""ISO timestamp -> naive UTC. Without an offset it is read in the site's time zone."""
	text = _required(value, field)
	try:
		moment = date_parser.isoparse(text)
	except (ValueError, OverflowError):
		raise Refusal("ERP_VALIDATION", _("{0} must be an ISO timestamp, got {1!r}").format(field, text))
	if moment.tzinfo is None:
		moment = moment.replace(tzinfo=ZoneInfo(get_system_timezone()))
	return moment.astimezone(timezone.utc).replace(tzinfo=None)


def _ok(result, message):
	return {"ok": True, "code": None, "message": message, "retryable": False, "result": result}


def _refused(refusal):
	frappe.clear_messages()
	return {
		"ok": False,
		"code": refusal.code,
		"message": refusal.message,
		"retryable": refusal.code in RETRYABLE,
		"result": None,
	}


def _as_refusal(exc):
	"""Map an exception raised inside the transaction to its code; None = not ours to answer."""
	if isinstance(exc, Refusal):
		return exc
	if isinstance(
		exc,
		frappe.QueryDeadlockError
		| frappe.QueryTimeoutError
		| frappe.TimestampMismatchError
		| frappe.DuplicateEntryError
		| frappe.UniqueValidationError,
	):
		# A duplicate is the other first open of this item committing first: a retry reads its row.
		return Refusal("LOCK_RETRY", _message(exc))
	if _is_lock_error(exc):
		return Refusal("LOCK_RETRY", _message(exc))
	if isinstance(exc, frappe.ValidationError | frappe.PermissionError):
		return Refusal("ERP_VALIDATION", _message(exc))
	return None


def _is_lock_error(exc):
	try:
		return bool(frappe.db.is_deadlocked(exc) or frappe.db.is_timedout(exc))
	except Exception:
		return False


def _message(exc):
	return strip_html(str(exc) or exc.__class__.__name__)[:500]
