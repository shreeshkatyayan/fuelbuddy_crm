# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""billing_repair without a site (IDEV-3268): what it flags, what it writes, and what it never writes.

Runs as plain Python (``python3 -m unittest discover -s fuelbuddy_crm/tests -p "test_*_pure.py"``)
and under bench run-tests, on the SQLite-backed fake frappe. ERPNext is not imported: the DN
status rules are ERPNext v15.96.0's status_map["Delivery Note"] copied below, and "stock's refresh"
is an independent Python rendering of update_billing_percentage + set_status (``stock_refresh``).
SQLite computes in floats where MariaDB computes in DECIMAL, so exact equality with MariaDB is the
site test's job (test_billing_repair_site.py); here the values are chosen to be exact in both.
"""

import os
import unittest

try:
	from . import fake_frappe as ff
except ImportError:  # run as a top-level module by unittest discover
	import fake_frappe as ff

MODULE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "billing_repair.py")

# erpnext v15.96.0 erpnext/controllers/status_updater.py status_map["Delivery Note"]
DN_RULES = [
	["Draft", None],
	["To Bill", "eval:self.per_billed == 0 and self.docstatus == 1"],
	["Partially Billed", "eval:self.per_billed < 100 and self.per_billed > 0 and self.docstatus == 1"],
	["Completed", "eval:self.per_billed == 100 and self.docstatus == 1"],
	["Return Issued", "eval:self.per_returned == 100 and self.docstatus == 1"],
	["Return", "eval:self.is_return == 1 and self.per_billed == 0 and self.docstatus == 1"],
	["Cancelled", "eval:self.docstatus==2"],
	["Closed", "eval:self.status=='Closed' and self.docstatus != 2"],
]


def rule_status(row):
	"""set_status's pick, written out by hand from DN_RULES (reversed, first match)."""
	if row["status"] == "Closed" and row["docstatus"] != 2:
		return "Closed"
	if row["docstatus"] == 2:
		return "Cancelled"
	if row["is_return"] == 1 and row["per_billed"] == 0 and row["docstatus"] == 1:
		return "Return"
	if row["per_returned"] == 100 and row["docstatus"] == 1:
		return "Return Issued"
	if row["per_billed"] == 100 and row["docstatus"] == 1:
		return "Completed"
	if 0 < row["per_billed"] < 100 and row["docstatus"] == 1:
		return "Partially Billed"
	if row["per_billed"] == 0 and row["docstatus"] == 1:
		return "To Bill"
	return "Draft"


def stock_refresh(db, name):
	"""update_billing_percentage(update_modified=True) + set_status(update=True), in plain Python."""
	items = db.rows(
		"select amount, returned_qty, rate, billed_amt from `tabDelivery Note Item` where parent = ? order by idx",
		name,
	)
	total_amount = total_returned = 0
	for amount, returned_qty, rate, _billed in items:
		total_amount += amount
		total_returned += returned_qty * rate
	refs = [(a - rq * r) if total_returned < total_amount else a for a, rq, r, _b in items]
	den = sum(abs(x) for x in refs)
	per_billed = (
		round(
			sum(min(abs(x), abs(b)) for x, (_a, _rq, _r, b) in zip(refs, items, strict=True)) / den * 100, 6
		)
		if den > 0
		else 0
	)
	(docstatus, is_return, per_returned, status) = db.rows(
		"select docstatus, is_return, per_returned, status from `tabDelivery Note` where name = ?", name
	)[0]
	new_status = rule_status(
		{
			"per_billed": per_billed,
			"docstatus": docstatus,
			"is_return": is_return,
			"per_returned": per_returned,
			"status": status,
		}
	)
	db.conn.execute(
		"update `tabDelivery Note` set per_billed = ?, status = ? where name = ?",
		(per_billed, new_status, name),
	)


