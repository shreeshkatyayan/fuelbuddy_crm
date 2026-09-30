"""Source fingerprint of the ERPNext and frappe code the billing re-check relies on (IDEV-3268).

Pure standard library, no frappe import: the same code hashes the running apps (billing_recheck.guard)
and any checkout on disk (scripts/check_billing_walk_fingerprint.py, CI / pre-deploy).

Each segment is a function, a method, a class attribute or one literal entry of a module-level dict.
Its fingerprint is the sha256 of a canonical dump of its AST: docstrings are dropped, and fields that
are empty or None are left out, so comments, formatting, docstring edits and the Python version (3.10
to 3.14 add or show empty AST fields differently) do not change it. Any change to the code itself does.
"ABSENT" means the segment must NOT exist (for example DeliveryNote must not override set_status).

The accepted fingerprints are pinned per app version in ``fingerprint_pins.json``. A version that is not
pinned, or any segment that differs from the pin for that version, is a mismatch: the runtime guard
then runs stock ERPNext code, and the CI script fails.
"""

import ast
import hashlib
import json
import os
import pathlib

ABSENT = "ABSENT"
PINS_FILE = pathlib.Path(__file__).with_name("fingerprint_pins.json")
WALK = "update_billed_amount_based_on_so"

# module key -> path inside the app checkout (the directory that contains the ``erpnext`` / ``frappe``
# package)
ERPNEXT_MODULES = {
	"delivery_note": "erpnext/stock/doctype/delivery_note/delivery_note.py",
	"sales_invoice": "erpnext/accounts/doctype/sales_invoice/sales_invoice.py",
	"stock_controller": "erpnext/controllers/stock_controller.py",
	"status_updater": "erpnext/controllers/status_updater.py",
	"sales_and_purchase_return": "erpnext/controllers/sales_and_purchase_return.py",
}
FRAPPE_MODULES = {
	"frappe": "frappe/__init__.py",
	"document": "frappe/model/document.py",
	"base_document": "frappe/model/base_document.py",
	"naming": "frappe/model/naming.py",
	"database": "frappe/database/database.py",
	"mariadb": "frappe/database/mariadb/database.py",
	"realtime": "frappe/realtime.py",
	"comment": "frappe/core/doctype/comment/comment.py",
	"version": "frappe/core/doctype/version/version.py",
	"file_utils": "frappe/core/doctype/file/utils.py",
	"notifications": "frappe/desk/notifications.py",
	"document_follow": "frappe/desk/form/document_follow.py",
	"global_search": "frappe/utils/global_search.py",
	"data": "frappe/utils/data.py",
	"html_utils": "frappe/utils/html_utils.py",
	"safe_exec": "frappe/utils/safe_exec.py",
}

