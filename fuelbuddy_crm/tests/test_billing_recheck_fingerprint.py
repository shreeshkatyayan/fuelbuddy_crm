# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""The upgrade guard's fingerprint and the pre-deploy check scripts/check_billing_walk_fingerprint.py (IDEV-3268).

Pure: no site needed (``python -m unittest fuelbuddy_crm.tests.test_billing_recheck_fingerprint``).
It also checks real checkouts when it can find them (tests/stock_code.py): the erpnext / frappe this
interpreter can import (``bench run-tests`` in the lab), or the directories named by
BILLING_RECHECK_ERPNEXT and BILLING_RECHECK_FRAPPE (checkouts of pinned versions, e.g. ``git worktree
add ... v15.96.0``). The tests that need one are skipped without it.
"""

import ast
import contextlib
import importlib.util
import io
import json
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import textwrap
import types
import unittest

from fuelbuddy_crm.billing_recheck import fingerprint as fp
from fuelbuddy_crm.tests.stock_code import checkout_root

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check_billing_walk_fingerprint.py"
DRIVER_PINS = {"runtime": {"db_driver": {"PyMySQL": ["1.1.1"]}}}


def load_script():
	spec = importlib.util.spec_from_file_location("check_billing_walk_fingerprint", SCRIPT)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def h(src, dotted):
	return fp.segment_hash(ast.parse(textwrap.dedent(src)), dotted)


class TestCanonical(unittest.TestCase):
	BASE = """
	class A:
		def f(self, x, y=1):
			'''doc'''
			return x + y  # add
	"""

	def test_formatting_comments_and_docstrings_do_not_count(self):
		other = """
		class A:
			def f(self, x, y = 1):
				\"\"\"another docstring\"\"\"
				# a comment
				return (
					x +
					y
				)
		"""
		self.assertEqual(h(self.BASE, "A.f"), h(other, "A.f"))

	def test_any_code_change_counts(self):
		for changed in (
			"class A:\n\tdef f(self, x, y=2):\n\t\treturn x + y\n",
			"class A:\n\tdef f(self, x, y=1):\n\t\treturn x - y\n",
			"class A:\n\tdef f(self, x, y=1):\n\t\treturn y + x\n",
			"class A:\n\tdef f(self, x, y=1):\n\t\tassert x\n\t\treturn x + y\n",
			"class A:\n\t@staticmethod\n\tdef f(self, x, y=1):\n\t\treturn x + y\n",
		):
			with self.subTest(changed=changed):
				self.assertNotEqual(h(self.BASE, "A.f"), h(changed, "A.f"))

	def test_absent_and_last_definition(self):
		self.assertEqual(h(self.BASE, "A.g"), fp.ABSENT)
		self.assertEqual(h(self.BASE, "B.f"), fp.ABSENT)
		redefined = textwrap.dedent(self.BASE) + "\nclass A:\n\tdef f(self):\n\t\treturn 0\n"
		self.assertEqual(h(redefined, "A.f"), h("class A:\n\tdef f(self):\n\t\treturn 0\n", "A.f"))

	def test_class_attribute_and_dict_entry(self):
		src = """
		class D:
			LIMIT = 200_000
			MAP = base | {1: float}
		status_map = {"Delivery Note": [["Draft", None], ["To Bill", "eval:self.per_billed == 0"]], "X": []}
		"""
		self.assertNotEqual(h(src, "D.LIMIT"), fp.ABSENT)
		self.assertNotEqual(h(src, "D.LIMIT"), h(src.replace("200_000", "300_000"), "D.LIMIT"))
		self.assertNotEqual(h(src, "D.MAP"), h(src.replace("float", "str"), "D.MAP"))
		entry = h(src, "status_map[Delivery Note]")
		self.assertEqual(entry, h(src.replace('"X": []', '"X": [1]'), "status_map[Delivery Note]"))
		self.assertNotEqual(entry, h(src.replace("== 0", "== 1"), "status_map[Delivery Note]"))
		self.assertEqual(h(src, "status_map[Sales Order]"), fp.ABSENT)

	def test_count_calls(self):
		tree = ast.parse("def a():\n\tf(1)\n\tm.f(2)\n\tg(f)\n\tx.f\n")
		self.assertEqual(fp.count_calls(tree, "f"), 2)


class TestPins(unittest.TestCase):
	def test_every_pinned_version_covers_every_segment(self):
		pins = fp.load_pins()
		for app, (_modules, segments) in fp.APPS.items():
			self.assertTrue(pins[app], app)
			for version, pinned in pins[app].items():
				with self.subTest(app=app, version=version):
					self.assertEqual(set(pinned["segments"]), set(segments))
		self.assertEqual(pins["erpnext"]["15.96.0"]["callers"], 2)

	def test_line_guard_premise_is_pinned(self):
		for key in (
			"status_updater.StatusUpdater.update_qty",
			"status_updater.StatusUpdater._update_children",
		):
			with self.subTest(key=key):
				self.assertIn(key, fp.ERPNEXT_SEGMENTS)
				self.assertNotEqual(fp.load_pins()["erpnext"]["15.96.0"]["segments"][key], fp.ABSENT)

	def test_db_driver_pins(self):
		pinned = fp.load_pins()["runtime"]["db_driver"]
		# a misspelt driver name would never match, and the guard would stay off everywhere
		self.assertLessEqual(set(pinned), {name for name, _attribute in fp.DB_DRIVERS.values()})
		self.assertIn("1.1.1", pinned["PyMySQL"])  # prod's driver (frappe 15.x requires PyMySQL==1.1.1)

	def test_compare(self):
		pins = {
			"frappe": {"1.0": {"segments": {"a": "x", "b": "y"}}, "2.0": {"segments": {"a": "z", "b": "y"}}}
		}
		self.assertEqual(fp.compare("frappe", "1.0", {"a": "x", "b": "y"}, pins), [])
		self.assertEqual(fp.compare("frappe", "1.0", {"a": "z", "b": "y"}, pins), ["frappe:a"])
		unpinned = fp.compare("frappe", "3.0", {"a": "z", "b": "w"}, pins)
		self.assertIn("not pinned", unpinned[0])
		self.assertEqual(unpinned[1], "frappe:differs from pinned 2.0 in: b")


def connection(module):
	"""A connection object whose class is defined in ``module``, as a driver's own class is."""
	return type("Connection", (), {"__module__": module})()


