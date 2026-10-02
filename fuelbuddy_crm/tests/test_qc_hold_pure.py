# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""The invoice hold (IDEV-3266) without a site: api/qc_hold, invoice_hold and the auto-invoicing
deferral.

Runs as plain Python from the app directory and under ``bench run-tests``:

    python3 -m unittest fuelbuddy_crm.tests.test_qc_hold_pure

Each module under test is loaded with a SQLite-backed fake frappe (qc_hold_fake_frappe.py), never
the real one, so this checks decisions, rows and SQL, not MariaDB locking.
"""

import datetime
import json
import os
import random
import unittest

try:
	from . import qc_hold_fake_frappe as ff
except ImportError:  # run as a top-level module by unittest discover
	import qc_hold_fake_frappe as ff

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.path.join(APP, "api", "qc_hold.py")
INVOICE_HOLD = os.path.join(APP, "invoice_hold.py")
AUTO_INVOICING = os.path.join(APP, "auto_invoicing.py")
DN_INVOICE_LINK = os.path.join(APP, "dn_invoice_link.py")
HOOKS = os.path.join(APP, "hooks.py")
DOCTYPE_JSON = os.path.join(APP, "fuelbuddy_crm", "doctype", "qc_hold", "qc_hold.json")

IID = "0b7c7d1e-5a0e-4a3f-9d7e-1f2a3b4c5d6e"
TICKET = "7f1c2d3e-0000-4000-8000-000000000001"
EP1 = f"{TICKET}-1759300000000"  # raised first
EP2 = f"{TICKET}-1759400000000"  # re-raised later
CUTOFF = "2026-11-01T05:00:00.000Z"  # 09:00 Dubai on the 1st: the ticket's apply_cutoff_at
CUTOFF_DB = "2026-11-01 05:00:00.000000"


def _dt(text):
	return datetime.datetime.fromisoformat(text)


class Site:
	"""One fake site: the fake frappe, the modules loaded against it, and fixture helpers."""

	def __init__(self):
		self.frappe = ff.make(now="2026-10-15 08:00:00", today="2026-10-15")
		self.db = self.frappe.db
		self.api = ff.load(API, self.frappe)
		self.hold_mod = ff.load(INVOICE_HOLD, self.frappe)

	# -- fixtures -------------------------------------------------------------------------------
	def hold(self, iid, episode, status="Open", expires=CUTOFF_DB, opened="2026-10-14 08:00:00.000000"):
		self.db.insert(
			"QC Hold",
			name=iid,
			invoiced_item_id=iid,
			episode_key=episode,
			status=status,
			opened_at=opened,
			expires_at=expires,
		)

	def dn(self, name, date, lines=(("SOI-1", 1000),), iid=None, docstatus=1, is_return=0, so="SO-1"):
		self.db.insert(
			"Delivery Note",
			name=name,
			docstatus=docstatus,
			is_return=is_return,
			posting_date=date,
			custom_invoiced_item_id=iid,
		)
		for idx, (so_detail, qty) in enumerate(lines, 1):
			self.db.insert(
				"Delivery Note Item",
				name=f"{name}-{idx}",
				parent=name,
				idx=idx,
				so_detail=so_detail,
				against_sales_order=so,
				qty=qty,
			)

	def row(self, iid=IID):
		return self.db.row("QC Hold", iid)

	def open(self, episode=EP1, iid=IID, expires=CUTOFF):
		return self.api.open_hold(invoiced_item_id=iid, episode_key=episode, expires_at=expires)

	def close(self, episode=EP1, reason="APPLIED", iid=IID):
		return self.api.close_hold(invoiced_item_id=iid, episode_key=episode, reason=reason)


def invoice(lines, frm=None, to=None, is_return=0, docstatus=0, before=None):
	doc = ff.Doc(
		doctype="Sales Invoice",
		name="SINV-1",
		docstatus=docstatus,
		is_return=is_return,
		custom_dn_from_date=frm,
		custom_dn_to_date=to,
		items=[ff._dict(so_detail=line, sales_order="SO-1") for line in lines],
	)
	doc.__dict__["_before"] = before
	return doc


# ==================================================================================================
# api/qc_hold: open_hold / close_hold
# ==================================================================================================
class TestOpenHold(unittest.TestCase):
	def setUp(self):
		self.site = Site()

	def assertOk(self, answer, result):
		self.assertEqual(
			(answer["ok"], answer["code"], answer["retryable"], answer["result"]),
			(True, None, False, result),
			answer,
		)

	def test_first_open_inserts_an_open_row_and_commits(self):
		answer = self.site.open()

		self.assertOk(answer, "OPENED")
		row = self.site.row()
		self.assertEqual(
			(row.invoiced_item_id, row.episode_key, row.status, row.opened_at, row.expires_at, row.closed_at),
			(IID, EP1, "Open", "2026-10-15 08:00:00.000000", CUTOFF_DB, None),
		)
		self.assertEqual(self.site.frappe.inserted, [("QC Hold", IID, True)])
		self.assertEqual((self.site.db.commits, self.site.db.rollbacks), (1, 0))

	def test_the_row_is_read_with_a_lock_before_anything_is_written(self):
		self.site.open()
		reads = [q for q in self.site.db.log if "tabQC Hold" in q]
		self.assertTrue(reads[0].startswith("select name, episode_key, status from `tabQC Hold`"), reads)
		self.assertTrue(reads[0].endswith("for update"), reads)

	def test_a_retry_for_the_same_episode_changes_nothing(self):
		self.site.open()
		self.site.frappe.now = _dt("2026-10-15 09:30:00")

		answer = self.site.open()

		self.assertOk(answer, "ALREADY_OPEN")
		self.assertEqual(self.site.row().opened_at, "2026-10-15 08:00:00.000000")

	def test_a_newer_episode_takes_the_row_over(self):
		self.site.hold(IID, EP1, status="Closed")
		self.site.db.set_value(
			"QC Hold", IID, {"closed_at": "2026-10-14 10:00:00.000000", "close_reason": "REJECTED"}
		)

		answer = self.site.open(EP2, expires="2026-11-01T05:00:00+00:00")

		self.assertOk(answer, "OPENED")
		self.assertIn(EP1, answer["message"])
		row = self.site.row()
		self.assertEqual(
			(row.episode_key, row.status, row.opened_at, row.closed_at, row.close_reason),
			(EP2, "Open", "2026-10-15 08:00:00.000000", None, None),
		)

	def test_a_newer_episode_takes_over_a_row_an_older_one_left_open(self):
		self.site.hold(IID, EP1)
		self.assertOk(self.site.open(EP2), "OPENED")
		self.assertEqual((self.site.row().episode_key, self.site.row().status), (EP2, "Open"))

	def test_an_older_episode_never_takes_the_row_back(self):
		for status in ("Open", "Closed"):
			with self.subTest(status=status):
				site = Site()
				site.hold(IID, EP2, status=status)

				answer = site.open(EP1)

				self.assertOk(answer, "SUPERSEDED")
				self.assertEqual((site.row().episode_key, site.row().status), (EP2, status))

	def test_an_ended_episode_never_reopens_its_hold(self):
		self.site.open()
		self.site.close()

		answer = self.site.open()

		self.assertOk(answer, "ALREADY_CLOSED")
		self.assertEqual(self.site.row().status, "Closed")

	def test_expires_at_is_stored_as_utc(self):
		cases = {
			"2026-11-01T05:00:00.000Z": CUTOFF_DB,
			"2026-11-01T09:00:00+04:00": CUTOFF_DB,
			"2026-11-01T09:00:00": CUTOFF_DB,  # no offset: the site's time zone (Asia/Dubai)
			"2026-10-31T21:00:00.123456-04:00": "2026-11-01 01:00:00.123456",
		}
		for given, stored in cases.items():
			with self.subTest(given):
				site = Site()
				self.assertOk(site.open(expires=given), "OPENED")
				self.assertEqual(site.row().expires_at, stored)


class TestCloseHold(unittest.TestCase):
	def setUp(self):
		self.site = Site()

	def assertOk(self, answer, result):
		self.assertEqual((answer["ok"], answer["code"], answer["result"]), (True, None, result), answer)

	def test_closing_the_open_episode_closes_it(self):
		self.site.open()
		self.site.frappe.now = _dt("2026-10-16 07:00:00")

		answer = self.site.close(reason="APPLIED")

		self.assertOk(answer, "CLOSED")
		row = self.site.row()
		self.assertEqual(
			(row.episode_key, row.status, row.closed_at, row.close_reason),
			(EP1, "Closed", "2026-10-16 07:00:00.000000", "APPLIED"),
		)
		self.assertEqual(self.site.db.commits, 2)

	def test_a_retried_close_changes_nothing(self):
		self.site.open()
		self.site.close(reason="APPLIED")
		self.site.frappe.now = _dt("2026-10-17 07:00:00")

		answer = self.site.close(reason="APPLIED")

		self.assertOk(answer, "NOTHING_TO_CLOSE")
		self.assertEqual(self.site.row().closed_at, "2026-10-15 08:00:00.000000")

	def test_an_old_episode_never_closes_a_newer_ones_hold(self):
		self.site.hold(IID, EP2)

		answer = self.site.close(EP1, reason="SUPERSEDED")

		self.assertOk(answer, "NOTHING_TO_CLOSE")
		row = self.site.row()
		self.assertEqual((row.episode_key, row.status, row.closed_at), (EP2, "Open", None))

	def test_a_newer_episode_closes_a_hold_an_older_one_left_open(self):
		self.site.hold(IID, EP1)

		answer = self.site.close(EP2, reason="REJECTED")

		self.assertOk(answer, "CLOSED")
		row = self.site.row()
		self.assertEqual((row.episode_key, row.status), (EP2, "Closed"))
		self.assertEqual(row.close_reason, f"REJECTED; ends earlier episode {EP1}")
		# ...and a late open for the newer episode does not hold again
		self.assertEqual(self.site.open(EP2)["result"], "ALREADY_CLOSED")

	def test_a_newer_episode_stamps_an_older_closed_row(self):
		self.site.hold(IID, EP1, status="Closed")

		answer = self.site.close(EP2, reason="TIMED_OUT")

		self.assertOk(answer, "NOTHING_TO_CLOSE")
		self.assertEqual((self.site.row().episode_key, self.site.row().status), (EP2, "Closed"))

	def test_a_close_with_no_hold_leaves_a_closed_row_so_a_late_open_does_not_hold(self):
		answer = self.site.close(reason="REJECTED")

		self.assertOk(answer, "NOTHING_TO_CLOSE")
		row = self.site.row()
		self.assertEqual((row.episode_key, row.status, row.close_reason), (EP1, "Closed", "REJECTED"))
		self.assertEqual(self.site.open()["result"], "ALREADY_CLOSED")
		self.assertEqual(self.site.row().status, "Closed")

	def test_a_long_reason_is_cut_to_the_field_length(self):
		self.site.hold(IID, EP1)
		reason = "R" * 120
		self.site.close(EP2, reason=reason)
		self.assertEqual(len(self.site.row().close_reason), 140)


class TestEpisodeOrder(unittest.TestCase):
	def test_the_raise_time_after_the_last_dash_orders_episodes(self):
		api = Site().api
		self.assertEqual(api.episode_order(EP1), 1759300000000)
		self.assertIsNone(api.episode_order("no-digits-here"))
		self.assertIsNone(api.episode_order(None))
		self.assertTrue(api._is_older(EP1, EP2))
		self.assertFalse(api._is_older(EP2, EP1))
		self.assertFalse(api._is_older(EP1, EP1))

	def test_keys_without_a_raise_time_take_over_on_open_but_never_close_another(self):
		site = Site()
		site.hold(IID, "episode-a")

		self.assertEqual(site.open("episode-b")["result"], "OPENED")
		self.assertEqual(site.row().episode_key, "episode-b")
		self.assertEqual(site.close("episode-a")["result"], "NOTHING_TO_CLOSE")
		self.assertEqual((site.row().episode_key, site.row().status), ("episode-b", "Open"))


class TestRefusals(unittest.TestCase):
	def setUp(self):
		self.site = Site()

	def assertRefused(self, answer, code, retryable=False):
		self.assertEqual(
			(answer["ok"], answer["code"], answer["retryable"], answer["result"]),
			(False, code, retryable, None),
			answer,
		)
		self.assertTrue(answer["message"])

	def test_bad_input_is_refused_before_any_read(self):
		cases = [
			dict(invoiced_item_id="", episode_key=EP1, expires_at=CUTOFF),
			dict(invoiced_item_id=IID, episode_key="  ", expires_at=CUTOFF),
			dict(invoiced_item_id=IID, episode_key=EP1, expires_at=None),
			dict(invoiced_item_id=IID, episode_key=EP1, expires_at="next tuesday"),
			dict(invoiced_item_id=IID, episode_key="E" * 141, expires_at=CUTOFF),
		]
		for kwargs in cases:
			with self.subTest(kwargs=kwargs):
				site = Site()
				self.assertRefused(site.api.open_hold(**kwargs), "ERP_VALIDATION")
				self.assertEqual(site.db.log, [])
		for reason in ("", None, "R" * 141):
			with self.subTest(reason=reason):
				site = Site()
				self.assertRefused(site.close(reason=reason), "ERP_VALIDATION")
				self.assertEqual(site.db.log, [])

	def test_a_caller_that_cannot_write_delivery_notes_is_refused(self):
		self.site.frappe.permitted = False

		self.assertRefused(self.site.open(), "ERP_VALIDATION")
		self.assertRefused(self.site.close(), "ERP_VALIDATION")
		self.assertEqual(self.site.frappe.permission_checks, [("Delivery Note", "write")] * 2)
		self.assertEqual(self.site.db.log, [])

	def test_lock_waits_and_deadlocks_are_retryable(self):
		f = self.site.frappe
		for exc in (
			f.QueryDeadlockError("deadlock"),
			f.QueryTimeoutError("lock wait"),
			f.TimestampMismatchError("x"),
		):
			with self.subTest(exc=type(exc).__name__):
				self.site.db.fail_next = exc
				self.assertRefused(self.site.open(), "LOCK_RETRY", retryable=True)
				self.assertEqual(self.site.db.log[-1], "rollback")

	def test_a_concurrent_first_open_is_retryable(self):
		f = self.site.frappe

		def clash(table, row):
			raise f.DuplicateEntryError(table, row.get("name"))

		self.site.db.write_row = clash
		self.assertRefused(self.site.open(), "LOCK_RETRY", retryable=True)
		self.assertEqual(self.site.db.rollbacks, 1)

	def test_anything_else_is_raised_after_a_rollback(self):
		self.site.db.fail_next = RuntimeError("db gone")
		with self.assertRaises(RuntimeError):
			self.site.open()
		self.assertEqual(self.site.db.rollbacks, 1)

	def test_both_methods_are_whitelisted_for_post_only(self):
		self.assertEqual(self.site.frappe.whitelisted, {"open_hold": ["POST"], "close_hold": ["POST"]})


# ==================================================================================================
# invoice_hold: the coverage read
# ==================================================================================================
class TestHeldDeliveries(unittest.TestCase):
	def setUp(self):
		self.site = Site()
		self.held = self.site.hold_mod.held_deliveries

	def names(self, rows):
		return [row.delivery_note for row in rows]

	def test_no_open_hold_reads_no_delivery_note(self):
		self.site.dn("DN-1", "2026-09-10", iid=IID)
		self.assertEqual(self.held(["SOI-1"], "2026-09-01", "2026-09-30"), [])
		self.assertFalse(any("tabDelivery Note" in q for q in self.site.db.log), self.site.db.log)

	def test_no_lines_reads_nothing(self):
		self.site.hold(IID, EP1)
		self.assertEqual(self.held([None, ""], "2026-09-01", "2026-09-30"), [])
		self.assertEqual(self.site.db.log, [])

	def test_a_held_delivery_in_the_window_is_returned_with_its_episode(self):
		self.site.hold(IID, EP1)
		self.site.dn("DN-1", "2026-09-10", iid=IID)
		self.site.dn("DN-2", "2026-09-11", iid="other-item")

		rows = self.held(["SOI-1"], "2026-09-01", "2026-09-30")

		self.assertEqual(len(rows), 1)
		self.assertEqual(
			(rows[0].delivery_note, rows[0].posting_date, rows[0].invoiced_item_id, rows[0].episode_key),
			("DN-1", "2026-09-10", IID, EP1),
		)
		self.assertEqual(rows[0].expires_at, CUTOFF_DB)

	def test_window_bounds_are_inclusive_and_a_blank_bound_is_open(self):
		self.site.hold(IID, EP1)
		self.site.dn("DN-1", "2026-09-10", iid=IID)
		cases = [
			(("2026-09-10", "2026-09-10"), ["DN-1"]),
			(("2026-09-11", "2026-09-30"), []),
			(("2026-09-01", "2026-09-09"), []),
			((None, None), ["DN-1"]),
			(("", ""), ["DN-1"]),
			((None, "2026-09-10"), ["DN-1"]),
			(("2026-09-10", None), ["DN-1"]),
			((datetime.date(2026, 9, 1), datetime.date(2026, 9, 30)), ["DN-1"]),
		]
		for (frm, to), expected in cases:
			with self.subTest(frm=frm, to=to):
				self.assertEqual(self.names(self.held(["SOI-1"], frm, to)), expected)

	def test_only_the_invoices_sales_order_lines_count(self):
		self.site.hold(IID, EP1)
		self.site.dn("DN-1", "2026-09-10", lines=[("SOI-2", 500)], iid=IID)
		self.assertEqual(self.held(["SOI-1"], None, None), [])
		self.assertEqual(self.names(self.held(["SOI-1", "SOI-2"], None, None)), ["DN-1"])

	def test_a_delivery_on_two_lines_is_one_row(self):
		self.site.hold(IID, EP1)
		self.site.dn("DN-1", "2026-09-10", lines=[("SOI-1", 500), ("SOI-2", 700)], iid=IID)
		self.assertEqual(self.names(self.held(["SOI-1", "SOI-2"], None, None)), ["DN-1"])

	def test_only_submitted_non_return_delivery_notes_are_held(self):
		self.site.hold(IID, EP1)
		self.site.dn("DN-DRAFT", "2026-09-10", iid=IID, docstatus=0)
		self.site.dn("DN-CANCELLED", "2026-09-10", iid=IID, docstatus=2)
		self.site.dn("DN-RETURN", "2026-09-10", iid=IID, is_return=1)
		self.site.dn("DN-LIVE", "2026-09-10", iid=IID)  # e.g. the QC amendment: it keeps the stamp
		self.assertEqual(self.names(self.held(["SOI-1"], None, None)), ["DN-LIVE"])

	def test_a_blank_bound_reaches_the_database_as_null(self):
		# MariaDB would compare a date with '' as NULL (no row); SQLite would not, so check the values.
		self.site.hold(IID, EP1)
		self.held(["SOI-1"], "", "")
		values = self.site.db.calls[-1][1]
		self.assertEqual((values["from_date"], values["to_date"]), (None, None))

	def test_a_lapsed_hold_holds_nothing_while_another_hold_is_live(self):
		self.site.hold(IID, EP1, expires="2026-10-15 07:00:00.000000")
		self.site.hold("live-item", EP2)
		self.site.dn("DN-1", "2026-09-10", iid=IID)
		self.site.dn("DN-LIVE", "2026-09-11", iid="live-item")
		self.assertEqual(self.names(self.held(["SOI-1"], None, None)), ["DN-LIVE"])

	def test_a_closed_or_lapsed_hold_holds_nothing(self):
		self.site.dn("DN-1", "2026-09-10", iid=IID)
		for status, expires in (
			("Closed", CUTOFF_DB),
			("Open", "2026-10-15 07:59:59.999999"),
			("Open", "2026-10-15 08:00:00.000000"),  # lapses at the cut-off itself
		):
			with self.subTest(status=status, expires=expires):
				self.site.db.conn.execute("delete from `tabQC Hold`")
				self.site.hold(IID, EP1, status=status, expires=expires)
				self.assertEqual(self.held(["SOI-1"], None, None), [])

	def test_rows_come_oldest_first(self):
		for n, (iid, date) in enumerate([("i3", "2026-09-20"), ("i1", "2026-09-02"), ("i2", "2026-09-10")]):
			self.site.hold(iid, f"{TICKET}-17593000000{n}")
			self.site.dn(f"DN-{iid}", date, iid=iid)
		self.assertEqual(self.names(self.held(["SOI-1"], None, None)), ["DN-i1", "DN-i2", "DN-i3"])


class TestSameCoverageAsWindowInvoices(unittest.TestCase):
	"""held_deliveries(lines, window) returns a held Delivery Note exactly when an invoice on those
	lines and window is one of dn_invoice_link.window_invoices for that Delivery Note: the invoice
	the correction's INVOICED check would find."""

	DATES = tuple(f"2026-09-{d:02d}" for d in (1, 5, 10, 15, 20, 25, 30))

	def test_random_windows_and_deliveries(self):
		rng = random.Random(3266)
		for round_ in range(150):
			site = Site()
			link = ff.load(DN_INVOICE_LINK, site.frappe)
			dns = []
			for n in range(rng.randint(1, 5)):
				lines = rng.sample(["SOI-1", "SOI-2", "SOI-3"], rng.randint(1, 2))
				name, iid = f"DN-{n}", f"item-{n}"
				site.dn(name, rng.choice(self.DATES), lines=[(line, 100) for line in lines], iid=iid)
				site.hold(iid, f"{TICKET}-1759300000{n:03d}")
				dns.append((name, lines))
			inv_lines = rng.sample(["SOI-1", "SOI-2", "SOI-3"], rng.randint(1, 3))
			frm = rng.choice([None, *self.DATES])
			to = rng.choice([None, *self.DATES])
			site.db.insert("Sales Invoice", name="SINV-X", custom_dn_from_date=frm, custom_dn_to_date=to)
			for idx, line in enumerate(inv_lines, 1):
				site.db.insert(
					"Sales Invoice Item", name=f"SINV-X-{idx}", parent="SINV-X", idx=idx, so_detail=line
				)

			held = {row.delivery_note for row in site.hold_mod.held_deliveries(inv_lines, frm, to)}
			for name, lines in dns:
				posting = site.db.row("Delivery Note", name).posting_date
				covered = "SINV-X" in link.window_invoices(lines, posting)
				with self.subTest(round=round_, dn=name, window=(frm, to), lines=inv_lines):
					self.assertEqual(name in held, covered)