# segment key -> (module key, dotted path). A path ending in ``[Key]`` is one entry of a module-level
# dict literal (status_map). Why each one is here:
ERPNEXT_SEGMENTS = {
	# the walk itself (billing_recheck.fifo reproduces its loop) and its two callers
	"delivery_note.update_billed_amount_based_on_so": ("delivery_note", WALK),
	"delivery_note.DeliveryNote.update_billing_status": (
		"delivery_note",
		"DeliveryNote.update_billing_status",
	),
	"sales_invoice.SalesInvoice.update_billing_status_in_dn": (
		"sales_invoice",
		"SalesInvoice.update_billing_status_in_dn",
	),
	# call order: update_prevdoc_status (SO line lock, returned_qty) runs before the walk
	"delivery_note.DeliveryNote.on_submit": ("delivery_note", "DeliveryNote.on_submit"),
	"delivery_note.DeliveryNote.on_cancel": ("delivery_note", "DeliveryNote.on_cancel"),
	"sales_invoice.SalesInvoice.on_submit": ("sales_invoice", "SalesInvoice.on_submit"),
	"sales_invoice.SalesInvoice.on_cancel": ("sales_invoice", "SalesInvoice.on_cancel"),
	# status_updater config: a return changes returned_qty only on the DN items its dn_detail names;
	# an SO-billing invoice updates Sales Order Item.billed_amt (the fast path's locking read)
	"delivery_note.DeliveryNote.__init__": ("delivery_note", "DeliveryNote.__init__"),
	"sales_invoice.SalesInvoice.__init__": ("sales_invoice", "SalesInvoice.__init__"),
	"status_updater.StatusUpdater.update_prevdoc_status": (
		"status_updater",
		"StatusUpdater.update_prevdoc_status",
	),
	# line_guard's premise: every committed Delivery Note / Sales Invoice event writes its Sales Order
	# Item rows (update_prevdoc_status -> update_qty -> _update_children: ``update ... set <field> =
	# <sum>``), so a row that differs between the snapshot and the locked read is an event it misses
	"status_updater.StatusUpdater.update_qty": ("status_updater", "StatusUpdater.update_qty"),
	"status_updater.StatusUpdater._update_children": ("status_updater", "StatusUpdater._update_children"),
	# a return's items carry the so_detail / dn_detail of the DN they return
	"delivery_note.make_sales_return": ("delivery_note", "make_sales_return"),
	"sales_and_purchase_return.make_return_doc": ("sales_and_purchase_return", "make_return_doc"),
	# the per-DN refresh the bulk path reproduces
	"stock_controller.StockController.update_billing_percentage": (
		"stock_controller",
		"StockController.update_billing_percentage",
	),
	"status_updater.StatusUpdater._update_percent_field": (
		"status_updater",
		"StatusUpdater._update_percent_field",
	),
	"status_updater.StatusUpdater._update_modified": ("status_updater", "StatusUpdater._update_modified"),
	"status_updater.StatusUpdater.set_status": ("status_updater", "StatusUpdater.set_status"),
	"status_updater.status_map[Delivery Note]": ("status_updater", "status_map[Delivery Note]"),
	# must not exist: an override would change what the refresh does
	"delivery_note.DeliveryNote.set_status": ("delivery_note", "DeliveryNote.set_status"),
	"delivery_note.DeliveryNote.get_status": ("delivery_note", "DeliveryNote.get_status"),
	"delivery_note.DeliveryNote.update_billing_percentage": (
		"delivery_note",
		"DeliveryNote.update_billing_percentage",
	),
	"delivery_note.DeliveryNote._update_percent_field": (
		"delivery_note",
		"DeliveryNote._update_percent_field",
	),
	"status_updater.StatusUpdater.get_status": ("status_updater", "StatusUpdater.get_status"),
}