class TestDbDriver(unittest.TestCase):
	"""Which database driver a connection runs, and whether it is pinned."""

	def setUp(self):
		# PyMySQL 1.1.1 and mysqlclient 2.2.7 as they show their versions
		self.modules = {
			"pymysql": types.SimpleNamespace(
				VERSION_STRING="1.1.1", __version__="1.4.6", version_info=(1, 4, 6, "final", 1)
			),
			"MySQLdb": types.SimpleNamespace(version_info=(2, 2, 7, "final", 0)),
		}

	def driver(self, module):
		return fp.connection_driver(connection(module), self.modules)

	def test_pymysql_version_is_its_own_not_the_mysqldb_compatible_one(self):
		self.assertEqual(self.driver("pymysql.connections"), ("PyMySQL", "1.1.1"))

	def test_mysqlclient(self):
		self.assertEqual(self.driver("MySQLdb.connections"), ("mysqlclient", "2.2.7"))

	def test_pymysql_installed_as_mysqldb_is_still_pymysql(self):
		self.modules["MySQLdb"] = self.modules["pymysql"]  # pymysql.install_as_MySQLdb()
		self.assertEqual(self.driver("pymysql.connections"), ("PyMySQL", "1.1.1"))

	def test_unknown_or_missing_connection(self):
		self.assertEqual(fp.connection_driver(None, self.modules), ("no connection", None))
		conn = sqlite3.connect(":memory:")
		self.addCleanup(conn.close)
		self.assertEqual(fp.connection_driver(conn, sys.modules), ("sqlite3.Connection", None))
		self.assertEqual(self.driver("psycopg2.extensions"), ("psycopg2.extensions.Connection", None))

	def test_driver_package_not_loaded(self):
		self.assertEqual(fp.connection_driver(connection("MySQLdb.connections"), {}), ("mysqlclient", None))

	def test_mismatches(self):
		pinned = "(pinned: PyMySQL 1.1.1)"
		for (driver, version), expected in (
			(("PyMySQL", "1.1.1"), []),
			(("PyMySQL", "1.1.2"), [f"db driver PyMySQL 1.1.2 not pinned {pinned}"]),
			(("PyMySQL", None), [f"db driver PyMySQL None not pinned {pinned}"]),
			(("mysqlclient", "2.2.7"), [f"db driver mysqlclient 2.2.7 not pinned {pinned}"]),
			(("no connection", None), ["db driver unknown: no connection"]),
			(("sqlite3.Connection", None), ["db driver unknown: sqlite3.Connection"]),
		):
			with self.subTest(driver=driver, version=version):
				self.assertEqual(fp.driver_mismatches(driver, version, DRIVER_PINS), expected)
		self.assertEqual(
			fp.driver_mismatches("PyMySQL", "1.1.1", {"runtime": {}}),
			["db driver PyMySQL 1.1.1 not pinned (pinned: none)"],
		)


