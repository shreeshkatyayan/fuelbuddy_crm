# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""A stand-in for the few frappe calls the billing modules make, on an in-memory SQLite database,
so their logic runs as plain Python with no bench and no site (IDEV-3268).

It is not frappe: SQLite has no row locks, no REPEATABLE READ snapshot and no DECIMAL columns.
Tests on it check which statements run, in what order, and the rows they leave. The *_site.py
tests check the MariaDB-only parts (locks, DECIMAL round trips) on a real site.

``load(path, fake)`` executes a module file with this fake bound as its ``frappe``. sys.modules is
restored straight after, so nothing else in the process (bench run-tests included) sees the fake.
"""

import importlib.util
import itertools
import re
import sqlite3
import sys
import types

SCHEMA = """
create table `tabSales Order Item` (name text primary key, billed_amt real default 0);
create table `tabDelivery Note` (
	name text primary key, docstatus int default 1, is_return int default 0,
	posting_date text, posting_time text default '10:00:00',
	custom_sales_invoice text, custom_sales_invoice_qty real default 0,
	custom_department text, custom_billing_location text,
	per_billed real default 0, per_returned real default 0, status text default 'To Bill',
	grand_total real default 0
);
create table `tabDelivery Note Item` (
	name text primary key, parent text, parenttype text default 'Delivery Note', idx int default 1,
	so_detail text, si_detail text, stock_qty real default 0, amount real default 0,
	billed_amt real default 0, returned_qty real default 0, rate real default 0
);
create table `tabSales Invoice` (
	name text primary key, docstatus int default 0, is_return int default 0, posting_date text,
	creation text, custom_dn_from_date text, custom_dn_to_date text,
	custom_department text, custom_location text
);
create table `tabSales Invoice Item` (
	name text primary key, parent text, idx int default 1, so_detail text, sales_order text,
	stock_qty real default 0, qty real default 0, conversion_factor real default 1
);
"""


class _dict(dict):
	"""frappe._dict: a dict whose keys read as attributes (a missing one reads as None)."""

	__getattr__ = dict.get

	def __setattr__(self, key, value):
		self[key] = value


class Doc:
	"""A loaded document: fields as attributes (a missing one reads as None, as on a frappe
	Document), .get(), and child rows as lists of _dict. Not a dict, so doc.items is the rows."""

	def __init__(self, **fields):
		self.__dict__.update(fields)

	def __getattr__(self, key):
		return None

	def get(self, key, default=None):
		return self.__dict__.get(key, default)

	def as_dict(self):
		return _dict(self.__dict__)


def flt(value, precision=None):
	try:
		num = float(value.replace(",", "") if isinstance(value, str) else value)
	except (TypeError, ValueError):
		return 0.0
	return round(num, precision) if precision is not None else num


_TOKEN = re.compile(r"%\((\w+)\)s|%s")
_FOR_UPDATE = re.compile(r"\s+for\s+update\b", re.IGNORECASE)


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
			params.extend(value)
			return "(" + ", ".join("?" * len(value)) + ")" if value else "(null)"
		params.append(value)
		return "?"

	return _TOKEN.sub(repl, _FOR_UPDATE.sub("", query)), params


class FakeDB:
	def __init__(self):
		self.conn = sqlite3.connect(":memory:")
		self.conn.executescript(SCHEMA)
		self.log = []  # every statement as the module wrote it (whitespace folded), plus commit/rollback
		self.calls = []  # (statement, values) for every sql() call
		self.commits = 0
		self.rollbacks = 0

	def __del__(self):
		self.conn.close()

	def sql(self, query, values=None, as_dict=False, pluck=False, **kwargs):
		self.log.append(" ".join(query.split()))
		self.calls.append((self.log[-1], values))
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

	def get_value(self, doctype, name, fieldname, **kwargs):
		rows = self.sql(f"select `{fieldname}` from `tab{doctype}` where name = %s", name)
		return rows[0][0] if rows else None

	def commit(self):
		self.commits += 1
		self.log.append("commit")
		self.conn.commit()

	def rollback(self, save_point=None):
		self.rollbacks += 1
		self.log.append("rollback")
		self.conn.rollback()

	# -- helpers for tests ----------------------------------------------------------------------
	def insert(self, table, **row):
		cols = ", ".join(f"`{c}`" for c in row)
		self.conn.execute(
			f"insert into `tab{table}` ({cols}) values ({', '.join('?' * len(row))})", list(row.values())
		)

	def rows(self, query, *params):
		return self.conn.execute(query, params).fetchall()

	def writes(self):
		return [q for q in self.log if q.split(" ", 1)[0].lower() in ("update", "insert", "delete")]


class _Logger:
	def __init__(self, sink):
		self.sink = sink

	def _log(self, level, msg, *args, **kwargs):
		self.sink.append((level, msg))

	def info(self, msg, *a, **k):
		self._log("info", msg)

	def warning(self, msg, *a, **k):
		self._log("warning", msg)

	def error(self, msg, *a, **k):
		self._log("error", msg)


def make():
	"""A fresh fake ``frappe`` module with an empty database."""
	fake = types.ModuleType("frappe")
	fake.db = FakeDB()
	fake.flags = _dict(in_install=False, in_migrate=False)
	fake.conf = _dict()
	fake.local = _dict()
	fake._dict = _dict
	fake.messages = []
	fake.errors = []  # frappe.log_error calls
	fake.logged = []  # frappe.logger() lines
	fake.cache_cleared = []
	fake.doc_hooks = {}  # doctype -> callable(doc) adding methods to loaded docs

	def _(msg, *args, **kwargs):
		return msg

	def msgprint(msg, **kwargs):
		fake.messages.append(msg)

	def clear_document_cache(doctype, name=None):
		fake.cache_cleared.append((doctype, name))

	def log_error(title=None, message=None, reference_doctype=None, reference_name=None, **kwargs):
		fake.errors.append(_dict(title=title, message=message, **kwargs))

	def logger(*args, **kwargs):
		return _Logger(fake.logged)

	def safe_eval(code, eval_globals=None, eval_locals=None):
		return eval(code, {"__builtins__": {}}, dict(eval_locals or {}))

	def get_all(doctype, filters=None, order_by=None, pluck=None, fields=None, **kwargs):
		where, params = [], []
		for key, cond in (filters or {}).items():
			if isinstance(cond, list | tuple) and cond[0] == "in":
				where.append(f"`{key}` in %s")
				params.append(list(cond[1]))
			else:
				where.append(f"`{key}` = %s")
				params.append(cond)
		cols = pluck or ", ".join(fields or ["name"])
		query = f"select {cols} from `tab{doctype}`"
		if where:
			query += " where " + " and ".join(where)
		if order_by:
			query += f" order by {order_by}"
		if pluck:
			return fake.db.sql_list(query, params)
		return fake.db.sql(query, params, as_dict=True)

	def get_doc(doctype, name):
		row = fake.db.sql(f"select * from `tab{doctype}` where name = %s", name, as_dict=True)
		if not row:
			raise LookupError(f"{doctype} {name} not found")
		doc = Doc(doctype=doctype, **row[0])
		child = {"Sales Invoice": "Sales Invoice Item", "Delivery Note": "Delivery Note Item"}.get(doctype)
		if child:
			doc.items = fake.db.sql(
				f"select * from `tab{child}` where parent = %s order by idx", name, as_dict=True
			)
		if doctype in fake.doc_hooks:
			fake.doc_hooks[doctype](doc)
		return doc

	fake._ = _
	fake.msgprint = msgprint
	fake.clear_document_cache = clear_document_cache
	fake.log_error = log_error
	fake.logger = logger
	fake.safe_eval = safe_eval
	fake.get_all = get_all
	fake.get_doc = get_doc

	utils = types.ModuleType("frappe.utils")
	utils.flt = flt
	utils.cint = lambda v: int(flt(v))
	utils.getdate = lambda v=None: v
	utils.nowdate = lambda: "2026-09-27"
	fake.utils = utils
	return fake


_SEQ = itertools.count()


def load(path, fake):
	"""Execute the module file at ``path`` with ``fake`` as its frappe; returns the module."""
	saved = {key: sys.modules.get(key) for key in ("frappe", "frappe.utils")}
	sys.modules["frappe"], sys.modules["frappe.utils"] = fake, fake.utils
	try:
		spec = importlib.util.spec_from_file_location(f"_fake_frappe_mod_{next(_SEQ)}", path)
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
	finally:
		for key, value in saved.items():
			if value is None:
				sys.modules.pop(key, None)
			else:
				sys.modules[key] = value
	return module