FRAPPE_SEGMENTS = {
	# the write path of the walk and of set_status (the 2**23 write-skip premise rests on it)
	"database.Database.set_value": ("database", "Database.set_value"),
	"database.Database._get_update_dict": ("database", "Database._get_update_dict"),
	"database.Database.check_transaction_status": ("database", "Database.check_transaction_status"),
	"database.Database.MAX_WRITES_PER_TRANSACTION": ("database", "Database.MAX_WRITES_PER_TRANSACTION"),
	"database.Database.bulk_insert": ("database", "Database.bulk_insert"),
	"mariadb.MariaDBDatabase.CONVERSION_MAP": ("mariadb", "MariaDBDatabase.CONVERSION_MAP"),
	"data.flt": ("data", "flt"),
	"data.cint": ("data", "cint"),
	# get_doc + set_status(update=True): load, as_dict, db_set, add_comment, notify_update
	"document.Document.load_from_db": ("document", "Document.load_from_db"),
	"document.Document.db_set": ("document", "Document.db_set"),
	"document.Document.get_doc_before_save": ("document", "Document.get_doc_before_save"),
	"document.Document.load_doc_before_save": ("document", "Document.load_doc_before_save"),
	"document.Document.run_method": ("document", "Document.run_method"),
	"document.Document.hook": ("document", "Document.hook"),
	"document.Document.notify_update": ("document", "Document.notify_update"),
	"document.Document.add_comment": ("document", "Document.add_comment"),
	"base_document.BaseDocument.as_dict": ("base_document", "BaseDocument.as_dict"),
	"base_document.BaseDocument.get_valid_dict": ("base_document", "BaseDocument.get_valid_dict"),
	"base_document.BaseDocument._fix_numeric_types": ("base_document", "BaseDocument._fix_numeric_types"),
	"base_document.get_controller": ("base_document", "get_controller"),
	"frappe.get_doc_hooks": ("frappe", "get_doc_hooks"),
	"frappe.clear_document_cache": ("frappe", "clear_document_cache"),
	"frappe.get_document_cache_key": ("frappe", "get_document_cache_key"),
	"safe_exec.safe_eval": ("safe_exec", "safe_eval"),
	# the Label Comment insert the bulk path writes directly
	"document.Document.insert": ("document", "Document.insert"),
	"document.Document.set_user_and_timestamp": ("document", "Document.set_user_and_timestamp"),
	"document.Document.set_new_name": ("document", "Document.set_new_name"),
	"document.Document.run_before_save_methods": ("document", "Document.run_before_save_methods"),
	"document.Document._validate": ("document", "Document._validate"),
	"document.Document.run_post_save_methods": ("document", "Document.run_post_save_methods"),
	"document.Document.clear_cache": ("document", "Document.clear_cache"),
	"document.Document.save_version": ("document", "Document.save_version"),
	"base_document.BaseDocument.db_insert": ("base_document", "BaseDocument.db_insert"),
	"base_document.BaseDocument._sanitize_content": ("base_document", "BaseDocument._sanitize_content"),
	"naming.set_new_name": ("naming", "set_new_name"),
	"naming.make_autoname": ("naming", "make_autoname"),
	"naming.validate_name": ("naming", "validate_name"),
	"comment.Comment.after_insert": ("comment", "Comment.after_insert"),
	"comment.Comment.validate": ("comment", "Comment.validate"),
	"comment.Comment.on_update": ("comment", "Comment.on_update"),
	"comment.Comment.notify_change": ("comment", "Comment.notify_change"),
	"comment.update_comment_in_doc": ("comment", "update_comment_in_doc"),
	"notifications.notify_mentions": ("notifications", "notify_mentions"),
	"version.Version.for_insert": ("version", "Version.for_insert"),
	"version.Version.update_version_info": ("version", "Version.update_version_info"),
	"file_utils.relink_mismatched_files": ("file_utils", "relink_mismatched_files"),
	"document_follow.follow_document": ("document_follow", "follow_document"),
	"global_search.update_global_search": ("global_search", "update_global_search"),
	"html_utils.sanitize_html": ("html_utils", "sanitize_html"),
	# realtime messages queued after commit
	"realtime.publish_realtime": ("realtime", "publish_realtime"),
	"realtime.flush_realtime_log": ("realtime", "flush_realtime_log"),
	"realtime.clear_realtime_log": ("realtime", "clear_realtime_log"),
	"realtime.get_doc_room": ("realtime", "get_doc_room"),
	"realtime.get_doctype_room": ("realtime", "get_doctype_room"),
}

APPS = {
	"erpnext": (ERPNEXT_MODULES, ERPNEXT_SEGMENTS),
	"frappe": (FRAPPE_MODULES, FRAPPE_SEGMENTS),
}


# ---- canonical AST ---------------------------------------------------------------------------------
_DOCSTRING_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def canonical(node):
	"""A dump of ``node`` that does not depend on formatting, comments, docstrings or the Python
	version: node type plus every non-empty field, recursively (no line numbers)."""
	if isinstance(node, ast.AST):
		parts = []
		for name in node._fields:
			value = getattr(node, name, None)
			if name == "body" and isinstance(node, _DOCSTRING_OWNERS):
				value = without_docstring(value)
			if value is None or value == []:
				continue
			parts.append(f"{name}={canonical(value)}")
		return f"{type(node).__name__}({', '.join(parts)})"
	if isinstance(node, list):
		return "[" + ", ".join(canonical(item) for item in node) + "]"
	return repr(node)