class FakeCore:
	"""The core branch's interface, as billing_repair assumes it."""

	def __init__(self, db, answers, parents):
		self.db = db
		self.answers = answers  # so_detail -> list of rows | [] | None
		self.parents = parents  # so_detail -> DNs recompute_line returns
		self.recomputed = []

	def stock_would_change(self, so_detail):
		return self.answers.get(so_detail, [])

	def recompute_line(self, so_detail, update_modified=True):
		self.recomputed.append((so_detail, update_modified, self.db.log[-1]))
		for dn_item, _dn, value in self.answers[so_detail]:
			self.db.conn.execute(
				"update `tabDelivery Note Item` set billed_amt = ? where name = ?", (value, dn_item)
			)
		return self.parents.get(so_detail, [])


class RepairCase(unittest.TestCase):
	def setUp(self):
		self.frappe = ff.make()
		self.db = self.frappe.db
		self.m = ff.load(MODULE, self.frappe)
		self.m._dn_status_rules = lambda: DN_RULES
		self.m._core = lambda: None
		self.m._link_audit = lambda sample: {"linked_not_submitted": {"count": 0, "sample": []}}
		self.refreshed = []
		self.fail_on = set()

		def attach(doc):
			def update_billing_percentage(update_modified=True):
				self.assertTrue(update_modified)
				self.refreshed.append(doc.name)
				if doc.name in self.fail_on:
					raise RuntimeError(f"refresh of {doc.name} failed")
				stock_refresh(self.db, doc.name)

			doc.update_billing_percentage = update_billing_percentage

		self.frappe.doc_hooks["Delivery Note"] = attach

	def dn(self, name, amount=100.0, billed=0.0, per_billed=0.0, status="To Bill", **kw):
		"""A submitted DN with one item on SOI-1; stored per_billed / status as given."""
		items = kw.pop("items", None) or [(amount, billed, 0.0, 1.0)]
		self.db.insert(
			"Delivery Note",
			name=name,
			docstatus=kw.pop("docstatus", 1),
			is_return=kw.pop("is_return", 0),
			per_returned=kw.pop("per_returned", 0),
			posting_date=kw.pop("date", "2026-09-01"),
			per_billed=per_billed,
			status=status,
		)
		for idx, (amt, bill, returned_qty, rate) in enumerate(items, 1):
			self.db.insert(
				"Delivery Note Item",
				name=f"{name}-{idx}",
				parent=name,
				idx=idx,
				so_detail=kw.get("line", "SOI-1"),
				amount=amt,
				billed_amt=bill,
				returned_qty=returned_qty,
				rate=rate,
			)

	def stored(self, name):
		return tuple(
			self.db.rows("select per_billed, status from `tabDelivery Note` where name = ?", name)[0]
		)


class TestStockValues(RepairCase):
	def test_status_rules_are_evaluated_like_set_status(self):
		base = {"docstatus": 1, "is_return": 0, "per_returned": 0, "status": "To Bill"}
		for per_billed, expected in ((0, "To Bill"), (40, "Partially Billed"), (100, "Completed")):
			row = self.frappe._dict(base, per_billed=per_billed)
			self.assertEqual(self.m.stock_status(row, DN_RULES), expected)
		self.assertEqual(
			self.m.stock_status(self.frappe._dict(base, per_billed=0, is_return=1), DN_RULES), "Return"
		)
		self.assertEqual(
			self.m.stock_status(self.frappe._dict(base, per_billed=0, per_returned=100), DN_RULES),
			"Return Issued",
		)
		self.assertEqual(
			self.m.stock_status(self.frappe._dict(base, per_billed=50, status="Closed"), DN_RULES), "Closed"
		)

	def test_a_method_rule_makes_status_unknown(self):
		rules = [*DN_RULES, ["Odd", "is_odd"]]
		self.assertIsNone(self.m.stock_status(self.frappe._dict(per_billed=0, docstatus=1), rules))

	def test_ref_follows_stock_float_sums(self):
		# 100 AED delivered, 20 returned (returned value < amount): ref = amount - returned, so
		# 80 billed is 100 %.
		self.dn("DN-NET", items=[(100.0, 80.0, 20.0, 1.0)], per_billed=100.0, status="Completed")
		# fully returned (returned value == amount): stock falls back to ref = amount: 80 of 100.
		self.dn("DN-FULL", items=[(100.0, 80.0, 100.0, 1.0)], per_billed=80.0, status="Partially Billed")
		self.assertEqual(self.m.dn_drift(["DN-NET", "DN-FULL"]), [])

	def test_flags_only_real_differences(self):
		self.dn("DN-OK0")  # 0 billed, To Bill: right
		self.dn("DN-OK100", billed=100.0, per_billed=100.0, status="Completed")
		self.dn("DN-STALE", billed=100.0, per_billed=0.0, status="To Bill")  # billed, never refreshed
		self.dn("DN-STATUS", billed=50.0, per_billed=50.0, status="To Bill")  # per_billed right
		self.dn("DN-CLOSED", billed=50.0, per_billed=50.0, status="Closed")
		self.dn("DN-CANC", billed=100.0, per_billed=0.0, status="Cancelled", docstatus=2)  # out of scope
		drift = {
			d.name: d
			for d in self.m.dn_drift(["DN-OK0", "DN-OK100", "DN-STALE", "DN-STATUS", "DN-CLOSED", "DN-CANC"])
		}
		self.assertEqual(sorted(drift), ["DN-STALE", "DN-STATUS"])
		self.assertEqual(drift["DN-STALE"].per_billed, (0.0, 100.0))
		self.assertEqual(drift["DN-STALE"].status, ("To Bill", "Completed"))
		self.assertEqual(drift["DN-STATUS"].per_billed, (50.0, 50.0))
		self.assertEqual(drift["DN-STATUS"].status, ("To Bill", "Partially Billed"))


