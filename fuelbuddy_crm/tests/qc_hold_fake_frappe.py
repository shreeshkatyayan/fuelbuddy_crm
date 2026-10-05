# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""A stand-in for the frappe calls the invoice-hold code makes, on an in-memory SQLite database,
so it runs as plain Python with no bench and no site (IDEV-3266).

It is not frappe. SQLite has no row locks and no MariaDB clock: ``utc_timestamp()`` answers the
fake's ``now`` (settable), times are text in one fixed format (so text order is time order), and
``for update`` is dropped. Tests on it check the decisions the code makes, the rows it leaves and
the SQL it sends; locking itself needs a site.

``load(path, fake)`` executes a module file with this fake bound as its ``frappe``. Its
fuelbuddy_crm imports are loaded fresh against the same fake, and sys.modules is restored straight
after, so nothing else in the process (bench run-tests included) sees the fake. When python-dateutil
is not installed (it comes with frappe), a small ``dateutil.parser.isoparse`` stands in.

Named apart from IDEV-3268's fake_frappe.py so the two branches merge without clashing.
"""

import datetime
import html
import importlib.util
import itertools
import re
import sqlite3
import sys
import types

SCHEMA = """
create table `tabQC Hold` (
	name text primary key, creation text, modified text, owner text, modified_by text,
	docstatus int default 0, idx int default 0,
	invoiced_item_id text unique, episode_key text, status text,
	opened_at text, expires_at text, closed_at text, close_reason text
);
create table `tabDelivery Note` (
	name text primary key, docstatus int default 1, is_return int default 0, posting_date text,
	status text default 'To Bill', custom_invoiced_item_id text,
	custom_department text, custom_billing_location text
);
create table `tabDelivery Note Item` (
	name text primary key, parent text, idx int default 1, so_detail text, against_sales_order text,
	item_code text default 'FUEL', qty real default 0
);
create table `tabSales Order` (
	name text primary key, docstatus int default 1, status text default 'To Deliver and Bill',
	customer text, custom_invoicing_type text, custom_invoicing_frequency text,
	custom_quotation text, custom_last_invoiced_upto text, transaction_date text
);
create table `tabSales Order Item` (name text primary key, parent text, idx int default 1);
create table `tabCustomer` (name text primary key, custom_disable_auto_invoicing int default 0);
create table `tabSales Invoice` (
	name text primary key, docstatus int default 0, is_return int default 0, posting_date text,
	custom_dn_from_date text, custom_dn_to_date text
);
create table `tabSales Invoice Item` (name text primary key, parent text, idx int default 1, so_detail text);
"""

# frappe autonames these doctypes from a field ("autoname": "field:...").
AUTONAME_FIELD = {"QC Hold": "invoiced_item_id"}

TIME_FORMAT = "%Y-%m-%d %H:%M:%S.%f"


def fmt(moment):
	"""A datetime as the fake stores it: fixed width, so text order is time order."""
	return moment.strftime(TIME_FORMAT)


# ---- exceptions (frappe.exceptions) ---------------------------------------------------------------
class ValidationError(Exception):
	http_status_code = 417


class PermissionError(Exception):  # frappe's own name, shadowing the builtin as frappe does
	pass


class TimestampMismatchError(ValidationError):
	pass


class UniqueValidationError(ValidationError):
	pass


class DuplicateEntryError(NameError):
	pass


class QueryDeadlockError(Exception):
	pass


class QueryTimeoutError(Exception):
	pass


class _dict(dict):
	"""frappe._dict: a dict whose keys read as attributes (a missing one reads as None)."""

	__getattr__ = dict.get

	def __setattr__(self, key, value):
		self[key] = value


class Doc:
	"""A loaded or new document: fields as attributes (a missing one reads as None), .get(),
	and for Sales Invoice the child rows under ``items``."""

	def __init__(self, fake=None, **fields):
		self.__dict__["_fake"] = fake
		self.__dict__.update(fields)

	def __getattr__(self, key):
		return None

	def get(self, key, default=None):
		return self.__dict__.get(key, default)

	def insert(self, ignore_permissions=False, **kwargs):
		fake = self._fake
		row = {k: v for k, v in self.__dict__.items() if not k.startswith("_") and k != "doctype"}
		if self.doctype in AUTONAME_FIELD:
			row["name"] = row[AUTONAME_FIELD[self.doctype]]
		row.setdefault("creation", fmt(fake.now))
		row.setdefault("modified", fmt(fake.now))
		fake.db.write_row(self.doctype, row)
		fake.inserted.append((self.doctype, row["name"], ignore_permissions))
		self.name = row["name"]
		return self

	def get_doc_before_save(self):
		return self.__dict__.get("_before")


# ---- SQL ------------------------------------------------------------------------------------------
_TOKEN = re.compile(r"%\((\w+)\)s|%s")
_FOR_UPDATE = re.compile(r"\s+for\s+update\b", re.IGNORECASE)


def _param(value):
	if isinstance(value, datetime.datetime):
		return fmt(value)
	if isinstance(value, datetime.date):
		return value.isoformat()
	return value


def adapt(query, values):
	"""pymysql-style parameters -> SQLite ``?``; a list/tuple value expands to ``(?, ?, ...)``."""
	if values is None:
		seq = iter(())
	elif isinstance(values, dict):
		seq = None
	elif isinstance(values, list | tuple):
		seq = iter(values)
	else:
		seq = iter((values,))
	params = []

	def repl(match):
		value = values[match.group(1)] if match.group(1) else next(seq)
		if isinstance(value, list | tuple | set):
			value = list(value)
			params.extend(_param(v) for v in value)
			return "(" + ", ".join("?" * len(value)) + ")" if value else "(null)"
		params.append(_param(value))
		return "?"

	return _TOKEN.sub(repl, _FOR_UPDATE.sub("", query)), params


class FakeDB:
	def __init__(self, fake):
		self.fake = fake
		self.conn = sqlite3.connect(":memory:")
		self.conn.executescript(SCHEMA)
		self.conn.create_function("utc_timestamp", -1, lambda *precision: fmt(fake.now))
		self.log = []  # every statement as the module wrote it (whitespace folded), plus commit/rollback
		self.calls = []  # (statement, values) for every sql() call
		self.commits = 0
		self.rollbacks = 0
		self.fail_next = None  # an exception the next sql() call raises (lock wait, deadlock, ...)

	def sql(self, query, values=None, as_dict=False, pluck=False, **kwargs):
		self.log.append(" ".join(query.split()))
		self.calls.append((self.log[-1], values))
		if self.fail_next is not None:
			exc, self.fail_next = self.fail_next, None
			raise exc
		text, params = adapt(query, values)
		cur = self.conn.execute(text, params)
		if cur.description is None:
			return ()
		rows = cur.fetchall()
		if as_dict:
			cols = [d[0] for d in cur.description]
			return [_dict(zip(cols, row, strict=True)) for row in rows]
		if pluck:
			return [row[0] for row in rows]
		return tuple(tuple(row) for row in rows)

	def sql_list(self, query, values=None, **kwargs):
		return [row[0] for row in self.sql(query, values)]

	def get_value(self, doctype, name, fieldname, as_dict=False, **kwargs):
		fields = [fieldname] if isinstance(fieldname, str) else list(fieldname)
		cols = ", ".join(f"`{f}`" for f in fields)
		rows = self.sql(f"select {cols} from `tab{doctype}` where name = %s", name, as_dict=True)
		if not rows:
			return None
		return rows[0] if as_dict else rows[0][fields[0]] if len(fields) == 1 else tuple(rows[0].values())

	def set_value(self, doctype, name, fieldname, value=None, **kwargs):
		updates = dict(fieldname) if isinstance(fieldname, dict) else {fieldname: value}
		cols = ", ".join(f"`{f}` = %({f})s" for f in updates)
		self.sql(f"update `tab{doctype}` set {cols} where name = %(__name)s", {**updates, "__name": name})

	def get_single_value(self, doctype, field):
		return self.fake.singles.get((doctype, field))

	def commit(self):
		self.commits += 1
		self.log.append("commit")
		self.conn.commit()

	def rollback(self, save_point=None):
		self.rollbacks += 1
		self.log.append("rollback")
		self.conn.rollback()

	def is_deadlocked(self, exc):
		return False

	def is_timedout(self, exc):
		return False

	def write_row(self, table, row):
		"""A document insert made by the code under test: part of its transaction (not committed)."""
		cols = ", ".join(f"`{c}`" for c in row)
		try:
			self.conn.execute(
				f"insert into `tab{table}` ({cols}) values ({', '.join('?' * len(row))})",
				[_param(v) for v in row.values()],
			)
		except sqlite3.IntegrityError as exc:
			raise DuplicateEntryError(table, row.get("name"), exc)

	# -- helpers for tests ----------------------------------------------------------------------
	def insert(self, table, **row):
		"""A fixture row, committed, so a rollback in the code under test keeps it."""
		self.write_row(table, row)
		self.conn.commit()

	def rows(self, query, *params):
		return self.conn.execute(query, params).fetchall()

	def row(self, table, name):
		cur = self.conn.execute(f"select * from `tab{table}` where name = ?", (name,))
		values = cur.fetchone()
		return _dict(zip([d[0] for d in cur.description], values, strict=True)) if values else None


# ---- the fake module ---------------------------------------------------------------------------
class _Logger:
	def __init__(self, sink, name):
		self.sink, self.name = sink, name

	def info(self, msg, *a, **k):
		self.sink.append((self.name, "info", msg))

	def warning(self, msg, *a, **k):
		self.sink.append((self.name, "warning", msg))

	def error(self, msg, *a, **k):
		self.sink.append((self.name, "error", msg))


class _Cache:
	def __init__(self):
		self.values = {}
		self.broken = False

	def get_value(self, key, *args, **kwargs):
		if self.broken:
			raise ConnectionError("redis down")
		return self.values.get(key)

	def set_value(self, key, value, *args, expires_in_sec=None, **kwargs):
		if self.broken:
			raise ConnectionError("redis down")
		self.values[key] = (value, expires_in_sec)


def _getdate(value=None):
	if value is None or value == "":
		return None
	if isinstance(value, datetime.datetime):
		return value.date()
	if isinstance(value, datetime.date):
		return value
	return datetime.date.fromisoformat(str(value)[:10])


def _get_datetime(value=None):
	if value is None or value == "":
		return None
	if isinstance(value, datetime.datetime):
		return value
	return datetime.datetime.fromisoformat(str(value))


def _flt(value, precision=None):
	try:
		num = float(value.replace(",", "") if isinstance(value, str) else value)
	except (TypeError, ValueError):
		return 0.0
	return round(num, precision) if precision is not None else num


def make(now="2026-10-15 08:00:00", today="2026-10-15"):
	"""A fresh fake ``frappe`` module with an empty database. ``now`` is the database clock (UTC)
	and ``today`` the site date; tests may change ``fake.now`` / ``fake.today`` at any point."""
	fake = types.ModuleType("frappe")
	fake.now = datetime.datetime.fromisoformat(now)
	fake.today = today
	fake.db = FakeDB(fake)
	fake.flags = _dict(in_install=False, in_migrate=False)
	fake.singles = {}
	fake.permitted = True
	fake.permission_checks = []
	fake.whitelisted = {}  # function name -> methods
	fake.inserted = []  # (doctype, name, ignore_permissions)
	fake.thrown = []  # (exception, title)
	fake.logged = []  # (logger name, level, message)
	fake.cleared_messages = 0
	fake.cache = _Cache()
	fake._dict = _dict

	for exc in (
		ValidationError,
		PermissionError,
		TimestampMismatchError,
		UniqueValidationError,
		DuplicateEntryError,
		QueryDeadlockError,
		QueryTimeoutError,
	):
		setattr(fake, exc.__name__, exc)
	fake.DoesNotExistError = type("DoesNotExistError", (ValidationError,), {})

	def _(msg, *args, **kwargs):
		return msg

	def whitelist(allow_guest=False, xss_safe=False, methods=None):
		def wrap(fn):
			fake.whitelisted[fn.__name__] = methods
			return fn

		return wrap

	def throw(msg, exc=ValidationError, title=None, **kwargs):
		# frappe.msgprint(raise_exception=...): a class is instantiated with the message, an
		# instance gets it as its args.
		if isinstance(exc, type):
			exc = exc(msg)
		else:
			exc.args = (msg,)
		fake.thrown.append((exc, title))
		raise exc

	def has_permission(doctype=None, ptype="read", *args, **kwargs):
		fake.permission_checks.append((doctype, ptype))
		return fake.permitted

	def clear_messages():
		fake.cleared_messages += 1

	def logger(module=None, *args, **kwargs):
		return _Logger(fake.logged, module)

	def get_all(doctype, filters=None, pluck=None, fields=None, order_by=None, **kwargs):
		where, params = [], []
		for key, cond in (filters or {}).items():
			where.append(f"`{key}` = %s")
			params.append(cond)
		query = f"select {pluck or ', '.join(fields or ['name'])} from `tab{doctype}`"
		if where:
			query += " where " + " and ".join(where)
		query += f" order by {order_by or 'name'}"
		if pluck:
			return fake.db.sql_list(query, params)
		return fake.db.sql(query, params, as_dict=True)

	def get_doc(doctype, name=None, **kwargs):
		if isinstance(doctype, dict):
			return Doc(fake, **doctype)
		row = fake.db.row(doctype, name)
		if row is None:
			raise fake.DoesNotExistError(f"{doctype} {name} not found")
		return Doc(fake, doctype=doctype, **row)

	fake._ = _
	fake.whitelist = whitelist
	fake.throw = throw
	fake.has_permission = has_permission
	fake.clear_messages = clear_messages
	fake.logger = logger
	fake.get_all = get_all
	fake.get_doc = get_doc
	fake.get_traceback = lambda *a, **k: "traceback"
	fake.log_error = lambda *a, **k: fake.logged.append(("error_log", "error", k.get("title")))

	utils = types.ModuleType("frappe.utils")
	utils.flt = _flt
	utils.cint = lambda v: int(_flt(v))
	utils.cstr = lambda v: "" if v is None else str(v)
	utils.getdate = _getdate
	utils.get_datetime = _get_datetime
	utils.nowdate = lambda: fake.today
	utils.add_days = lambda d, n: _getdate(d) + datetime.timedelta(days=n)
	utils.get_first_day = lambda d: _getdate(d).replace(day=1)
	utils.get_system_timezone = lambda: "Asia/Dubai"
	utils.strip_html = lambda text: re.sub(r"<[^>]*>", "", str(text))
	utils.escape_html = lambda text: html.escape(text, quote=True) if isinstance(text, str) else text
	utils.get_link_to_form = (
		lambda doctype,
		name,
		label=None: f'<a href="/app/{doctype.lower().replace(" ", "-")}/{name}">{label or name}</a>'
	)
	fake.utils = utils
	return fake


# ---- loading a module against the fake ------------------------------------------------------------
def _isoparse(text):
	return datetime.datetime.fromisoformat(text.replace("Z", "+00:00") if text.endswith("Z") else text)


def _dateutil_stub():
	package = types.ModuleType("dateutil")
	parser = types.ModuleType("dateutil.parser")
	parser.isoparse = _isoparse
	package.parser = parser
	return {"dateutil": package, "dateutil.parser": parser}


def _has_dateutil():
	try:
		return importlib.util.find_spec("dateutil") is not None
	except (ImportError, ValueError):
		return False


_SEQ = itertools.count()


def load(path, fake):
	"""Execute the module file at ``path`` with ``fake`` as its frappe; returns the module. Its
	fuelbuddy_crm imports are fresh copies bound to the same fake."""
	stubs = {"frappe": fake, "frappe.utils": fake.utils}
	if not _has_dateutil():
		stubs.update(_dateutil_stub())
	ours = [key for key in sys.modules if key.startswith("fuelbuddy_crm.")]
	saved = {key: sys.modules.get(key) for key in (*stubs, *ours)}
	for key in ours:
		del sys.modules[key]
	sys.modules.update(stubs)
	try:
		spec = importlib.util.spec_from_file_location(f"_qc_hold_fake_mod_{next(_SEQ)}", path)
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
	finally:
		for key in [key for key in sys.modules if key.startswith("fuelbuddy_crm.")]:
			del sys.modules[key]
		for key, value in saved.items():
			if value is None:
				sys.modules.pop(key, None)
			else:
				sys.modules[key] = value
	return module