def without_docstring(body):
	if (
		body
		and isinstance(body[0], ast.Expr)
		and isinstance(body[0].value, ast.Constant)
		and isinstance(body[0].value.value, str)
	):
		return body[1:]
	return body


def digest(text):
	return hashlib.sha256(text.encode()).hexdigest()


# ---- segments ----------------------------------------------------------------------------------------
def find(tree, dotted):
	"""The AST node (or literal value, for ``name[Key]``) at ``dotted`` in a parsed module, or None."""
	if dotted.endswith("]"):
		name, key = dotted[:-1].split("[", 1)
		for node in tree.body:
			if isinstance(node, ast.Assign) and any(
				isinstance(t, ast.Name) and t.id == name for t in node.targets
			):
				value = ast.literal_eval(node.value)
				return ("literal", value[key]) if key in value else None
		return None
	body, node = tree.body, None
	for part in dotted.split("."):
		node = _member(body, part)
		if node is None:
			return None
		body = getattr(node, "body", [])
	return node


def _member(body, name):
	found = None
	for node in body:
		if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and node.name == name:
			found = node
		elif isinstance(node, ast.Assign) and any(
			isinstance(t, ast.Name) and t.id == name for t in node.targets
		):
			found = node
		elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
			found = node
	return found  # the last definition wins, as it does at import time


def segment_hash(tree, dotted):
	found = find(tree, dotted)
	if found is None:
		return ABSENT
	if isinstance(found, tuple):  # literal dict entry
		return digest(json.dumps(found[1], sort_keys=True, separators=(",", ":")))
	return digest(canonical(found))


def count_calls(tree, name=WALK):
	"""Calls of ``name`` (plain or as an attribute) in a parsed module."""
	count = 0
	for node in ast.walk(tree):
		if isinstance(node, ast.Call):
			func = node.func
			if (isinstance(func, ast.Name) and func.id == name) or (
				isinstance(func, ast.Attribute) and func.attr == name
			):
				count += 1
	return count


def app_version(root, app):
	"""``__version__`` from ``<root>/<app>/__init__.py`` without importing it."""
	tree = ast.parse(pathlib.Path(root, app, "__init__.py").read_text())
	for node in tree.body:
		if (
			isinstance(node, ast.Assign)
			and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets)
			and isinstance(node.value, ast.Constant)
		):
			return node.value.value
	return None


def parse_modules(root, app):
	modules, _segments = APPS[app]
	return {key: ast.parse(pathlib.Path(root, path).read_text()) for key, path in modules.items()}


def fingerprints(trees, app):
	"""{segment key: hash} for ``app`` from {module key: parsed module}."""
	_modules, segments = APPS[app]
	return {key: segment_hash(trees[mod], dotted) for key, (mod, dotted) in segments.items()}


def tree_callers(root, app="erpnext", name=WALK):
	"""Calls of ``name`` across every .py file of the app package (the CI caller-count check)."""
	total, where = 0, []
	for dirpath, _dirs, files in os.walk(pathlib.Path(root, app)):
		for filename in files:
			if not filename.endswith(".py"):
				continue
			path = pathlib.Path(dirpath, filename)
			try:
				n = count_calls(ast.parse(path.read_text()), name)
			except (SyntaxError, UnicodeDecodeError):
				continue
			if n:
				total += n
				where.append(f"{path.relative_to(root)}:{n}")
	return total, sorted(where)


# ---- pins ------------------------------------------------------------------------------------------
def load_pins(path=PINS_FILE):
	return json.loads(pathlib.Path(path).read_text())