# frappe's pyproject.toml as frappe 15 requires its database driver (plus a typing stub that is not one)
FRAPPE_PYPROJECT = textwrap.dedent(
	"""
	[project]
	name = "frappe"
	dependencies = [
	    # do NOT add loose requirements on PyMySQL versions.
	    "PyMySQL==1.1.1",
	]

	[project.optional-dependencies]
	dev = ["types-PyMySQL"]
	"""
)
FRAPPE_16_DRIVERS = '"PyMySQL==1.1.2",\n    "mysqlclient==2.2.7",'


class TestDeclaredDrivers(unittest.TestCase):
	"""The database drivers a frappe checkout's pyproject.toml installs (the pre-deploy check)."""

	def drivers(self, text):
		root = pathlib.Path(tempfile.mkdtemp())
		self.addCleanup(shutil.rmtree, root)
		if text is not None:
			(root / "pyproject.toml").write_text(text)
		return fp.declared_drivers(root)

	def test_frappe_15_and_16(self):
		self.assertEqual(self.drivers(FRAPPE_PYPROJECT), [("PyMySQL", "1.1.1")])
		frappe_16 = FRAPPE_PYPROJECT.replace('"PyMySQL==1.1.1",', FRAPPE_16_DRIVERS)
		self.assertEqual(self.drivers(frappe_16), [("PyMySQL", "1.1.2"), ("mysqlclient", "2.2.7")])

	def test_requirement_forms(self):
		for requirement, expected in (
			('"PyMySQL == 1.1.1"', ("PyMySQL", "1.1.1")),
			('"pymysql==1.1.1"', ("PyMySQL", "1.1.1")),
			('"PyMySQL[rsa]==1.1.1"', ("PyMySQL", "1.1.1")),
			("'mysqlclient==2.2.7; sys_platform != \"win32\"'", ("mysqlclient", "2.2.7")),
			('"PyMySQL>=1.1,<2"', ("PyMySQL", ">=1.1,<2")),  # not a version: no pin matches it
			('"PyMySQL===1.1.1"', ("PyMySQL", "===1.1.1")),
			('"PyMySQL"', ("PyMySQL", "(no version)")),
		):
			with self.subTest(requirement=requirement):
				self.assertEqual(self.drivers(f"dependencies = [{requirement}]\n"), [expected])

	def test_what_is_not_a_driver_requirement(self):
		self.assertEqual(self.drivers('# "PyMySQL==9.9"\ndev = ["types-PyMySQL", "types-mysqlclient"]\n'), [])
		self.assertIsNone(self.drivers(None))


def _write_checkout(root, app, bodies=None):
	"""A minimal checkout with every module the segments name; ``bodies``: {module key: source}."""
	modules, _segments = fp.APPS[app]
	(root / app).mkdir(parents=True)
	(root / app / "__init__.py").write_text('__version__ = "0.0.1"\n')
	for key, rel in modules.items():
		path = root / rel
		path.parent.mkdir(parents=True, exist_ok=True)
		if not path.exists():
			path.write_text((bodies or {}).get(key, "x = 1\n"))
	if app == "frappe":
		(root / "pyproject.toml").write_text(FRAPPE_PYPROJECT)


