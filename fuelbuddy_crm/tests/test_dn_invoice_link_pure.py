# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""dn_invoice_link without a site (IDEV-3268): the write-only-changed path and the SO line lock.

Runs as plain Python (``python3 -m unittest discover -s fuelbuddy_crm/tests -p "test_*_pure.py"``)
and under ``bench run-tests``; the module under test is loaded with a SQLite-backed fake frappe
(fake_frappe.py), never the real one. MariaDB-only behaviour (row locks, DECIMAL round trip) is in
test_dn_invoice_link_site.py.

The oracle for "same end state" is the allocation as it was before IDEV-3268 (origin/main
0699a46): clear every DN the invoice holds, then stamp every take (``install_old`` below).
"""

import os
import random
import unittest

try:
	from . import fake_frappe as ff
except ImportError:  # run as a top-level module by unittest discover
	import fake_frappe as ff

MODULE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dn_invoice_link.py")
LOCK_SQL = "select name from `tabSales Order Item` where name in %(names)s order by name for update"


def install_old(mod):
	"""Swap in the pre-IDEV-3268 allocation (clear everything the invoice holds, re-stamp every
	take) on a loaded module, so reallocate_for_delivery_note calls it too."""
	frappe = mod.frappe

	def allocate_sales_invoice(doc, method=None, exclude_dn=None):
		if frappe.flags.in_install or frappe.flags.in_migrate:
			return
		mod._clear(doc.name)
		if doc.docstatus == 2 or doc.get("is_return"):
			return
		takes = {}
		for item in doc.items:
			if not item.so_detail:
				continue
			short = mod._allocate_row(doc, item, takes, exclude_dn)
			if short > mod.EPS:
				frappe.msgprint(
					frappe._(
						"Row {0}: {1} L of {2} L not covered by Delivery Notes on {3} between {4} and {5}"
					).format(
						item.idx,
						round(short, 3),
						round(mod._litres(item), 3),
						item.sales_order,
						doc.custom_dn_from_date or "-",
						doc.custom_dn_to_date or "-",
					),
					title=frappe._("Delivery Note link"),
					indicator="orange",
				)
		mod._stamp(doc.name, takes)

	mod.allocate_sales_invoice = allocate_sales_invoice
	mod.clear_sales_invoice = lambda doc, method=None: mod._clear(doc.name)


class Site:
	"""One fake site: a fake frappe, the module loaded against it, and fixture helpers."""

	def __init__(self, old=False):
		self.frappe = ff.make()
		self.db = self.frappe.db
		self.m = ff.load(MODULE, self.frappe)
		if old:
			install_old(self.m)
		for line in ("SOI-1", "SOI-2"):
			self.db.insert("Sales Order Item", name=line)

	def dn(
		self, name, date, litres, line="SOI-1", docstatus=1, is_return=0, dept=None, loc=None, time="10:00:00"
	):
		self.db.insert(
			"Delivery Note",
			name=name,
			docstatus=docstatus,
			is_return=is_return,
			posting_date=date,
			posting_time=time,
			custom_department=dept,
			custom_billing_location=loc,
		)
		lines = litres if isinstance(litres, list) else [(line, litres)]
		for idx, (so_detail, qty) in enumerate(lines, 1):
			self.db.insert(
				"Delivery Note Item",
				name=f"{name}-{idx}",
				parent=name,
				idx=idx,
				so_detail=so_detail,
				stock_qty=qty,
			)

	def si(
		self, name, rows, docstatus=0, is_return=0, frm=None, to=None, dept=None, loc=None, date="2026-09-30"
	):
		self.db.insert(
			"Sales Invoice",
			name=name,
			docstatus=docstatus,
			is_return=is_return,
			posting_date=date,
			creation=f"2026-09-30 00:00:{len(self.db.rows('select name from `tabSales Invoice`')):02d}",
			custom_dn_from_date=frm,
			custom_dn_to_date=to,
			custom_department=dept,
			custom_location=loc,
		)
		for idx, (so_detail, litres) in enumerate(rows, 1):
			self.db.insert(
				"Sales Invoice Item",
				name=f"{name}-{idx}",
				parent=name,
				idx=idx,
				so_detail=so_detail,
				sales_order="SO-1" if so_detail else None,
				stock_qty=litres,
				qty=litres,
			)
		return self.doc(name)

	def doc(self, name, doctype="Sales Invoice"):
		return self.frappe.get_doc(doctype, name)

	def set(self, table, name, **values):
		sets = ", ".join(f"`{k}` = ?" for k in values)
		self.db.conn.execute(f"update `tab{table}` set {sets} where name = ?", [*values.values(), name])

	def links(self):
		return {
			name: (si, qty)
			for name, si, qty in self.db.rows(
				"select name, custom_sales_invoice, custom_sales_invoice_qty from `tabDelivery Note` order by name"
			)
			if si
		}


class DnInvoiceLinkCase(unittest.TestCase):
	def setUp(self):
		self.s = Site()
		self.m = self.s.m
		self.db = self.s.db

	def mark(self):
		self._mark = len(self.db.log)
		self._mark_calls = len(self.db.calls)

	def statements(self):
		return self.db.log[self._mark :]

	def dn_updates(self):
		"""(statement, sorted DN names it writes) for each DN UPDATE since mark()."""
		out = []
		for query, values in self.db.calls[self._mark_calls :]:
			if query.lower().startswith("update `tabdelivery note`"):
				names = values["names"] if isinstance(values, dict) else values[-1]
				out.append((query, sorted(names)))
		return out


class TestWriteOnlyChanged(DnInvoiceLinkCase):
	def seed_three(self):
		for name, day in (("DN-A", "2026-09-01"), ("DN-B", "2026-09-02"), ("DN-C", "2026-09-03")):
			self.s.dn(name, day, 100)

	def test_partial_frontier_dn(self):
		self.seed_three()
		si1 = self.s.si("SI-1", [("SOI-1", 250)], frm="2026-09-01", to="2026-09-30")
		self.m.allocate_sales_invoice(si1)
		self.assertEqual(self.s.links(), {"DN-A": ("SI-1", 100), "DN-B": ("SI-1", 100), "DN-C": ("SI-1", 50)})

		# A second invoice on the window finds nothing free: the frontier DN stays with SI-1.
		si2 = self.s.si("SI-2", [("SOI-1", 100)], frm="2026-09-01", to="2026-09-30")
		self.s.frappe.messages.clear()
		self.m.allocate_sales_invoice(si2)
		self.assertEqual(self.s.links()["DN-C"], ("SI-1", 50))
		self.assertEqual(len(self.s.frappe.messages), 1)
		self.assertIn("100", self.s.frappe.messages[0])

		# SI-1 shrinks to 150 L: the frontier moves back to DN-B; DN-C is released; DN-A untouched.
		self.s.set("Sales Invoice Item", "SI-1-1", stock_qty=150, qty=150)
		si1 = self.s.doc("SI-1")
		self.mark()
		self.m.allocate_sales_invoice(si1)
		self.assertEqual(self.s.links(), {"DN-A": ("SI-1", 100), "DN-B": ("SI-1", 50)})
		updates = self.dn_updates()
		self.assertEqual([names for _q, names in updates], [["DN-C"], ["DN-B"]])
		self.assertIn("= NULL", updates[0][0])  # the clear

		# Now SI-2 can take what SI-1 released, and part of nothing else.
		self.m.allocate_sales_invoice(self.s.doc("SI-2"))
		self.assertEqual(self.s.links()["DN-C"], ("SI-2", 100))

	def test_rerun_writes_nothing(self):
		self.seed_three()
		si = self.s.si("SI-1", [("SOI-1", 250)], frm="2026-09-01", to="2026-09-30")
		self.m.allocate_sales_invoice(si)
		si = self.s.doc("SI-1")
		self.mark()
		self.m.allocate_sales_invoice(si)
		self.assertEqual(self.dn_updates(), [])
		self.assertEqual(
			self.s.frappe.cache_cleared[-3:], [("Delivery Note", n) for n in ("DN-A", "DN-B", "DN-C")]
		)

	def test_two_invoice_rows_share_a_dn(self):
		# Force Majeure split: two rows on one SO line; the second row takes the rest of DN-B.
		self.s.dn("DN-A", "2026-09-01", 100)
		self.s.dn("DN-B", "2026-09-02", 200)
		si = self.s.si("SI-1", [("SOI-1", 150), ("SOI-1", 100)])
		self.m.allocate_sales_invoice(si)
		self.assertEqual(self.s.links(), {"DN-A": ("SI-1", 100), "DN-B": ("SI-1", 150)})
		si = self.s.doc("SI-1")
		self.mark()
		self.m.allocate_sales_invoice(si)
		self.assertEqual(self.dn_updates(), [])

	def test_cancelled_invoice_clears_and_return_never_stamps(self):
		self.seed_three()
		si = self.s.si("SI-1", [("SOI-1", 250)], docstatus=1)
		self.m.allocate_sales_invoice(si)
		self.s.set("Sales Invoice", "SI-1", docstatus=2)
		self.m.clear_sales_invoice(self.s.doc("SI-1"))  # on_cancel
		self.assertEqual(self.s.links(), {})

		# allocate on a cancelled invoice (e.g. a DN event re-running it) also leaves nothing
		self.m.allocate_sales_invoice(self.s.doc("SI-1"))
		self.assertEqual(self.s.links(), {})

		# a credit note never takes DNs, and lets go of any it somehow holds
		self.s.set("Delivery Note", "DN-A", custom_sales_invoice="CN-1", custom_sales_invoice_qty=100)
		cn = self.s.si("CN-1", [("SOI-1", -100)], docstatus=1, is_return=1)
		self.m.allocate_sales_invoice(cn)
		self.assertEqual(self.s.links(), {})

	def test_department_and_location_split(self):
		self.s.dn("DN-A", "2026-09-01", 100, dept="Ops", loc="Dubai")
		self.s.dn("DN-B", "2026-09-02", 100, dept="Fin", loc="Dubai")
		self.s.dn("DN-C", "2026-09-03", 100, dept="Ops", loc="Sharjah")
		self.s.dn("DN-D", "2026-09-04", 100, dept="Ops", loc="Dubai")
		ops = self.s.si("SI-OPS", [("SOI-1", 300)], dept="Ops", loc="Dubai")
		fin = self.s.si("SI-FIN", [("SOI-1", 100)], dept="Fin", loc="Dubai")
		self.m.allocate_sales_invoice(ops)
		self.m.allocate_sales_invoice(fin)
		self.assertEqual(
			self.s.links(),
			{"DN-A": ("SI-OPS", 100), "DN-B": ("SI-FIN", 100), "DN-D": ("SI-OPS", 100)},
		)

	def test_only_submitted_non_return_dns_are_taken(self):
		self.s.dn("DN-DRAFT", "2026-09-01", 100, docstatus=0)
		self.s.dn("DN-CANC", "2026-09-01", 100, docstatus=2)
		self.s.dn("DN-RET", "2026-09-01", -100, is_return=1)
		self.s.dn("DN-OK", "2026-09-02", 100)
		self.m.allocate_sales_invoice(self.s.si("SI-1", [("SOI-1", 300)]))
		self.assertEqual(self.s.links(), {"DN-OK": ("SI-1", 100)})

	def test_dn_cancel_moves_the_frontier(self):
		self.seed_three()
		self.s.dn("DN-D", "2026-09-04", 100)
		self.m.allocate_sales_invoice(self.s.si("SI-1", [("SOI-1", 250)], docstatus=1))
		self.assertEqual(self.s.links()["DN-C"], ("SI-1", 50))

		self.s.set("Delivery Note", "DN-A", docstatus=2)  # frappe writes docstatus before on_cancel
		dn = self.s.doc("DN-A", "Delivery Note")
		self.mark()
		self.m.on_delivery_note_cancel(dn)
		self.assertEqual(self.s.links(), {"DN-B": ("SI-1", 100), "DN-C": ("SI-1", 100), "DN-D": ("SI-1", 50)})
		# DN-A cleared by the cancel path, then one stamp for DN-C and DN-D; DN-B is never written.
		self.assertEqual([names for _q, names in self.dn_updates()], [["DN-A"], ["DN-C", "DN-D"]])

	def test_float_band(self):
		self.assertTrue(self.m._same(100.0, 100.0))
		self.assertFalse(self.m._same(None, 0.0))
		self.assertFalse(self.m._same(100.0, 100.000000001))
		self.assertFalse(self.m._same(2.0**23, 2.0**23))  # at or above the band: always write


class TestLockOrder(DnInvoiceLinkCase):
	def test_allocate_locks_its_so_lines_first(self):
		self.s.dn("DN-A", "2026-09-01", 100, line="SOI-2")
		si = self.s.si("SI-1", [("SOI-2", 50), (None, 10), ("SOI-1", 50), ("SOI-2", 10)])
		self.mark()
		self.m.allocate_sales_invoice(si)
		statements = self.statements()
		self.assertEqual(statements[0], LOCK_SQL)
		self.assertEqual(sum(1 for q in statements if "for update" in q), 1)

	def test_lock_names_are_sorted_and_distinct(self):
		captured = []
		real_sql = self.db.sql

		def spy(query, values=None, **kwargs):
			if "for update" in query:
				captured.append(values)
			return real_sql(query, values, **kwargs)

		self.db.sql = spy
		self.m.lock_so_lines(self.s.si("SI-1", [("SOI-2", 1), ("SOI-1", 1), ("SOI-2", 1), (None, 1)]))
		self.assertEqual(captured, [{"names": ("SOI-1", "SOI-2")}])

	def test_no_lock_without_so_lines(self):
		si = self.s.si("SI-1", [(None, 10)])
		self.mark()
		self.m.lock_so_lines(si)
		self.assertEqual(self.statements(), [])

	def test_clear_locks_first(self):
		self.s.dn("DN-A", "2026-09-01", 100)
		self.m.allocate_sales_invoice(self.s.si("SI-1", [("SOI-1", 100)], docstatus=1))
		si = self.s.doc("SI-1")
		self.mark()
		self.m.clear_sales_invoice(si)
		self.assertEqual(self.statements()[0], LOCK_SQL)
		self.assertEqual(self.s.links(), {})

	def test_dn_event_locks_before_writing_links(self):
		# Inside a DN event, every invoice re-run takes the SO line (already held by the DN event,
		# so re-entrant) before it writes any DN row.
		self.s.dn("DN-A", "2026-09-01", 100)
		self.s.dn("DN-B", "2026-09-02", 100)
		self.m.allocate_sales_invoice(self.s.si("SI-1", [("SOI-1", 150)], docstatus=1))
		self.s.set("Delivery Note", "DN-A", docstatus=2)
		dn = self.s.doc("DN-A", "Delivery Note")
		self.mark()
		self.m.on_delivery_note_cancel(dn)
		statements = self.statements()
		lock_at = statements.index(LOCK_SQL)
		stamp_at = max(i for i, q in enumerate(statements) if "case name when" in q)
		self.assertLess(lock_at, stamp_at)

	def test_hooks_wire_the_lock_ahead_of_the_so_header_write(self):
		hooks_path = os.path.join(os.path.dirname(MODULE), "hooks.py")
		ns = {}
		with open(hooks_path) as fh:
			exec(fh.read(), ns)
		si_events = ns["doc_events"]["Sales Invoice"]
		self.assertEqual(
			si_events["after_insert"],
			[
				"fuelbuddy_crm.dn_invoice_link.lock_so_lines",
				"fuelbuddy_crm.auto_invoicing.update_so_last_invoiced",
			],
		)
		self.assertEqual(si_events["on_update"], "fuelbuddy_crm.dn_invoice_link.allocate_sales_invoice")
		self.assertEqual(si_events["on_cancel"], "fuelbuddy_crm.dn_invoice_link.clear_sales_invoice")


class TestConvergence(unittest.TestCase):
	"""Whatever links are stored (for instance a mix left by two overlapping re-allocations read
	from old snapshots), one run leaves exactly the allocation a clean run gives."""

	def test_any_stored_state_converges_in_one_run(self):
		for seed in range(150):
			rng = random.Random(seed)
			fresh, dirty = Site(), Site()
			for site in (fresh, dirty):
				_seed_dns(site, random.Random(seed))
				site.si("SI-1", [("SOI-1", 400), ("SOI-2", 150)], frm="2026-09-03", to="2026-09-25")
			names = [r[0] for r in dirty.db.rows("select name from `tabDelivery Note`")]
			for name in rng.sample(names, k=min(len(names), rng.randint(1, 6))):
				dirty.set(
					"Delivery Note",
					name,
					custom_sales_invoice="SI-1",
					custom_sales_invoice_qty=rng.choice([1, 50, 999]),
				)
			fresh.m.allocate_sales_invoice(fresh.doc("SI-1"))
			dirty.m.allocate_sales_invoice(dirty.doc("SI-1"))
			self.assertEqual(dirty.links(), fresh.links(), f"seed {seed}")


def _seed_dns(site, rng, n=None):
	for i in range(n or rng.randint(3, 20)):
		day = rng.randint(1, 30)
		litres = rng.choice([50, 100, 250.5, 1000, 3.3, 0.1 + 0.2, 77.77])
		if rng.random() < 0.08:  # a DN with two items (same line, or two lines)
			other = rng.choice(["SOI-1", "SOI-2"])
			litres = [("SOI-1", litres), (other, rng.choice([10, 20.5]))]
		site.dn(
			f"DN-{rng.randint(0, 9999):04d}-{i}",
			f"2026-09-{day:02d}",
			litres,
			line=rng.choice(["SOI-1", "SOI-1", "SOI-2"]),
			docstatus=rng.choice([1, 1, 1, 1, 1, 0, 2]),
			is_return=1 if rng.random() < 0.05 else 0,
			dept=rng.choice([None, None, "Ops", "Fin"]),
			loc=rng.choice([None, None, "Dubai"]),
			time=rng.choice(["08:00:00", "10:00:00", "10:00:00"]),
		)


class TestSameEndStateAsClearAndRestamp(unittest.TestCase):
	"""Random DNs, invoices and events, run on the new code and on the old clear-and-restamp code
	side by side: identical links, litres and shortfall messages after every event; the new code
	writes a DN row only when its link or litres change, and never more rows than the old code."""

	EVENTS = ("save", "resize", "submit", "cancel_si", "cancel_dn", "submit_dn", "save")

	def test_random_histories(self):
		for seed in range(250):
			self._history(seed)

	def _history(self, seed):
		new, old = Site(), Site(old=True)
		for site in (new, old):
			rng = random.Random(seed)
			_seed_dns(site, rng)
			for k in range(rng.randint(1, 4)):
				rows = [
					(rng.choice(["SOI-1", "SOI-2", None]), rng.choice([100, 250, 333.3, 1200]))
					for _ in range(rng.randint(1, 2))
				]
				frm = rng.choice([None, "2026-09-01", "2026-09-10"])
				to = rng.choice([None, "2026-09-20", "2026-09-30"])
				site.si(
					f"SI-{k}",
					rows,
					frm=frm,
					to=to,
					dept=rng.choice([None, None, "Ops"]),
					loc=rng.choice([None, None, "Dubai"]),
				)
		rng = random.Random(seed * 7919 + 1)
		sis = [r[0] for r in new.db.rows("select name from `tabSales Invoice` order by name")]
		for step in range(10):
			event = rng.choice(self.EVENTS)
			si = rng.choice(sis)
			dns = [r[0] for r in new.db.rows("select name from `tabDelivery Note` order by name")]
			dn = rng.choice(dns)
			litres = rng.choice([50, 400, 999.9])
			before_new = new.links()
			changes = []
			for site in (new, old):
				site.frappe.messages.clear()
				run = _prepare(site, event, si, dn, litres)
				start = site.db.conn.total_changes
				run()
				changes.append(site.db.conn.total_changes - start)
			label = f"seed {seed} step {step} {event} {si} {dn}"
			self.assertEqual(new.links(), old.links(), label)
			self.assertEqual(new.frappe.messages, old.frappe.messages, label)
			self.assertLessEqual(changes[0], changes[1], label)
			if event in ("save", "resize", "submit", "cancel_si"):
				after = new.links()
				moved = {k for k in set(before_new) | set(after) if before_new.get(k) != after.get(k)}
				self.assertEqual(changes[0], len(moved), label)


def _prepare(site, event, si, dn, litres):
	"""Apply the event's own document change (what frappe writes before the hook runs) and return
	the hook call, so the caller can count the rows the hook alone writes."""
	m = site.m
	if event == "resize":
		site.set("Sales Invoice Item", f"{si}-1", stock_qty=litres, qty=litres)
	elif event == "submit":
		site.set("Sales Invoice", si, docstatus=1)
	elif event == "cancel_si":
		site.set("Sales Invoice", si, docstatus=2)
		doc = site.doc(si)
		return lambda: m.clear_sales_invoice(doc)
	elif event == "cancel_dn":
		site.set("Delivery Note", dn, docstatus=2)
		doc = site.doc(dn, "Delivery Note")
		return lambda: m.on_delivery_note_cancel(doc)
	elif event == "submit_dn":
		site.set("Delivery Note", dn, docstatus=1)
		doc = site.doc(dn, "Delivery Note")
		return lambda: m.reallocate_for_delivery_note(doc)
	doc = site.doc(si)
	return lambda: m.allocate_sales_invoice(doc)


if __name__ == "__main__":
	unittest.main()