def compare(app, version, hashes, pins=None):
	"""Mismatch reasons (empty when ``hashes`` equal the pin for ``app`` ``version``)."""
	pins = pins or load_pins()
	versions = pins.get(app, {})
	pinned = versions.get(version)
	if pinned is None:
		out = [f"{app}:version {version} not pinned (pinned: {', '.join(sorted(versions))})"]
		if versions:  # the review starting point: what differs from the closest pinned version
			nearest = min(versions, key=lambda v: len(_differing(hashes, versions[v]["segments"])))
			diff = _differing(hashes, versions[nearest]["segments"])
			out.append(f"{app}:differs from pinned {nearest} in: {', '.join(diff) or 'nothing'}")
		return out
	return [f"{app}:{key}" for key in _differing(hashes, pinned["segments"])]


def _differing(hashes, expected):
	out = [key for key in sorted(expected) if hashes.get(key) != expected[key]]
	return out + [f"{key} (not pinned)" for key in sorted(set(hashes) - set(expected))]


# ---- database driver -------------------------------------------------------------------------------
# Every value the re-check writes goes through frappe's database driver, and the 2**23 write-skip rule
# (billing_recheck.fifo.written_decimal) is proven only for how the pinned driver formats a float.
# frappe 15 connects to MariaDB with PyMySQL only; frappe 16 uses mysqlclient unless the site sets
# use_mysqlclient: 0. The pins name drivers as pip and frappe's pyproject.toml do:
# "runtime": {"db_driver": {"PyMySQL": ["1.1.1"]}}.
# top-level package of a connection class -> (driver name, the package attribute holding its version)
DB_DRIVERS = {
	"pymysql": ("PyMySQL", "VERSION_STRING"),  # not __version__: that is a MySQLdb-compatible "1.4.6"
	"MySQLdb": ("mysqlclient", "version_info"),  # a tuple, e.g. (2, 2, 7, "final", 0)
}


def connection_driver(conn, modules):
	"""(driver, version) of the database connection ``conn``, read from the package that defines its
	class (looked up in ``modules``, i.e. sys.modules). Any other connection comes back as (its class
	path, None), and a missing one as ("no connection", None); driver_mismatches rejects both."""
	if conn is None:
		return "no connection", None
	cls = type(conn)
	package = cls.__module__.partition(".")[0]
	if package not in DB_DRIVERS:
		return f"{cls.__module__}.{cls.__qualname__}", None
	driver, attribute = DB_DRIVERS[package]
	version = getattr(modules.get(package), attribute, None)
	if isinstance(version, tuple):
		version = ".".join(str(part) for part in version[:3])
	return driver, version


def driver_mismatches(driver, version, pins=None):
	"""[] when ``driver`` ``version`` is pinned (pins["runtime"]["db_driver"]), else the reason: an
	unknown driver, a driver that is not pinned and a version that is not pinned all count."""
	pins = pins or load_pins()
	pinned = pins.get("runtime", {}).get("db_driver", {})
	if driver not in {name for name, _attribute in DB_DRIVERS.values()}:
		return [f"db driver unknown: {driver}"]
	if version not in pinned.get(driver, []):
		accepted = ", ".join(f"{name} {v}" for name in sorted(pinned) for v in pinned[name]) or "none"
		return [f"db driver {driver} {version} not pinned (pinned: {accepted})"]
	return []


def check_checkout(root, app, pins=None, scan_callers=True):
	"""Full check of one checkout: version pinned, every segment equal, and (erpnext) the walk has
	exactly the pinned number of callers in the whole package. Returns (version, hashes, mismatches)."""
	pins = pins or load_pins()
	version = app_version(root, app)
	hashes = fingerprints(parse_modules(root, app), app)
	mismatches = compare(app, version, hashes, pins)
	pinned = pins.get(app, {}).get(version) or {}
	if scan_callers and "callers" in pinned:
		total, where = tree_callers(root, app)
		if total != pinned["callers"]:
			mismatches.append(
				f"{app}:{WALK} has {total} callers, pinned {pinned['callers']} ({', '.join(where)})"
			)
	return version, hashes, mismatches