class TestScript(unittest.TestCase):
	ERPNEXT_DN = textwrap.dedent(
		"""
		class DeliveryNote:
			def update_billing_status(self, update_modified=True):
				for so_detail in self.items:
					update_billed_amount_based_on_so(so_detail, update_modified)

		def update_billed_amount_based_on_so(so_detail, update_modified=True):
			return [so_detail]
		"""
	)
	ERPNEXT_SI = "def f(d):\n\treturn update_billed_amount_based_on_so(d)\n"

	def setUp(self):
		self.tmp = pathlib.Path(tempfile.mkdtemp())
		self.addCleanup(shutil.rmtree, self.tmp)
		self.erpnext, self.frappe = self.tmp / "erpnext_src", self.tmp / "frappe_src"
		_write_checkout(
			self.erpnext, "erpnext", {"delivery_note": self.ERPNEXT_DN, "sales_invoice": self.ERPNEXT_SI}
		)
		_write_checkout(self.frappe, "frappe")
		pins = {
			"erpnext": {
				"0.0.1": {"callers": 2, "segments": fp.check_checkout(self.erpnext, "erpnext", {})[1]}
			},
			"frappe": {"0.0.1": {"segments": fp.check_checkout(self.frappe, "frappe", {})[1]}},
			**DRIVER_PINS,
		}
		self.pins = self.tmp / "pins.json"
		self.pins.write_text(json.dumps(pins))
		self.script = load_script()

	def run_script(self, *args):
		out, err = io.StringIO(), io.StringIO()
		with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
			code = self.script.main(list(args))
		return code, out.getvalue() + err.getvalue()

	def args(self):
		return ["--erpnext", str(self.erpnext), "--frappe", str(self.frappe), "--pins", str(self.pins)]

	def test_match_passes(self):
		code, output = self.run_script(*self.args())
		self.assertEqual(code, 0, output)

	def test_summary(self):
		code, output = self.run_script(*self.args())
		self.assertEqual(code, 0, output)
		self.assertIn(
			f"erpnext 0.0.1: ok ({len(fp.ERPNEXT_SEGMENTS)} code segments, 2 callers of {fp.WALK})", output
		)
		self.assertIn(
			f"frappe 0.0.1: ok ({len(fp.FRAPPE_SEGMENTS)} code segments, database driver PyMySQL 1.1.1)",
			output,
		)
		self.assertTrue(
			output.rstrip().endswith(
				"PASS: this is the ERPNext and Frappe code the billing re-check was proven against."
			),
			output,
		)

	def test_drivers_the_update_installs_must_be_pinned(self):
		pinned = "(pinned: PyMySQL 1.1.1)"
		for text, reason in (
			(
				FRAPPE_PYPROJECT.replace("PyMySQL==1.1.1", "PyMySQL==1.1.2"),
				f"frappe:db driver PyMySQL 1.1.2 not pinned {pinned}",
			),
			(
				FRAPPE_PYPROJECT.replace('"PyMySQL==1.1.1",', FRAPPE_16_DRIVERS),
				f"frappe:db driver mysqlclient 2.2.7 not pinned {pinned}",
			),
			(
				FRAPPE_PYPROJECT.replace("PyMySQL==1.1.1", "PyMySQL>=1.1"),
				f"frappe:db driver PyMySQL >=1.1 not pinned {pinned}",
			),
			(
				'[project]\nname = "frappe"\n',
				"frappe:pyproject.toml requires no known database driver (PyMySQL, mysqlclient)",
			),
		):
			with self.subTest(reason=reason):
				(self.frappe / "pyproject.toml").write_text(text)
				code, output = self.run_script(*self.args())
				self.assertEqual(code, 1, output)
				self.assertIn(reason, output)
				self.assertIn("FAIL: frappe does not match the pins.", output)

	def test_missing_pyproject_fails(self):
		(self.frappe / "pyproject.toml").unlink()
		code, output = self.run_script(*self.args())
		self.assertEqual(code, 1, output)
		self.assertIn("frappe:no pyproject.toml, so the database driver it installs is unknown", output)

	def test_package_directory_is_accepted(self):
		code, output = self.run_script(
			"--erpnext", str(self.erpnext / "erpnext"), "--frappe", str(self.frappe), "--pins", str(self.pins)
		)
		self.assertEqual(code, 0, output)

	def test_changed_function_fails(self):
		dn = self.erpnext / fp.ERPNEXT_MODULES["delivery_note"]
		dn.write_text(dn.read_text().replace("return [so_detail]", "return []"))
		code, output = self.run_script(*self.args())
		self.assertEqual(code, 1)
		self.assertIn("erpnext:delivery_note.update_billed_amount_based_on_so", output)
		self.assertIn("FAIL: erpnext does not match the pins.", output)
		self.assertIn('README.md, "Before any ERPNext or Frappe update"', output)

	def test_reformatting_passes(self):
		dn = self.erpnext / fp.ERPNEXT_MODULES["delivery_note"]
		dn.write_text(dn.read_text().replace("return [so_detail]", "# comment\n\treturn [ so_detail ]"))
		code, output = self.run_script(*self.args())
		self.assertEqual(code, 0, output)

	def test_new_override_fails(self):
		dn = self.erpnext / fp.ERPNEXT_MODULES["delivery_note"]
		dn.write_text(
			dn.read_text().replace(
				"class DeliveryNote:", "class DeliveryNote:\n\tdef set_status(self):\n\t\tpass\n"
			)
		)
		code, output = self.run_script(*self.args())
		self.assertEqual(code, 1)
		self.assertIn("delivery_note.DeliveryNote.set_status", output)

	def test_new_caller_fails(self):
		extra = self.erpnext / "erpnext" / "other.py"
		extra.write_text(
			"from x import update_billed_amount_based_on_so\nupdate_billed_amount_based_on_so('a')\n"
		)
		code, output = self.run_script(*self.args())
		self.assertEqual(code, 1)
		self.assertIn("has 3 callers, pinned 2", output)

	def test_unpinned_version_fails(self):
		(self.frappe / "frappe" / "__init__.py").write_text('__version__ = "0.0.2"\n')
		code, output = self.run_script(*self.args())
		self.assertEqual(code, 1)
		self.assertIn("frappe:version 0.0.2 not pinned", output)

	def test_usage_errors(self):
		self.assertEqual(self.run_script("--erpnext", str(self.erpnext))[0], 2)
		code, output = self.run_script("--erpnext", str(self.tmp / "nope"), "--frappe", str(self.frappe))
		self.assertEqual(code, 2)
		self.assertIn("FAIL: the check could not run. Do not deploy until it passes", output)

	def test_both_apps_failing(self):
		(self.frappe / "frappe" / "__init__.py").write_text('__version__ = "0.0.2"\n')
		dn = self.erpnext / fp.ERPNEXT_MODULES["delivery_note"]
		dn.write_text(dn.read_text().replace("return [so_detail]", "return []"))
		code, output = self.run_script(*self.args())
		self.assertEqual(code, 1)
		self.assertIn("FAIL: erpnext and frappe do not match the pins.", output)

	def test_print(self):
		code, output = self.run_script(*self.args(), "--print")
		self.assertEqual(code, 0)
		self.assertIn('"callers": 2', output)