# ==================================================================================================
# invoice_hold: Sales Invoice hooks
# ==================================================================================================
class TestRefuseHeldInvoice(unittest.TestCase):
	def setUp(self):
		self.site = Site()
		self.site.hold(IID, EP1)
		self.site.dn("DN-1", "2026-09-10", iid=IID)
		self.mod = self.site.hold_mod

	def test_an_invoice_covering_a_held_delivery_is_refused_with_the_table(self):
		with self.assertRaises(self.mod.InvoiceHeldError) as caught:
			self.mod.refuse_held_invoice(invoice(["SOI-1"], "2026-09-01", "2026-09-30"))

		exc = caught.exception
		self.assertIsInstance(exc, self.site.frappe.ValidationError)
		self.assertEqual([row.delivery_note for row in exc.held], ["DN-1"])
		message = str(exc)
		self.assertIn('<a href="/app/delivery-note/DN-1">DN-1</a>', message)
		self.assertIn(EP1, message)
		self.assertIn("2026-09-10", message)
		self.assertIn("2026-11-01 09:00", message)  # 05:00 UTC, shown in the site's time zone
		self.assertIn("Asia/Dubai", message)
		self.assertEqual(self.site.frappe.thrown[-1][1], "Deliveries under quantity correction")

	def test_a_draft_save_and_a_submit_are_both_checked(self):
		for docstatus in (0, 1):
			with self.subTest(docstatus=docstatus), self.assertRaises(self.mod.InvoiceHeldError):
				self.mod.refuse_held_invoice(invoice(["SOI-1"], docstatus=docstatus))

	def test_invoices_that_cover_no_held_delivery_pass(self):
		for doc in (
			invoice(["SOI-1"], "2026-09-11", "2026-09-30"),  # window leaves it out
			invoice(["SOI-9"], None, None),  # other lines
			invoice([], None, None),  # no Sales Order lines at all
			invoice(["SOI-1"], None, None, is_return=1),  # credit note
			invoice(["SOI-1"], None, None, docstatus=2),
		):
			with self.subTest(doc=doc.__dict__):
				self.mod.refuse_held_invoice(doc)

	def test_install_and_migrate_are_not_checked(self):
		for flag in ("in_install", "in_migrate"):
			with self.subTest(flag=flag):
				self.site.frappe.flags[flag] = True
				self.mod.refuse_held_invoice(invoice(["SOI-1"]))
				self.site.frappe.flags[flag] = False

	def test_values_in_the_table_are_escaped(self):
		self.site.db.set_value("QC Hold", IID, {"episode_key": "<b>x</b>"})
		with self.assertRaises(self.mod.InvoiceHeldError) as caught:
			self.mod.refuse_held_invoice(invoice(["SOI-1"]))
		self.assertIn("&lt;b&gt;x&lt;/b&gt;", str(caught.exception))
		self.assertNotIn("<b>x</b>", str(caught.exception))