class TestRepair(RepairCase):
	def seed(self):
		self.dn("DN-1")
		self.dn("DN-2", billed=100.0)  # stale: 100 %
		self.dn("DN-3", billed=40.0)  # stale: 40 %
		self.dn("DN-4", billed=100.0, per_billed=100.0, status="Completed")
		self.dn("DN-5", billed=100.0, per_billed=100.0, status="To Bill")  # status only
		self.dn("DN-6", billed=10.0)
		self.dn("DN-7", billed=70.0, date="2026-08-15")  # before from_date in the scoped test
		self.db.conn.commit()

	def test_dry_run_writes_nothing(self):
		self.seed()
		report = self.m.repair(page_size=3)
		self.assertTrue(report.dry_run)
		self.assertEqual(report.dns["checked"], 7)
		self.assertEqual(report.dns["drifted"], 5)
		self.assertEqual((report.dns["per_billed"], report.dns["status_only"]), (4, 1))
		self.assertEqual(self.db.writes(), [])
		self.assertEqual(self.refreshed, [])
		self.assertEqual(self.db.commits, 0)
		self.assertGreaterEqual(self.db.rollbacks, 3)  # one per page at least
		self.assertEqual(self.stored("DN-2"), (0.0, "To Bill"))
		self.assertIn("skipped", report.lines)

	def test_repair_refreshes_only_drifted_dns_and_commits_per_chunk(self):
		self.seed()
		report = self.m.repair(dry_run=0, chunk_size=2, page_size=100)
		self.assertEqual(sorted(self.refreshed), ["DN-2", "DN-3", "DN-5", "DN-6", "DN-7"])
		self.assertEqual(self.db.commits, 3)  # 5 DNs in chunks of 2
		self.assertEqual((report.dns["fixed"], report.dns["still_different"]), (5, 0))
		self.assertEqual(self.stored("DN-2"), (100.0, "Completed"))
		self.assertEqual(self.stored("DN-5"), (100.0, "Completed"))
		self.assertEqual(self.m.dn_drift([f"DN-{i}" for i in range(1, 8)]), [])

	def test_scope_by_posting_date(self):
		self.seed()
		self.m.repair(dry_run=0, from_date="2026-09-01")
		self.assertNotIn("DN-7", self.refreshed)

	def test_a_failing_dn_is_isolated(self):
		self.seed()
		self.fail_on = {"DN-3"}
		report = self.m.repair(dry_run=0, chunk_size=10)
		self.assertEqual(report.failed, 1)
		self.assertEqual(report.failures[0]["delivery_note"], "DN-3")
		self.assertEqual(self.stored("DN-2"), (100.0, "Completed"))  # its chunk-mates still land
		self.assertEqual(self.stored("DN-3"), (0.0, "To Bill"))
		self.assertEqual(report.dns["still_different"], 1)

	def test_line_phase_is_skipped_without_the_core(self):
		self.seed()
		report = self.m.repair()
		self.assertIn("recompute_line", report.lines["skipped"])
		self.assertIn("billed_amt not checked", self.m.summary(report))

	def test_line_phase_with_the_core(self):
		self.dn("DN-A", billed=0.0)  # stock would bill it 100
		self.dn("DN-B", billed=0.0, line="SOI-2")
		self.dn("DN-C", billed=0.0, line="SOI-3")
		self.db.conn.commit()
		core = FakeCore(
			self.db,
			answers={"SOI-1": [("DN-A-1", "DN-A", 100.0)], "SOI-2": [], "SOI-3": None},
			parents={"SOI-1": ["DN-A"]},
		)
		self.m._core = lambda: core

		dry = self.m.repair()
		self.assertEqual((dry.lines["checked"], dry.lines["drifted"], dry.lines["unknown"]), (3, 1, 1))
		self.assertEqual(dry.lines["unknown_lines"], ["SOI-3"])
		self.assertEqual(core.recomputed, [])
		self.assertEqual(self.db.writes(), [])

		self.db.log.clear()
		report = self.m.repair(dry_run=0)
		self.assertEqual([(so, um) for so, um, _last in core.recomputed], [("SOI-1", True)])
		# the SO line lock is the statement right before recompute_line, after a rollback
		self.assertEqual(
			core.recomputed[0][2], "select name from `tabSales Order Item` where name = %s for update"
		)
		lock_at = self.db.log.index(core.recomputed[0][2])
		self.assertEqual(self.db.log[lock_at - 1], "rollback")
		self.assertEqual(self.refreshed, ["DN-A"])  # returned parent, refreshed once
		self.assertEqual(self.stored("DN-A"), (100.0, "Completed"))
		self.assertEqual(report.dns["drifted"], 0)


