"""Invoice-side bulk refresh of Delivery Note billing (IDEV-3268).

Why. A Sales Invoice submit or cancel really changes most of the line (lab, 30k rows: 24,506 DNs), and
ERPNext v15.96.0 then refreshes every changed DN one at a time inside the invoice's transaction
(SalesInvoice.update_billing_status_in_dn, sales_invoice.py:1784): get_doc, update_billing_percentage
(UPDATE per_billed and modified), and with update_modified a fresh get_doc, set_status(update=True) (a
Label Comment insert when the status changes, then db_set('status') for every DN) and notify_update.
That loop held the Sales Order line 568 s at 30k (IDEV-3268 lab evidence TWIN_n30000.json, S7).

What. With ``billing_recheck_bulk_over`` = N and more than N DNs to refresh, the same refresh runs
set-based, in the same transaction, for exactly the same DNs (nothing deferred, nothing left stale):
1. per_billed: stock's own SQL expression (stock_refresh.PER_BILLED), one UPDATE per 1000 DNs per
   target_ref_field, the field picked per DN as stock picks it; modified / modified_by set only with
   update_modified, as stock does.
2. with update_modified only, as in stock: the status set_status gives each DN from its row as it now is
   (stock_refresh.statuses); status written where it changes (modified / modified_by already hold this
   refresh's timestamp on every DN, where db_set leaves them); one Label Comment per change outside
   set_status's exceptions, with the columns Comment.insert writes; the document-cache clear
   Database.set_value does; the notify_update messages (Comment and DN) queued after commit as
   publish_realtime queues them.

Not reproduced per DN, which gap_reasons() proves to be no-ops on this site before every bulk run (any
doubt, or an error while checking, runs stock's loop instead):
- db_set('status')'s before_change / on_change: doc_events hooks, Notifications, Webhooks, Server Scripts;
- the Comment insert's hooks and handlers (before_insert ... on_change), Document Naming Rules, global
  search, follow_document (returns at once for Comment), link validation (reads only);
- an override of the Delivery Note or Comment controller (frappe.get_controller must be the stock class).
Differences that remain by design: one timestamp for the whole refresh instead of one per DN and
Comment; the order of the realtime messages; stock also locks every child row of every refreshed DN
(db_set loads the DN FOR UPDATE with its children), the bulk UPDATEs lock only the Delivery Note rows;
the ('Comment', None) entry each stock Comment insert leaves in frappe.flags.currently_saving; a DN name
given twice with different case is refreshed once.

Transaction size. frappe counts every UPDATE / INSERT statement against MAX_WRITES_PER_TRANSACTION
(200,000 by default). Stock's loop issues 3 to 4 statements per DN (so an invoice changing ~50k DNs fails
with TooManyWritesError); this refresh issues about 2 per 1000 DNs plus one INSERT per 10,000 Comments.
The walk still writes one statement per changed Delivery Note Item row, so one invoice stays under the
limit up to roughly 199k changed rows.
"""

import json
import time

import frappe
from frappe import _
from frappe.model.naming import make_autoname
from frappe.realtime import clear_realtime_log, flush_realtime_log, get_doc_room, get_doctype_room
from frappe.utils import now, sanitize_html

from fuelbuddy_crm.billing_recheck import config, guard, observe
from fuelbuddy_crm.billing_recheck import stock_refresh as sr