class TestRefuseNewlyHeldAfterSubmit(unittest.TestCase):
	def setUp(self):
		self.site = Site()
		self.site.hold(IID, EP1)
		self.site.dn("DN-1", "2026-09-20", iid=IID)
		self.mod = self.site.hold_mod

	def submitted(self, frm, to, before_window):
		before = invoice(["SOI-1"], *before_window, docstatus=1)
		return invoice(["SOI-1"], frm, to, docstatus=1, before=before)

	def test_widening_the_window_over_a_held_delivery_is_refused(self):
		with self.assertRaises(self.mod.InvoiceHeldError):
			self.mod.refuse_newly_held_after_submit(
				self.submitted("2026-09-01", "2026-09-30", ("2026-09-01", "2026-09-15"))
			)

	def test_an_edit_that_leaves_coverage_alone_passes(self):
		self.mod.refuse_newly_held_after_submit(
			self.submitted("2026-09-01", "2026-09-30", ("2026-09-01", "2026-09-30"))
		)
		self.mod.refuse_newly_held_after_submit(
			self.submitted("2026-09-01", "2026-09-10", ("2026-09-01", "2026-09-30"))
		)

	def test_no_held_delivery_reads_nothing_more(self):
		self.site.db.set_value("QC Hold", IID, {"status": "Closed"})
		self.mod.refuse_newly_held_after_submit(self.submitted(None, None, ("2026-09-01", "2026-09-10")))


