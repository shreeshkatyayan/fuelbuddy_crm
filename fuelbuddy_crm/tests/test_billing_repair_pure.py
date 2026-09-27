# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""billing_repair without a site (IDEV-3268): what it flags, what it writes, and what it never writes.

Runs as plain Python (``python3 -m unittest discover -s fuelbuddy_crm/tests -p "test_*_pure.py"``)
and under bench run-tests, on the SQLite-backed fake frappe. ERPNext is not imported: the DN
status rules are ERPNext v15.96.0's status_map["Delivery Note"] copied below, and "stock's refresh"
is an independent Python rendering of update_billing_percentage + set_status (``stock_values``).

- TestStockValues runs the billing re-check's own stock_refresh module (what api.header_drift and
  the invoice-side bulk refresh compute) on SQLite against that rendering.
- The repair / audit tests run billing_repair against ``FakeCore``, the billing_recheck.api
  interface with per_billed / status from the same rendering.

SQLite computes in floats where MariaDB computes in DECIMAL, so exact equality with MariaDB is the
site test's job (test_billing_repair_site.py); here the values are chosen to be exact in both.
"""

import os
import unittest

try:
	from . import fake_frappe as ff
except ImportError:  # run as a top-level module by unittest discover
	import fake_frappe as ff

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE = os.path.join(APP, "billing_repair.py")
STOCK_REFRESH = os.path.join(APP, "billing_recheck", "stock_refresh.py")

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


def stock_values(db, name):
	"""(per_billed, status) update_billing_percentage + set_status would store, in plain Python."""
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
	return per_billed, rule_status(
		{
			"per_billed": per_billed,
			"docstatus": docstatus,
			"is_return": is_return,
			"per_returned": per_returned,
			"status": status,
		}
	)


def stock_refresh(db, name):
	"""update_billing_percentage(update_modified=True) + set_status(update=True), in plain Python."""
	per_billed, status = stock_values(db, name)
	db.conn.execute(
		"update `tabDelivery Note` set per_billed = ?, status = ? where name = ?", (per_billed, status, name)
	)


class FakeCore:
	"""fuelbuddy_crm.billing_recheck.api as billing_repair uses it."""

	class GuardError(Exception):
		pass

	def __init__(self, db, answers=None, parents=None):
		self.db = db
		self.answers = answers or {}  # so_detail -> [(dn_item, dn, stock value)] | None (stock's walk)
		self.parents = parents or {}  # so_detail -> DNs recompute_line returns
		self.recomputed = []
		self.header_args = []
		self.guard = None  # a mismatch text: every read raises GuardError, as api's do
		self.header_error = None  # an exception header_drift raises (status_fields' ValueError)

	def _require_guard(self):
		if self.guard:
			raise self.GuardError(self.guard)

	def stock_would_change(self, so_detail, header=True):
		self._require_guard()
		self.header_args.append(header)
		answer = self.answers.get(so_detail, [])
		out = {"so_detail": so_detail, "predicted": answer is not None, "reason": None, "rows": None}
		if answer is None:
			out["reason"] = "multi_item"
		else:
			out["rows"] = [{"name": i, "parent": dn, "stored": 0.0, "stock": v} for i, dn, v in answer]
		out["header"] = self.header_drift([]) if header else None
		return out

	def recompute_line(self, so_detail, update_modified=True):
		self.recomputed.append((so_detail, update_modified, self.db.log[-1]))
		for dn_item, _dn, value in self.answers[so_detail]:
			self.db.conn.execute(
				"update `tabDelivery Note Item` set billed_amt = ? where name = ?", (value, dn_item)
			)
		return self.parents.get(so_detail, [])

	def header_drift(self, names):
		self._require_guard()
		if self.header_error:
			raise self.header_error
		out = []
		for name in names:
			stored = self.db.rows("select per_billed, status from `tabDelivery Note` where name = ?", name)
			if not stored:
				continue
			stock = stock_values(self.db, name)
			if tuple(stored[0]) != stock:
				out.append(
					{"name": name, "per_billed": (stored[0][0], stock[0]), "status": (stored[0][1], stock[1])}
				)
		return out


class RepairCase(unittest.TestCase):
	def setUp(self):
		self.frappe = ff.make()
		self.db = self.frappe.db
		self.m = ff.load(MODULE, self.frappe)
		self.core = FakeCore(self.db)
		self.m._core = lambda: self.core
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
	"""billing_recheck.stock_refresh (api.header_drift's and bulk.py's arithmetic) on SQLite."""

	def setUp(self):
		super().setUp()
		self.sr = ff.load(STOCK_REFRESH, self.frappe)
		self.sr.status_map = lambda: DN_RULES

	def predicted(self, names):
		per_billed = self.sr.stock_per_billed(names)
		return {
			name: (per_billed[name], status)
			for name, (_stored, status) in self.sr.statuses(names, per_billed=per_billed).items()
		}

	def test_status_rules_are_evaluated_like_set_status(self):
		self.dn("DN-0", per_billed=0.0)
		self.dn("DN-40", per_billed=40.0)
		self.dn("DN-100", per_billed=100.0)
		self.dn("DN-RET", is_return=1, per_billed=0.0)
		self.dn("DN-RI", per_returned=100, per_billed=0.0)
		self.dn("DN-CL", per_billed=50.0, status="Closed")
		got = {
			name: status
			for name, (_stored, status) in self.sr.statuses(
				["DN-0", "DN-40", "DN-100", "DN-RET", "DN-RI", "DN-CL"]
			).items()
		}
		self.assertEqual(
			got,
			{
				"DN-0": "To Bill",
				"DN-40": "Partially Billed",
				"DN-100": "Completed",
				"DN-RET": "Return",
				"DN-RI": "Return Issued",
				"DN-CL": "Closed",
			},
		)

	def test_a_method_rule_is_refused(self):
		self.dn("DN-1")
		self.sr.status_map = lambda: [*DN_RULES, ["Odd", "is_odd"]]
		with self.assertRaises(ValueError):
			self.sr.statuses(["DN-1"])

	def test_ref_follows_stock_float_sums(self):
		# 100 AED delivered, 20 returned (returned value < amount): ref = amount - returned, so
		# 80 billed is 100 %.
		self.dn("DN-NET", items=[(100.0, 80.0, 20.0, 1.0)])
		# fully returned (returned value == amount): stock falls back to ref = amount: 80 of 100.
		self.dn("DN-FULL", items=[(100.0, 80.0, 100.0, 1.0)])
		self.assertEqual(
			self.predicted(["DN-NET", "DN-FULL"]),
			{"DN-NET": (100.0, "Completed"), "DN-FULL": (80.0, "Partially Billed")},
		)

	def test_matches_the_plain_python_rendering(self):
		shapes = {
			"DN-OK0": {},
			"DN-STALE": {"billed": 100.0},
			"DN-PART": {"billed": 25.0},
			"DN-OVER": {"billed": 150.0},
			"DN-ZERO": {"amount": 0.0},
			"DN-TWO": {"items": [(60.0, 60.0, 0.0, 1.0), (40.0, 10.0, 0.0, 1.0)]},
			"DN-RETPART": {"items": [(200.0, 50.0, 50.0, 1.0)]},
			"DN-NEG": {"items": [(-50.0, -50.0, 0.0, 1.0)], "is_return": 1},
			"DN-CLOSED": {"billed": 50.0, "status": "Closed"},
		}
		for name, kw in shapes.items():
			self.dn(name, **kw)
		self.assertEqual(self.predicted(list(shapes)), {n: stock_values(self.db, n) for n in shapes})


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
		self.assertEqual((report.lines["checked"], report.lines["drifted"]), (1, 0))
		self.assertEqual(self.core.header_args, [False])  # the DN pass checks headers, not the line pass
		self.assertEqual(self.db.writes(), [])
		self.assertEqual(self.refreshed, [])
		self.assertEqual(self.db.commits, 0)
		self.assertGreaterEqual(self.db.rollbacks, 3)  # one per page at least
		self.assertEqual(self.stored("DN-2"), (0.0, "To Bill"))

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

	def test_upgrade_guard_stops_both_checks_and_writes_nothing(self):
		self.seed()
		self.core.guard = "erpnext 15.99.0 not pinned"
		report = self.m.repair(dry_run=0)
		self.assertTrue(report.lines["skipped"].startswith("upgrade guard: erpnext 15.99.0"))
		self.assertIn("GuardError", report.dns["skipped"])
		self.assertEqual((report.lines["checked"], report.dns["checked"]), (0, 0))
		self.assertEqual(self.db.writes(), [])
		self.assertEqual(self.refreshed, [])
		self.assertEqual(self.core.recomputed, [])
		self.assertEqual(self.m.not_checked(report), ["lines", "dns"])
		self.assertIn("billed_amt not checked (upgrade guard", self.m.summary(report))
		self.assertIn("per_billed / status not checked", self.m.summary(report))

	def test_unevaluable_status_rule_stops_the_dn_check_only(self):
		self.seed()
		self.core.header_error = ValueError("method condition 'is_odd'")
		report = self.m.repair()
		self.assertIn("ValueError", report.dns["skipped"])
		self.assertEqual(report.lines["checked"], 1)
		self.assertEqual(self.m.not_checked(report), ["dns"])

	def test_lines_off_is_not_a_failure(self):
		self.seed()
		report = self.m.repair(lines=0)
		self.assertEqual(report.lines, {"skipped": "lines=0"})
		self.assertEqual(self.m.not_checked(report), [])

	def test_line_phase_with_the_core(self):
		self.dn("DN-A", billed=0.0)  # stock would bill it 100
		self.dn("DN-B", billed=0.0, line="SOI-2")
		self.dn("DN-C", billed=0.0, line="SOI-3")
		self.db.conn.commit()
		self.core.answers = {"SOI-1": [("DN-A-1", "DN-A", 100.0)], "SOI-2": [], "SOI-3": None}
		self.core.parents = {"SOI-1": ["DN-A"]}

		dry = self.m.repair()
		self.assertEqual((dry.lines["checked"], dry.lines["drifted"], dry.lines["unknown"]), (3, 1, 1))
		self.assertEqual(dry.lines["unknown_lines"], [{"so_detail": "SOI-3", "reason": "multi_item"}])
		self.assertEqual(dry.lines["samples"][0]["first"][0]["stock"], 100.0)
		self.assertEqual(self.core.recomputed, [])
		self.assertEqual(self.db.writes(), [])

		self.db.log.clear()
		report = self.m.repair(dry_run=0)
		self.assertEqual([(so, um) for so, um, _last in self.core.recomputed], [("SOI-1", True)])
		# the SO line lock is the statement right before recompute_line, after a rollback
		self.assertEqual(
			self.core.recomputed[0][2], "select name from `tabSales Order Item` where name = %s for update"
		)
		lock_at = self.db.log.index(self.core.recomputed[0][2])
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

	def test_nightly_alerts_when_it_cannot_check(self):
		self.dn("DN-1")
		self.db.conn.commit()
		self.core.guard = "frappe 15.121.0 not pinned"
		self.m.nightly_drift_audit()
		self.assertEqual(len(self.frappe.errors), 1)
		self.assertIn("not checked", self.frappe.errors[0].title)
		self.assertTrue(self.frappe.errors[0].defer_insert)

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
