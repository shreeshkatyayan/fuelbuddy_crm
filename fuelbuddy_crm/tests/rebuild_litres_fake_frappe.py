# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""A stand-in for the frappe calls the manual invoice rebuild makes, on an in-memory SQLite
database, so auto_invoicing.rebuild_lines_from_dn_range and dn_unbilled_litres run as plain Python with
no bench and no site (IDEV-3270).

It is not frappe: no MariaDB, no DECIMAL columns, no document controllers. Tests on it check which
rows the rebuild takes and the quantities it puts on the invoice; ERPNext's own billing status is
written by the tests as ERPNext would leave it. The site test (test_manual_invoice_rebuild) checks the
same cases on a real site.

``load(fake)`` executes auto_invoicing.py and its fuelbuddy_crm imports with this fake bound as
their ``frappe``. sys.modules is restored straight after, so nothing else in the process (bench
run-tests included) sees the fake. Named apart from IDEV-3268's fake_frappe.py and IDEV-3266's
qc_hold_fake_frappe.py so the branches merge without clashing.
"""

import datetime
import importlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import types

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCHEMA = """
create table `tabDelivery Note` (
	name text primary key, docstatus int default 1, is_return int default 0,
	status text default 'To Bill', posting_date text, posting_time text default '10:00:00'
);
create table `tabDelivery Note Item` (
	name text primary key, parent text, idx int default 1, item_code text default 'FUEL',
	so_detail text, si_detail text, dn_detail text,
	qty real default 0, stock_qty real default 0, conversion_factor real default 1,
	amount real default 0, billed_amt real default 0
);
create table `tabSales Invoice` (
	name text primary key, docstatus int default 0, is_return int default 0, update_stock int default 0
);
create table `tabSales Invoice Item` (
	name text primary key, parent text, idx int default 1, so_detail text, dn_detail text,
	qty real default 0, stock_qty real default 0, conversion_factor real default 1
);
"""


class ValidationError(Exception):
	pass


class _dict(dict):
	"""frappe._dict: a dict whose keys read as attributes (a missing one reads as None)."""

	__getattr__ = dict.get

	def __setattr__(self, key, value):
		self[key] = value


class Row(_dict):
	"""A child row of a document being built: fields as attributes, as_dict()."""

	def as_dict(self, no_default_fields=False):
		return _dict(self)


class Invoice:
	"""A new Sales Invoice as the mapper leaves it: items with sales_order / so_detail."""

	def __init__(self, customer, items, **fields):
		self.doctype = "Sales Invoice"
		self.customer = customer
		self.flags = _dict()
		self.fields = _dict(fields)
		self.items = [Row(idx=i, **row) for i, row in enumerate(items, 1)]

	def is_new(self):
		return True

	def get(self, key, default=None):
		if key == "items":
			return self.items
		return self.fields.get(key, default)

	def set(self, key, value):
		assert key == "items"
		self.items = [Row(row) for row in value]

	def append(self, key, row):
		assert key == "items"
		row = Row(row)
		self.items.append(row)
		return row


# ---- frappe.utils --------------------------------------------------------------------------------
def flt(value, precision=None):
	try:
		num = float(value.replace(",", "") if isinstance(value, str) else value)
	except (TypeError, ValueError):
		return 0.0
	return round(num, precision) if precision is not None else num


def cint(value):
	try:
		return int(float(value))
	except (TypeError, ValueError):
		return 0


def cstr(value):
	return "" if value is None else str(value)


def getdate(value=None):
	if value is None:
		return datetime.date.today()
	if isinstance(value, datetime.datetime):
		return value.date()
	if isinstance(value, datetime.date):
		return value
	return datetime.date.fromisoformat(str(value)[:10])


def add_days(value, days):
	return getdate(value) + datetime.timedelta(days=days)


def get_first_day(value):
	return getdate(value).replace(day=1)


def nowdate():
	return datetime.date.today().isoformat()


def strip_html(text):
	return re.sub(r"<[^>]+>", "", cstr(text))


# ---- frappe.db -----------------------------------------------------------------------------------
_TOKEN = re.compile(r"%\((\w+)\)s|%s")


def adapt(query, values):
	"""pymysql-style parameters -> SQLite ``?``; a list/tuple value expands to ``(?, ?, ...)``."""
	seq = iter(values if isinstance(values, list | tuple) else () if values is None else (values,))
	params = []

	def repl(match):
		value = values[match.group(1)] if match.group(1) else next(seq)
		if isinstance(value, list | tuple | set):
			value = list(value)
			params.extend(value)
			return "(" + ", ".join("?" * len(value)) + ")" if value else "(null)"
		params.append(value)
		return "?"

	return _TOKEN.sub(repl, query), params


class FakeDB:
	def __init__(self):
		self.conn = sqlite3.connect(":memory:")
		self.conn.create_function("greatest", -1, lambda *args: max(args))  # MariaDB's GREATEST
		self.conn.executescript(SCHEMA)
		self.statements = []

	def sql(self, query, values=None, as_dict=False, **kwargs):
		self.statements.append(" ".join(query.split()))
		text, params = adapt(query, values)
		cur = self.conn.execute(text, params)
		if cur.description is None:
			return ()
		rows = cur.fetchall()
		if as_dict:
			cols = [d[0] for d in cur.description]
			return [_dict(zip(cols, row, strict=True)) for row in rows]
		return tuple(tuple(row) for row in rows)

	def insert(self, table, **fields):
		cols = ", ".join(f"`{k}`" for k in fields)
		marks = ", ".join("?" * len(fields))
		self.conn.execute(f"insert into `tab{table}` ({cols}) values ({marks})", list(fields.values()))


class Logger:
	def __init__(self):
		self.warnings = []

	def warning(self, message):
		self.warnings.append(message)

	info = error = warning


def make(site_config=None, common_config=None):
	"""A fresh fake frappe. ``site_config``: the site's site_config.json contents (None: no file);
	``common_config``: what frappe.conf would add from common_site_config.json."""
	fake = types.ModuleType("frappe")
	fake.__file__ = __file__
	fake._dict = _dict
	fake._ = lambda text, *args: text
	fake.ValidationError = ValidationError
	fake.db = FakeDB()
	fake.messages = []
	fake.log = Logger()
	fake.logger = lambda *args, **kwargs: fake.log

	def throw(message, exc=ValidationError, *args, **kwargs):
		raise exc(message)

	def msgprint(message, *args, **kwargs):
		fake.messages.append(message)

	fake.throw = throw
	fake.msgprint = msgprint
	fake.get_cached_doc = lambda doctype, name=None: _dict()  # Fuelbuddy Settings: Force Majeure off
	fake.get_all = lambda *args, **kwargs: []

	site = tempfile.mkdtemp(prefix="rebuild_litres_site_")
	if site_config is not None:
		with open(os.path.join(site, "site_config.json"), "w") as fh:
			json.dump(site_config, fh)
	fake.local = types.SimpleNamespace(site_path=site)
	fake.conf = _dict({**(common_config or {}), **(site_config or {})})

	utils = types.ModuleType("frappe.utils")
	for fn in (flt, cint, cstr, getdate, add_days, get_first_day, nowdate, strip_html):
		setattr(utils, fn.__name__, fn)
	fake.utils = utils
	return fake


def dispose(fake):
	"""Close the fake's database and remove its site folder."""
	fake.db.conn.close()
	shutil.rmtree(fake.local.site_path, ignore_errors=True)


def load(fake, module="auto_invoicing"):
	"""Execute ``fuelbuddy_crm.<module>`` and its fuelbuddy_crm imports with ``fake`` as their
	frappe; returns the module. sys.modules is restored straight after."""
	package = types.ModuleType("fuelbuddy_crm")
	package.__path__ = [APP]
	stubs = {"frappe": fake, "frappe.utils": fake.utils, "fuelbuddy_crm": package}
	ours = [key for key in sys.modules if key.startswith("fuelbuddy_crm.")]
	saved = {key: sys.modules.get(key) for key in (*stubs, *ours)}
	for key in ours:
		del sys.modules[key]
	sys.modules.update(stubs)
	try:
		return importlib.import_module(f"fuelbuddy_crm.{module}")
	finally:
		for key in [key for key in sys.modules if key.startswith("fuelbuddy_crm.")]:
			del sys.modules[key]
		for key, value in saved.items():
			if value is None:
				sys.modules.pop(key, None)
			else:
				sys.modules[key] = value