DN = "Delivery Note"
COMMENT = "Comment"
NO_LABEL = ("Cancelled", "Partially Ordered", "Ordered", "Issued", "Transferred")  # set_status
CONTENT_DISALLOWED_TAGS = ["form", "input", "button"]  # Comment.validate
# every column of tabComment in frappe 15.99.0 / 15.113.1 / 15.121.1; any other column -> stock (gap_reasons)
COMMENT_FIELDS = (
	"name",
	"creation",
	"modified",
	"modified_by",
	"owner",
	"docstatus",
	"idx",
	"comment_type",
	"comment_email",
	"subject",
	"comment_by",
	"published",
	"seen",
	"reference_doctype",
	"reference_name",
	"reference_owner",
	"content",
	"ip_address",
	"_user_tags",
	"_comments",
	"_assign",
	"_liked_by",
)
UNSAFE_FLAGS = ("in_patch", "in_install", "in_migrate", "in_import", "in_setup_wizard")
# the run_method calls the bulk path does not make, per doctype
SKIPPED_METHODS = {
	DN: ("before_change", "on_change"),
	COMMENT: (
		"before_insert",
		"before_naming",
		"autoname",
		"before_validate",
		"validate",
		"before_save",
		"after_insert",
		"on_update",
		"on_change",
	),
}
# doc_events handlers that can resolve for SKIPPED_METHODS, each with the data check in _data_gaps()
# that proves it a no-op (frappe 15.99.0 / 15.113.1 / 15.121.1, erpnext 15.96.0, frappe_whatsapp 1.0.x hooks)
KNOWN_HOOKS = {
	"frappe.social.doctype.energy_point_rule.energy_point_rule.process_energy_points": "energy_point_rule",
	"frappe.automation.doctype.milestone_tracker.milestone_tracker.evaluate_milestone": "milestone_tracker",
	"frappe.desk.notifications.clear_doctype_notifications": "notification_config",
	"frappe.workflow.doctype.workflow_action.workflow_action.process_workflow_actions": "workflow",
	"frappe.core.doctype.file.utils.attach_files_to_document": "attach_fields",
	"frappe.automation.doctype.assignment_rule.assignment_rule.apply": "assignment_rule",
	"frappe.automation.doctype.assignment_rule.assignment_rule.update_due_date": "assignment_rule",
	"frappe.core.doctype.user_type.user_type.apply_permissions_for_non_standard_user_type": "user_type",
	"frappe.search.sqlite_search.update_doc_index": "sqlite_search",
	"erpnext.support.doctype.service_level_agreement.service_level_agreement.apply": "sla",
	"erpnext.setup.doctype.transaction_deletion_record.transaction_deletion_record.check_for_running_deletion_job": "company_field",
	"frappe_whatsapp.utils.run_server_script_for_doc_event": "whatsapp_notification",
}


# ---- entry point: SalesInvoice.update_billing_status_in_dn -------------------------------------------
def wrap_update_billing_status_in_dn(original):
	def update_billing_status_in_dn(self, update_modified=True):
		if not config.bulk_over():
			return original(self, update_modified)
		if mismatch := guard.mismatches():  # the copy below is only proven equal to the pinned body
			observe.count("guard_disabled", invoice=self.name, mismatch=",".join(mismatch))
			return original(self, update_modified)
		# ERPNext v15.96.0 sales_invoice.py:1784-1808 statement for statement (the fingerprint pins it,
		# tests/test_billing_recheck_fingerprint.py compares the ASTs), the walk looked up in the
		# sales_invoice module as stock does, and the final per-DN loop through refresh()
		from erpnext.accounts.doctype.sales_invoice import sales_invoice as si_mod

		if self.is_return and not self.update_billed_amount_in_delivery_note:
			return
		updated_delivery_notes = []
		for d in self.get("items"):
			if d.dn_detail:
				billed_amt = frappe.db.sql(
					"""select sum(amount) from `tabSales Invoice Item`
					where dn_detail=%s and docstatus=1""",
					d.dn_detail,
				)
				billed_amt = (billed_amt and billed_amt[0][0]) or 0
				frappe.db.set_value(
					"Delivery Note Item",
					d.dn_detail,
					"billed_amt",
					billed_amt,
					update_modified=update_modified,
				)
				updated_delivery_notes.append(d.delivery_note)
			elif d.so_detail:
				updated_delivery_notes += si_mod.update_billed_amount_based_on_so(
					d.so_detail, update_modified
				)

		refresh(set(updated_delivery_notes), update_modified)

	update_billing_status_in_dn.__wrapped__ = original
	update_billing_status_in_dn.__name__ = original.__name__
	update_billing_status_in_dn.__qualname__ = original.__qualname__
	update_billing_status_in_dn._billing_recheck = True
	return update_billing_status_in_dn


def refresh(names, update_modified=True):
	"""The final loop of update_billing_status_in_dn over the DN set ``names``: set-based when allowed,
	else stock's loop over the same set."""
	t0 = time.perf_counter()
	why = why_stock(names)
	if why:
		for dn in names:
			frappe.get_doc(DN, dn).update_billing_percentage(update_modified=update_modified)
		if why != "small":
			observe.log(f"bulk refresh refused ({why}), stock loop over {len(names)} DNs", "warning")
		return
	info = bulk_refresh(names, update_modified)
	observe.count("bulk", rows=len(names), ms=round((time.perf_counter() - t0) * 1000, 1), **info)