# ==================================================================================================
# auto_invoicing: the 12:00 job defers a held Sales Order
# ==================================================================================================
class Job:
	"""A monthly Single-Invoice Sales Order due today (2026-10-15: window 2026-09-01..2026-09-30)."""

	def __init__(self):
		self.site = Site()
		self.frappe = self.site.frappe
		self.db = self.site.db
		self.mod = ff.load(AUTO_INVOICING, self.frappe)
		self.frappe.singles[("Fuelbuddy Settings", "enable_auto_invoicing")] = 1
		self.made = []
		self.issues = []
		self.mod._make_draft_invoice = lambda so, group, frm, to: self.made.append(
			(so.name, [d.name for d in group], str(frm), str(to))
		)
		self.mod._log_invoicing_issue = lambda so_name, subject, detail=None: self.issues.append(so_name)
		for so in ("SO-1", "SO-2"):
			self.sales_order(so)

	def sales_order(self, name, customer="CUST-1"):
		self.db.insert(
			"Sales Order",
			name=name,
			customer=customer,
			custom_invoicing_type="Single Invoice",
			custom_invoicing_frequency="30",
			custom_last_invoiced_upto="2026-08-31",
			transaction_date="2026-01-01",
		)
		self.db.insert("Sales Order Item", name=f"{name}-L1", parent=name)
		if not self.db.row("Customer", customer):
			self.db.insert("Customer", name=customer)

	def cursor(self, so="SO-1"):
		return self.db.row("Sales Order", so).custom_last_invoiced_upto

	def deferral_lines(self):
		return [msg for name, _level, msg in self.frappe.logged if name == "fuelbuddy_crm.qc_hold"]