class TestRealCheckouts(unittest.TestCase):
	"""The code actually installed (or named by the environment) must match its pin."""

	def check(self, app):
		root = checkout_root(app)
		version, _hashes, mismatches = fp.check_checkout(root, app)
		self.assertEqual(mismatches, [], f"{app} {version} at {root}")

	def test_erpnext(self):
		self.check("erpnext")

	def test_frappe(self):
		self.check("frappe")


def _source_tree(name):
	return ast.parse((REPO / "fuelbuddy_crm" / "billing_recheck" / name).read_text())


def _same(testcase, ours, stock):
	testcase.assertEqual(fp.canonical(ours), fp.canonical(stock))


class TestCopiedStockCode(unittest.TestCase):
	"""The statements the re-check copies from ERPNext are ERPNext's, statement for statement."""

	def test_walk_queries_are_stocks(self):
		root = checkout_root("erpnext")
		stock = fp.find(ast.parse((root / fp.ERPNEXT_MODULES["delivery_note"]).read_text()), fp.WALK).body
		walk = _source_tree("walk.py")
		# stock: [import Sum, si, si_item, sum_amount, billed = qb...run(), billed = b and b[0][0] or 0,
		#         dn, dn_item, dn_details = qb...run(as_dict=True), updated_dn, for, return]
		billed = fp.without_docstring(fp.find(walk, "billed_against_so_of").body)
		for ours, theirs in zip(billed[:4], stock[1:5], strict=True):
			_same(self, ours, theirs)
		_same(self, billed[4].value, stock[5].value)
		rows = fp.without_docstring(fp.find(walk, "stock_rows").body)
		for ours, theirs in zip(rows[:2], stock[6:8], strict=True):
			_same(self, ours, theirs)
		query = rows[2].value
		for node in ast.walk(query):  # ours selects the stored billed_amt as well: take it out
			if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "select":
				node.args = [a for a in node.args if ast.unparse(a) != "dn_item.billed_amt"]
		_same(self, query, stock[8].value)

	def test_invoice_body_is_stocks(self):
		root = checkout_root("erpnext")
		stock = fp.find(
			ast.parse((root / fp.ERPNEXT_MODULES["sales_invoice"]).read_text()),
			"SalesInvoice.update_billing_status_in_dn",
		)
		wrapper = fp.find(_source_tree("bulk.py"), "wrap_update_billing_status_in_dn")
		ours = next(n for n in ast.walk(wrapper) if isinstance(n, ast.FunctionDef) and n.name == stock.name)
		# ours: [switch and guard checks, import of the module, *stock's body but its final loop, refresh()]
		start = next(i for i, node in enumerate(ours.body) if isinstance(node, ast.ImportFrom)) + 1
		rename = "si_mod.update_billed_amount_based_on_so"
		ours_body = ast.unparse(ast.Module(body=ours.body[start:-1], type_ignores=[]))
		self.assertIn(rename, ours_body)
		stock_body = ast.Module(body=stock.body[:-1], type_ignores=[])
		_same(self, ast.parse(ours_body.replace(rename, fp.WALK)), ast.parse(ast.unparse(stock_body)))
		self.assertIsInstance(stock.body[-1], ast.For)
		self.assertEqual(ast.unparse(ours.body[-1]), "refresh(set(updated_delivery_notes), update_modified)")


if __name__ == "__main__":
	unittest.main()