def why_stock(names):
	"""None when the bulk refresh may run for ``names``, else the reason stock's loop runs."""
	if len(names) <= config.bulk_over():
		return "small"
	if not all(isinstance(n, str) and n for n in names):
		return "blank_name"  # stock's get_doc decides what happens
	for flag in UNSAFE_FLAGS:
		if frappe.flags.get(flag):
			return f"flag_{flag}"  # several skipped handlers and notify_update behave differently
	if not guard.ok():
		return "guard"
	gaps = safe_gap_reasons()
	if gaps:
		observe.warn_once("bulk_gap:" + ",".join(gaps), f"bulk DN refresh refused, stock loop runs: {gaps}")
		return "gap"
	found = set()
	for chunk in sr.chunks(names):
		found.update(
			frappe.db.sql_list("select name from `tabDelivery Note` where name in %(n)s", {"n": chunk})
		)
	if found != set(names):
		return "names"  # missing (stock raises DoesNotExistError) or not the stored spelling
	return None


# ---- the set-based refresh ---------------------------------------------------------------------------
def bulk_refresh(names, update_modified=True):
	names = sorted(names)
	ts, user = now(), frappe.session.user
	info = {"dns": len(names)}

	# 1. per_billed: _update_percent_field's UPDATE (+ _update_modified's columns)
	set_modified = ", dn.modified = %(ts)s, dn.modified_by = %(user)s" if update_modified else ""
	for ref, group in sr.by_ref(names).items():
		for chunk in sr.chunks(group):
			frappe.db.sql(
				f"""update `tabDelivery Note` dn
				set dn.per_billed = {sr.PER_BILLED.format(ref=ref)}{set_modified}
				where dn.name in %(names)s""",
				{"names": chunk, "ts": ts, "user": user},
			)
	if not update_modified:
		return info  # stock: no set_status and no notify_update without update_modified

	# 2. set_status(update=True) on each DN as it is now
	statuses = sr.statuses(names)
	changed = [n for n in names if statuses[n][1] != statuses[n][0]]
	info["status_changed"] = len(changed)

	# db_set('status'): write only where it changes (modified / modified_by already hold ts / user)
	by_status = {}
	for n in changed:
		by_status.setdefault(statuses[n][1], []).append(n)
	for status, group in by_status.items():
		for chunk in sr.chunks(group):
			frappe.db.sql(
				"""update `tabDelivery Note` set status = %(status)s, modified = %(ts)s, modified_by = %(user)s
				where name in %(names)s""",
				{"status": status, "ts": ts, "user": user, "names": chunk},
			)

	# set_status's add_comment('Label', _(status)) for each change outside NO_LABEL
	labelled = [n for n in changed if statuses[n][1] not in NO_LABEL]
	comment_of = dict(zip(labelled, _comment_names(len(labelled)), strict=True))
	rows = [_comment_row(comment_of[n], n, label_content(statuses[n][1]), ts, user) for n in labelled]
	if rows:
		frappe.db.bulk_insert(COMMENT, list(COMMENT_FIELDS), rows)
	info["comments"] = len(rows)

	# Database.set_value's cache clear (db_set), then notify_update for each Comment and DN
	_clear_document_cache(names)
	_publish_after_commit(_messages(names, comment_of, ts, user))
	return info


def label_content(status):
	"""The content Comment.validate stores for add_comment('Label', _(status))."""
	return sanitize_html(_(status), always_sanitize=True, disallowed_tags=CONTENT_DISALLOWED_TAGS)


def _comment_row(name, dn, content, ts, user):
	values = {
		"name": name,
		"creation": ts,
		"modified": ts,
		"modified_by": user,
		"owner": user,
		"docstatus": 0,
		"idx": 0,
		"comment_type": "Label",
		"comment_email": user,
		"subject": None,
		"comment_by": None,
		"published": 0,
		"seen": 0,
		"reference_doctype": DN,
		"reference_name": dn,
		"reference_owner": None,
		"content": content,
		"ip_address": None,
		"_user_tags": None,
		"_comments": None,
		"_assign": None,
		"_liked_by": None,
	}
	return tuple(values[f] for f in COMMENT_FIELDS)


def _comment_names(count):
	"""make_autoname('hash', 'Comment') names (what set_new_name gives a Comment with no autoname and no
	naming rule), unique in the batch and not already taken."""
	names, seen = [], set()
	while len(names) < count:
		batch = []
		while len(batch) < count - len(names):
			name = make_autoname("hash", COMMENT)
			if name not in seen:
				seen.add(name)
				batch.append(name)
		taken = set()
		for chunk in sr.chunks(batch):
			taken.update(
				frappe.db.sql_list("select name from `tabComment` where name in %(n)s", {"n": chunk})
			)
		names += [n for n in batch if n not in taken]
	return names