class TestAudit(RepairCase):
	def test_audit_is_read_only(self):
		self.dn("DN-2", billed=100.0)
		self.db.conn.commit()
		report = self.m.audit()
		self.assertEqual(report.dns["drifted"], 1)
		self.assertEqual(self.db.writes(), [])
		self.assertEqual(self.db.commits, 0)
		self.assertEqual(self.refreshed, [])
		self.assertIn("links", report)

	def test_nightly_logs_one_deferred_error_only_on_drift(self):
		self.dn("DN-1")
		self.db.conn.commit()
		self.m.nightly_drift_audit()
		self.assertEqual(self.frappe.errors, [])
		self.assertTrue(any("billing drift audit" in msg for _lvl, msg in self.frappe.logged))

		self.dn("DN-2", billed=100.0)
		self.db.conn.commit()
		self.m.nightly_drift_audit()
		self.assertEqual(len(self.frappe.errors), 1)
		error = self.frappe.errors[0]
		self.assertTrue(error.title.startswith("Billing drift: 1 of 2 DNs differ"))
		self.assertLessEqual(len(error.title), 140)
		self.assertTrue(error.defer_insert)
		self.assertIn("DN-2", error.message)

	def test_link_drift_alone_raises_the_alert(self):
		self.dn("DN-1")
		self.db.conn.commit()
		self.m._link_audit = lambda sample: {"over_linked": {"count": 2, "sample": ["SI-1", "SI-2"]}}
		self.m.nightly_drift_audit()
		self.assertEqual(len(self.frappe.errors), 1)
		self.assertIn("2 over_linked", self.frappe.errors[0].title)

	def test_hooks_schedule_the_nightly_audit(self):
		ns = {}
		with open(os.path.join(os.path.dirname(MODULE), "hooks.py")) as fh:
			exec(fh.read(), ns)
		self.assertIn(
			"fuelbuddy_crm.billing_repair.nightly_drift_audit", ns["scheduler_events"]["daily_long"]
		)


if __name__ == "__main__":
	unittest.main()
