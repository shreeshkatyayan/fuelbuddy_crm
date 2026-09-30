# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Quantity correction (IDEV-3266) and the ERP wallet: never blocked, an overshoot raises an Issue.

Owner decision, 29 Sep 2026: a correction adjusts the customer's wallet both ways and is never
refused for it. One that leaves the wallet below zero is applied all the same, and an ERP Issue is
raised, assigned to ASSIGNEES.

fuelbuddy_crm.api.qty_correction.amend_delivery_note uses two things from here:

``QC_AMEND_FLAG``
    set in ``doc.flags`` on the Delivery Note the amend saves (a draft) or inserts and submits (the
    amendment). fuelbuddy_wallet's ``enforce_wallet_balance`` then skips its refusal, and its own
    block Issue, for that document only; its ``on_update`` / ``on_cancel`` / ``after_delete``
    recompute still books the change, so the wallet can go below zero. The string is the contract:
    fuelbuddy_wallet declares the same one.

``after_amend``
    inside the amend's transaction, once the change is booked: reads the customer's wallet balance
    the way fuelbuddy_wallet keeps it and, when it is below zero, raises the overshoot Issue in that
    same transaction, so the Issue commits with the change and never without it. One Issue per
    episode: its subject carries the episode key, and a retry is answered from the DN Amend Log
    before it gets here.