def _clear_document_cache(names):
	"""frappe.clear_document_cache(DN, name) for every name (now, after commit, after rollback) and the
	value_cache drop Database.set_value does."""
	keys = [frappe.get_document_cache_key(DN, n) for n in names]

	def clear_in_redis():
		for chunk in sr.chunks(keys):
			frappe.cache.delete_value(chunk)

	clear_in_redis()
	frappe.db.after_commit.add(clear_in_redis)
	frappe.db.after_rollback.add(clear_in_redis)
	frappe.db.value_cache.pop(DN, None)


def _sends_list_update(doctype):
	"""Document.notify_update's condition for the list_update message."""
	meta = frappe.get_meta(doctype)
	return not meta.get("read_only") and not meta.get("issingle") and not meta.get("istable")


def _messages(names, comment_of, ts, user):
	"""notify_update's messages in stock's order per DN: the Label Comment's (from its insert), then the
	DN's (after db_set)."""
	list_update = {dt: _sends_list_update(dt) for dt in (DN, COMMENT)}
	out = []
	for n in names:
		for doctype, name in ((COMMENT, comment_of.get(n)), (DN, n)):
			if name is None:
				continue
			out.append(("doc_update", {"modified": ts, "doctype": doctype, "name": name}, doctype, name))
			if list_update[doctype]:
				out.append(("list_update", {"doctype": doctype, "name": name, "user": user}, doctype, None))
	return out


def _publish_after_commit(messages):
	"""frappe.publish_realtime(event, message, doctype=, docname=, after_commit=True) for each message,
	without its linear duplicate scan of the whole log per message."""
	if getattr(frappe.local, "task_id", None):  # in a job stock emits at once to the task room
		for event, message, doctype, docname in messages:
			frappe.publish_realtime(event, message, doctype=doctype, docname=docname, after_commit=True)
		return
	if not hasattr(frappe.local, "_realtime_log"):
		frappe.local._realtime_log = []
		frappe.db.after_commit.add(flush_realtime_log)
		frappe.db.after_rollback.add(clear_realtime_log)
	log = frappe.local._realtime_log
	seen = {_key(p) for p in log}
	for event, message, doctype, docname in messages:
		room = get_doctype_room(doctype) if event == "list_update" else get_doc_room(doctype, docname)
		params = [event, message, room]
		if (key := _key(params)) not in seen:
			seen.add(key)
			log.append(params)


def _key(params):
	return json.dumps(params, sort_keys=True, default=str)


# ---- what the bulk path skips must be a no-op ------------------------------------------------------------
def safe_gap_reasons():
	"""gap_reasons(), with any error while checking turned into a reason (so stock runs)."""
	try:
		return gap_reasons()
	except Exception as exc:
		return [f"error:{exc!r}"]


def gap_reasons():
	"""[] when every per-document step the bulk path skips is a no-op on this site right now."""
	out = _controller_gaps()
	hooks = frappe.get_doc_hooks()
	for doctype, methods in SKIPPED_METHODS.items():
		for method in methods:
			for handler in hooks.get(doctype, {}).get(method, []) + hooks.get("*", {}).get(method, []):
				if handler not in KNOWN_HOOKS:
					out.append(f"hook:{doctype}.{method}:{handler}")
	out += _data_gaps()
	try:
		sr.status_fields()
	except ValueError as exc:
		out.append(f"status_map:{exc}")
	for label, _cond in sr.status_map():
		content = label_content(label)
		if "<" in content or ">" in content:  # Document._sanitize_content would sanitize it again
			out.append(f"label_content:{label}")
	columns = set(frappe.db.get_table_columns(COMMENT))
	if columns != set(COMMENT_FIELDS):
		out.append(f"comment_columns:{sorted(columns ^ set(COMMENT_FIELDS))}")
	meta = frappe.get_meta(COMMENT)
	if meta.autoname or getattr(meta, "naming_rule", None) or meta.get("is_virtual"):
		out.append("comment_naming")
	return out