class TestAutoInvoicingDefers(unittest.TestCase):
	def setUp(self):
		self.job = Job()
		self.job.site.dn("DN-1", "2026-09-10", lines=[("SO-1-L1", 1000)], iid=IID, so="SO-1")
		self.job.site.dn("DN-2", "2026-09-12", lines=[("SO-1-L1", 800)], iid="free-item", so="SO-1")

	def test_a_held_delivery_in_the_window_defers_the_whole_sales_order(self):
		self.job.site.hold(IID, EP1)

		self.job.mod._invoice_sales_order("SO-1", True)

		self.assertEqual(self.job.made, [])
		self.assertEqual(self.job.cursor(), "2026-08-31")
		self.assertFalse(any("dni.qty" in q for q in self.job.db.log), "the Delivery Notes were read")
		lines = self.job.deferral_lines()
		self.assertEqual(len(lines), 1)
		self.assertIn("SO-1", lines[0])
		self.assertIn(f"DN-1 (episode {EP1})", lines[0])
		self.assertIn("2026-09-01 to 2026-09-30", lines[0])

	def test_one_log_line_per_sales_order_per_day(self):
		self.job.site.hold(IID, EP1)
		self.job.mod._invoice_sales_order("SO-1", True)
		self.job.mod._invoice_sales_order("SO-1", True)
		self.assertEqual(len(self.job.deferral_lines()), 1)

		self.job.frappe.today = "2026-10-16"
		self.job.mod._invoice_sales_order("SO-1", True)
		self.assertEqual(len(self.job.deferral_lines()), 2)
		self.assertEqual(self.job.made, [])

	def test_without_the_cache_the_line_is_still_logged(self):
		self.job.site.hold(IID, EP1)
		self.job.frappe.cache.broken = True
		self.job.mod._invoice_sales_order("SO-1", True)
		self.assertEqual(len(self.job.deferral_lines()), 1)
		self.assertEqual(self.job.made, [])

	def test_without_a_hold_the_sales_order_is_invoiced_as_before(self):
		self.job.mod._invoice_sales_order("SO-1", True)

		self.assertEqual(self.job.made, [("SO-1", ["DN-1", "DN-2"], "2026-09-01", "2026-09-30")])
		self.assertEqual(self.job.cursor(), "2026-09-30")
		self.assertEqual(self.job.deferral_lines(), [])

	def test_a_closed_or_lapsed_hold_does_not_defer(self):
		self.job.site.hold(IID, EP1, status="Closed")
		self.job.mod._invoice_sales_order("SO-1", True)
		self.assertEqual(len(self.job.made), 1)

	def test_a_held_delivery_outside_the_window_does_not_defer(self):
		self.job.site.hold("oct-item", EP1)
		self.job.site.dn("DN-OCT", "2026-10-02", lines=[("SO-1-L1", 500)], iid="oct-item", so="SO-1")
		self.job.mod._invoice_sales_order("SO-1", True)
		self.assertEqual(self.job.made, [("SO-1", ["DN-1", "DN-2"], "2026-09-01", "2026-09-30")])

	def test_a_hold_on_another_sales_order_does_not_defer_this_one(self):
		self.job.site.hold(IID, EP1)
		self.job.site.dn("DN-3", "2026-09-15", lines=[("SO-2-L1", 700)], iid="so2-item", so="SO-2")

		self.job.mod._invoice_sales_order("SO-2", True)

		self.assertEqual(self.job.made, [("SO-2", ["DN-3"], "2026-09-01", "2026-09-30")])
		self.assertEqual(self.job.cursor("SO-2"), "2026-09-30")

	def test_a_hold_that_lands_during_the_insert_defers_the_sales_order_not_an_issue(self):
		self.job.site.dn("DN-3", "2026-09-15", lines=[("SO-2-L1", 700)], iid="so2-item", so="SO-2")
		mod = self.job.mod
		real = mod._invoice_sales_order
		held_row = ff._dict(delivery_note="DN-1", episode_key=EP1)

		def invoice_so(so_name, all_customers=False):
			if so_name == "SO-1":
				# validate refused the scheduler draft: a hold opened after the job's own check
				raise mod.InvoiceHeldError(held=[held_row])
			return real(so_name, all_customers)

		mod._invoice_sales_order = invoice_so
		self.job.db.log.clear()

		mod.generate_sales_invoices()

		self.assertEqual(self.job.issues, [])
		self.assertEqual(self.job.made, [("SO-2", ["DN-3"], "2026-09-01", "2026-09-30")])
		self.assertEqual(self.job.cursor("SO-1"), "2026-08-31")
		self.assertEqual(self.job.db.log.count("rollback"), 1)
		self.assertEqual(len(self.job.deferral_lines()), 1)
		self.assertIn(f"DN-1 (episode {EP1})", self.job.deferral_lines()[0])