"""

import frappe
from frappe import _
from frappe.desk.form import assign_to
from frappe.utils import escape_html, flt

# doc.flags key. fuelbuddy_wallet (wallet.py QC_AMEND_FLAG) reads the same string.
QC_AMEND_FLAG = "fb_qc_amend"

# Owner decision, 29 Sep 2026: finance follows up every wallet a correction leaves below zero.
ASSIGNEES = ("aman.prabhaker@fuelbuddy.in", "sahal.shamsudheen@fuelbuddy.ae")

# The Issue Type erp-functions and fuelbuddy_wallet give the Issues they raise in ERP.
ISSUE_TYPE = "Error Log"

WALLET_APP = "fuelbuddy_wallet"
SETTINGS_DOCTYPE = "Fuelbuddy Settings"

# An overshoot step that fails is rolled back to its savepoint alone, never the correction.
_ISSUE_SAVEPOINT = "fb_qc_wallet_issue"
_ASSIGN_SAVEPOINT = "fb_qc_wallet_assign"


def issue_subject(key):
	"""The overshoot Issue's subject. It carries the episode key and is how the Issue is found again,
	so it is never translated."""
	return f"Wallet below zero after quantity correction {key}"


def after_amend(key, correction):
	"""Inside the amend's transaction, once the change is booked: True when the customer's ERP wallet
	is now below zero, after raising the overshoot Issue (ensure_overshoot_issue).

	``correction``: customer, delivery_note, result, new_delivery_note, from_qty, target_qty.

	Never refuses the correction. A wallet that cannot be read counts as not below zero, and an Issue
	that cannot be raised is rolled back to its savepoint; both go to the Error Log and the change
	stands. Only a lock error propagates: a deadlock may have rolled the whole transaction back, and
	the amend answers it LOCK_RETRY, which the episode key makes safe to retry."""
	try:
		wallet = wallet_after_amend(correction.customer)
	except Exception as exc:
		_raise_if_lock_error(exc)
		_log(_("Quantity correction {0}: ERP wallet not read").format(key), correction)
		return False
	if not (wallet and wallet.below_zero):
		return False

	frappe.db.savepoint(_ISSUE_SAVEPOINT)
	try:
		ensure_overshoot_issue(key, correction, wallet)
	except Exception as exc:
		_raise_if_lock_error(exc)
		frappe.db.rollback(save_point=_ISSUE_SAVEPOINT)
		_log(_("Quantity correction {0}: wallet overshoot Issue not raised").format(key), correction)
	else:
		frappe.db.release_savepoint(_ISSUE_SAVEPOINT)
	return True


def wallet_after_amend(customer):
	"""The customer's ERP wallet as fuelbuddy_wallet keeps it, read inside the amend's transaction:
	``{name, balance, below_zero}``, or None when no wallet is live for the customer.

	The balance is ``Wallet.amount_remaining`` (received less delivered, drafts included):
	fuelbuddy_wallet's own figure, which its on_update / on_cancel / after_delete hooks recomputed
	from the live GL and Delivery Notes when the amend saved, cancelled or deleted the Delivery Note,
	in this transaction. None when fuelbuddy_wallet is not installed, Enable Wallet is off (its hooks
	then do nothing, so the figure is not kept up) or the customer has no wallet. Below zero is
	judged to the fils, so float dust never raises an Issue."""
	if not customer or WALLET_APP not in frappe.get_installed_apps():
		return None
	if not frappe.db.get_single_value(SETTINGS_DOCTYPE, "enable_wallet"):
		return None
	row = frappe.db.get_value(
		"Wallet",
		{"customer": customer, "payment_type": "Wallet"},
		["name", "amount_remaining"],
		as_dict=True,
	)
	if not row:
		return None
	balance = flt(row.amount_remaining)
	return frappe._dict(name=row.name, balance=balance, below_zero=flt(balance, 2) < 0)


def ensure_overshoot_issue(key, correction, wallet):
	"""The overshoot Issue for episode ``key``: the one already raised (any status), or a new one
	assigned to ASSIGNEES. Returns its name. An assignee with no enabled ERP user is left out and
	named in the description."""
	subject = issue_subject(key)
	existing = frappe.db.get_value("Issue", {"subject": subject}, "name")
	if existing:
		return existing
	users, missing = _resolve_assignees()
	issue = frappe.get_doc(
		{
			"doctype": "Issue",
			"subject": subject,
			"customer": correction.customer,
			"issue_type": _issue_type(),
			"description": _description(key, correction, wallet, missing),
		}
	).insert(ignore_permissions=True)
	for user in users:
		_assign(issue.name, user, subject)
	return issue.name


def _resolve_assignees():
	"""(users, missing): the enabled ERP users behind ASSIGNEES, looked up by email, and the emails
	with none."""
	users, missing = [], []
	for email in ASSIGNEES:
		user = frappe.db.get_value("User", {"email": email, "enabled": 1}, "name")
		if user:
			users.append(user)
		else:
			missing.append(email)
	return users, missing


def _assign(issue, user, subject):
	"""Assign ``issue`` to ``user``: a ToDo, a share when they cannot read Issues, a notification.
	A failure leaves only that user unassigned, and goes to the Error Log."""
	frappe.db.savepoint(_ASSIGN_SAVEPOINT)
	try:
		assign_to.add(
			{"assign_to": [user], "doctype": "Issue", "name": issue, "description": subject},
			ignore_permissions=True,
		)
	except Exception as exc:
		_raise_if_lock_error(exc)
		frappe.db.rollback(save_point=_ASSIGN_SAVEPOINT)
		frappe.log_error(
			title=_("Issue {0}: not assigned to {1}").format(issue, user),
			reference_doctype="Issue",
			reference_name=issue,
		)
	else:
		frappe.db.release_savepoint(_ASSIGN_SAVEPOINT)


def _issue_type():
	"""ISSUE_TYPE, created once where a site lacks it: Issue Type is mandatory on the production
	site (fuelbuddy_creditlimit ensures its own type the same way)."""
	if not frappe.db.exists("Issue Type", ISSUE_TYPE):
		issue_type = frappe.new_doc("Issue Type")
		issue_type.name = ISSUE_TYPE
		issue_type.insert(ignore_permissions=True)
	return ISSUE_TYPE


def _description(key, correction, wallet, missing):
	lines = [
		_(
			"Quantity correction {0} was applied although it leaves the customer's ERP wallet below zero."
		).format(key),
		_("Customer: {0}").format(correction.customer),
		_("Delivery Note: {0}, {1}").format(correction.delivery_note, _outcome(correction)),
		_("Correction: {0} L to {1} L").format(_litres(correction.from_qty), _litres(correction.target_qty)),
		_("Wallet {0} balance after the correction: {1}").format(
			wallet.name, f"{flt(wallet.balance, 2):,.2f}"
		),
	]
	lines += [
		_("Not assigned to {0}: no enabled ERP user has this email.").format(email) for email in missing
	]
	return "<br>".join(escape_html(line) for line in lines)


def _outcome(correction):
	if correction.result == "AMENDED":
		return _("cancelled and reissued as {0}").format(correction.new_delivery_note)
	if correction.result == "CANCELLED":
		return _("cancelled: corrected to zero")
	if correction.result == "DRAFT_DELETED":
		return _("draft deleted: corrected to zero")
	return _("draft updated in place")


def _litres(value):
	"""1,000 / 1,234.567: up to three decimals, no trailing zeros."""
	return f"{flt(value, 3):,.3f}".rstrip("0").rstrip(".")


def _log(title, correction):
	frappe.log_error(title=title, reference_doctype="Delivery Note", reference_name=correction.delivery_note)


def _raise_if_lock_error(exc):
	"""Never carry on past a lock error: a deadlock rolls InnoDB's whole transaction back, savepoints
	included. The amend answers it LOCK_RETRY."""
	if isinstance(exc, frappe.QueryDeadlockError | frappe.QueryTimeoutError):
		raise exc
	try:
		locked = bool(frappe.db.is_deadlocked(exc) or frappe.db.is_timedout(exc))
	except Exception:
		locked = False
	if locked:
		raise exc