def _controller_gaps():
	"""The controllers frappe resolves are the stock classes, and the controller methods run_method would
	call for SKIPPED_METHODS are only Comment's own fingerprinted after_insert / validate / on_update."""
	from frappe.core.doctype.comment.comment import Comment
	from frappe.model.base_document import get_controller  # frappe 15 has no frappe.get_controller

	dn_mod, _si, _sc, _su = guard.modules()
	out = []
	for doctype, cls in ((DN, dn_mod.DeliveryNote), (COMMENT, Comment)):
		resolved = get_controller(doctype)
		if resolved is not cls:
			out.append(f"controller:{doctype} is {resolved.__module__}.{resolved.__qualname__}")
	allowed = {(COMMENT, "after_insert"), (COMMENT, "validate"), (COMMENT, "on_update")}
	for doctype, cls in ((DN, dn_mod.DeliveryNote), (COMMENT, Comment)):
		for method in SKIPPED_METHODS[doctype]:
			defining = next((c for c in cls.__mro__ if method in c.__dict__), None)
			if defining is not None and not (defining is cls and (doctype, method) in allowed):
				out.append(f"method:{doctype}.{method} defined by {defining.__qualname__}")
	return out


def _data_gaps():
	out = []
	both = [DN, COMMENT]

	def exists(doctype, filters):
		return bool(frappe.db.table_exists(doctype) and frappe.get_all(doctype, filters=filters, limit=1))

	# Document.run_method -> run_notifications / run_webhooks / run_server_script_for_doc_event
	if exists(
		"Notification", {"enabled": 1, "document_type": DN, "event": ["in", ["Value Change", "Method"]]}
	):
		out.append("notification:Delivery Note")
	if exists("Notification", {"enabled": 1, "document_type": COMMENT}):
		out.append("notification:Comment")
	if exists("Webhook", {"enabled": 1, "webhook_doctype": DN, "webhook_docevent": "on_change"}):
		out.append("webhook:Delivery Note")
	if exists("Webhook", {"enabled": 1, "webhook_doctype": COMMENT}):
		out.append("webhook:Comment")
	from frappe.core.doctype.server_script.server_script_utils import EVENT_MAP

	for doctype, methods in SKIPPED_METHODS.items():
		events = [EVENT_MAP[m] for m in methods if m in EVENT_MAP]
		if events and exists(
			"Server Script",
			{
				"disabled": 0,
				"script_type": "DocType Event",
				"reference_doctype": doctype,
				"doctype_event": ["in", events],
			},
		):
			out.append(f"server_script:{doctype}")
	# frappe '*' on_change
	if exists("Energy Point Rule", {"enabled": 1, "reference_doctype": ["in", both]}):
		out.append("energy_point_rule")
	if exists("Milestone Tracker", {"disabled": 0, "document_type": ["in", both]}):
		out.append("milestone_tracker")
	# frappe '*' on_update (the Comment insert)
	from frappe.desk.notifications import get_notification_config

	if COMMENT in ((get_notification_config() or {}).get("for_doctype") or {}):
		out.append("notification_config")
	from frappe.model.workflow import get_workflow_name

	if get_workflow_name(COMMENT):
		out.append("workflow")
	meta = frappe.get_meta(COMMENT)
	if meta.get("fields", {"fieldtype": ["in", ["Attach", "Attach Image"]]}):
		out.append("attach_fields")
	if exists("Assignment Rule", {"disabled": 0, "document_type": COMMENT}):
		out.append("assignment_rule")
	if frappe.db.table_exists("User Type"):
		from frappe.core.doctype.user_type.user_type import get_non_standard_user_types

		user_types = frappe.cache.get_value("non_standard_user_types", get_non_standard_user_types) or {}
		if any(data[0] == COMMENT for data in user_types.values()):
			out.append("user_type")
	from frappe.search.sqlite_search import get_search_classes

	for search_class in get_search_classes():
		search = search_class()
		if search.is_search_enabled() and search.index_exists() and COMMENT in search.doc_configs:
			out.append("sqlite_search")
	# erpnext '*' validate (the Comment insert)
	from erpnext.support.doctype.service_level_agreement.service_level_agreement import (
		get_documents_with_active_service_level_agreement,
	)

	if COMMENT in (get_documents_with_active_service_level_agreement() or ()):
		out.append("sla")
	if meta.has_field("company"):
		out.append("company_field")
	# frappe_whatsapp '*' (installed in prod, not in the lab)
	if exists(
		"WhatsApp Notification",
		{"disabled": 0, "notification_type": "DocType Event", "reference_doctype": COMMENT},
	):
		out.append("whatsapp_notification")
	# Document.insert of the Comment
	if exists("Document Naming Rule", {"disabled": 0, "document_type": COMMENT}):
		out.append("document_naming_rule")
	if meta.get_global_search_fields():
		out.append("global_search")
	return out
