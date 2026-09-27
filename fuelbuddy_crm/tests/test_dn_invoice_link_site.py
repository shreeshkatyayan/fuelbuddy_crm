# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""dn_invoice_link on a real site (IDEV-3268), for the lab phase: what the SQLite tests cannot show.

    bench --site <site> run-tests --module fuelbuddy_crm.tests.test_dn_invoice_link_site

- The Sales Order line lock really holds against another connection (InnoDB row lock).
- The write-only-changed path skips rows on MariaDB's DECIMAL(21,9) column: frappe reads it back
  as a float that, below 2**23, equals the float that was written.
- Allocation, frontier and re-run on MariaDB with the real queries.

Fixture rows are raw INSERTs inside the test transaction (no controllers run) and are rolled
back after every test. The lock test reads one existing Sales Order Item row and writes nothing.
Skips when the production-only columns dn_invoice_link reads are missing (a fresh test site
without fuelbuddy_crm.tests.qc_fixtures.ensure_prod_schema).

Not automated here: an invoice save and a DN cancel racing on one hot line. Lab scenario: in two
bench consoles, open a Sales Invoice save (allocate_sales_invoice, do not commit) and cancel an
in-window DN of that line; the cancel must wait on the SO line until the first commits, and a
re-run of allocate_sales_invoice afterwards must write no DN rows (fixpoint).
"""

import types
import uuid

import frappe
from frappe.tests.utils import FrappeTestCase

from fuelbuddy_crm import dn_invoice_link as dil

_COLUMNS = {
	"Delivery Note": (dil.LINK_FIELD, dil.QTY_FIELD, "custom_department", "custom_billing_location"),
	"Sales Invoice": ("custom_dn_from_date", "custom_dn_to_date", "custom_department", "custom_location"),
}


class TestDnInvoiceLinkSite(FrappeTestCase):
	def setUp(self):
		missing = [
			f"{dt}.{col}"
			for dt, cols in _COLUMNS.items()
			for col in cols
			if not frappe.db.has_column(dt, col)
		]
		if missing:
			self.skipTest("production-only columns missing: " + ", ".join(missing))
		self.tag = "T3268-" + uuid.uuid4().hex[:8]
		self.line = f"{self.tag}-SOI"
		frappe.db.sql(
			"insert into `tabSales Order Item` (name, parenttype, parentfield, idx) values (%s, 'Sales Order', 'items', 1)",
			self.line,
		)

	def tearDown(self):
		frappe.db.rollback()

	# -- fixtures (raw rows: no controller, no hook) --------------------------------------------
	def dn(self, suffix, date, litres):
		name = f"{self.tag}-{suffix}"
		frappe.db.sql(
			"""insert into `tabDelivery Note` (name, docstatus, is_return, posting_date, posting_time)
			values (%s, 1, 0, %s, '10:00:00')""",
			(name, date),
		)
		frappe.db.sql(
			"""insert into `tabDelivery Note Item` (name, parent, parenttype, parentfield, idx, so_detail, stock_qty)
			values (%s, %s, 'Delivery Note', 'items', 1, %s, %s)""",
			(f"{name}-1", name, self.line, litres),
		)
		return name

	def si(self, suffix, litres, docstatus=0):
		name = f"{self.tag}-{suffix}"
		frappe.db.sql(
			"""insert into `tabSales Invoice` (name, docstatus, is_return, posting_date, creation,
				custom_dn_from_date, custom_dn_to_date)
			values (%s, %s, 0, '2026-09-30', now(), '2026-09-01', '2026-09-30')""",
			(name, docstatus),
		)
		frappe.db.sql(
			"""insert into `tabSales Invoice Item` (name, parent, parenttype, parentfield, idx, so_detail,
				stock_qty, qty, conversion_factor)
			values (%s, %s, 'Sales Invoice', 'items', 1, %s, %s, %s, 1)""",
			(f"{name}-1", name, self.line, litres, litres),
		)
		return frappe.get_doc("Sales Invoice", name)

	def links(self):
		return {
			r.name: (r.link, r.qty)
			for r in frappe.db.sql(
				f"""select name, `{dil.LINK_FIELD}` as link, `{dil.QTY_FIELD}` as qty from `tabDelivery Note`
				where name like %s and ifnull(`{dil.LINK_FIELD}`, '') != ''""",
				f"{self.tag}-%",
				as_dict=True,
			)
		}

	def dn_updates(self, fn, *args):
		"""Run fn; return the DN names each DN UPDATE it issued names (one list per statement)."""
		seen = []
		real = frappe.db.sql

		def spy(query, values=None, *a, **k):
			if query.lstrip().lower().startswith("update `tabdelivery note`"):
				seen.append(sorted(values["names"] if isinstance(values, dict) else values[-1]))
			return real(query, values, *a, **k)

		frappe.db.sql = spy
		try:
			fn(*args)
		finally:
			frappe.db.sql = real
		return seen

	# -- tests ----------------------------------------------------------------------------------
	def test_frontier_and_rerun_on_mariadb(self):
		a = self.dn("A", "2026-09-01", 100)
		b = self.dn("B", "2026-09-02", 100)
		c = self.dn("C", "2026-09-03", 100)
		si = self.si("SI1", 250)
		dil.allocate_sales_invoice(si)
		self.assertEqual(self.links(), {a: (si.name, 100), b: (si.name, 100), c: (si.name, 50)})
		rerun = self.dn_updates(dil.allocate_sales_invoice, frappe.get_doc("Sales Invoice", si.name))
		self.assertEqual(rerun, [])

		frappe.db.set_value("Sales Invoice Item", f"{si.name}-1", {"stock_qty": 150, "qty": 150})
		shrink = self.dn_updates(dil.allocate_sales_invoice, frappe.get_doc("Sales Invoice", si.name))
		self.assertEqual(shrink, [[c], [b]])  # clear DN-C, stamp DN-B; DN-A untouched
		self.assertEqual(self.links(), {a: (si.name, 100), b: (si.name, 50)})

	def test_decimal_round_trip_band(self):
		# Values a DN column can hold exactly (<= 9 decimals, below 2**23) are not rewritten: frappe
		# reads the DECIMAL back as the float that was written. A float carrying more digits than
		# the column keeps (0.1 + 0.2: a FIFO remainder) reads back different and is rewritten, to
		# the same stored value. At or above 2**23 the value is always rewritten.
		exact = [0.3, 1234.567890123, 9.9, 99999.999999999, 5_000_000.5]
		drift, big = 0.1 + 0.2, 2.0**23 + 0.5
		values = [*exact, drift, big]
		names = [self.dn(f"R{i}", "2026-09-01", 1) for i in range(len(values))]
		takes = dict(zip(names, values, strict=True))
		si_name = f"{self.tag}-SIX"
		dil._write_links(si_name, takes)
		self.assertEqual(self.dn_updates(dil._write_links, si_name, takes), [sorted(names[-2:])])

	def test_so_line_lock_holds_against_another_connection(self):
		import pymysql

		existing = frappe.db.sql_list(
			"select name from `tabSales Order Item` where name not like %s limit 1", "T3268-%"
		)
		if not existing:
			self.skipTest("no committed Sales Order Item row to lock")
		line = existing[0]
		dil.lock_so_lines(
			types.SimpleNamespace(items=[frappe._dict(so_detail=line)], get={"is_return": 0}.get)
		)

		conf = frappe.conf
		other = pymysql.connect(
			host=conf.get("db_host") or "127.0.0.1",
			port=int(conf.get("db_port") or 3306),
			user=conf.get("db_user") or conf.db_name,
			password=conf.db_password,
			database=conf.db_name,
		)
		lock = "select name from `tabSales Order Item` where name = %s for update"
		try:
			with other.cursor() as cur:
				cur.execute("set session innodb_lock_wait_timeout = 1")
				with self.assertRaises(pymysql.err.OperationalError) as ctx:
					cur.execute(lock, (line,))
				self.assertEqual(ctx.exception.args[0], 1205)  # lock wait timeout
			frappe.db.rollback()  # our transaction ends: the line is free
			with other.cursor() as cur:
				cur.execute(lock, (line,))
			other.rollback()
		finally:
			other.close()