# ==================================================================================================
# wiring
# ==================================================================================================
class TestWiring(unittest.TestCase):
	def test_sales_invoice_hooks_call_the_hold_check_first(self):
		hooks = ff.load(HOOKS, ff.make())
		si = hooks.doc_events["Sales Invoice"]
		self.assertEqual(si["validate"][0], "fuelbuddy_crm.invoice_hold.refuse_held_invoice")
		self.assertIn("fuelbuddy_crm.force_majeure.apply_force_majeure", si["validate"])
		self.assertEqual(
			si["before_update_after_submit"], "fuelbuddy_crm.invoice_hold.refuse_newly_held_after_submit"
		)
		mod = Site().hold_mod
		for path in (si["validate"][0], si["before_update_after_submit"]):
			self.assertTrue(callable(getattr(mod, path.rsplit(".", 1)[1])), path)

	def test_the_doctype_the_code_writes(self):
		with open(DOCTYPE_JSON) as f:
			meta = json.load(f)
		fields = {f["fieldname"]: f for f in meta["fields"]}
		self.assertEqual(
			(meta["name"], meta["module"], meta["autoname"]),
			("QC Hold", "Fuelbuddy CRM", "field:invoiced_item_id"),
		)
		self.assertEqual(
			(fields["invoiced_item_id"].get("unique"), fields["invoiced_item_id"].get("reqd")), (1, 1)
		)
		self.assertEqual(fields["status"]["options"].split("\n"), ["Open", "Closed"])
		for name in ("episode_key", "opened_at", "expires_at", "closed_at", "close_reason"):
			self.assertIn(name, fields)
		self.assertEqual(meta["field_order"], [f["fieldname"] for f in meta["fields"]])
		self.assertTrue(all(not p.get("write") and not p.get("create") for p in meta["permissions"]))


if __name__ == "__main__":
	unittest.main()
