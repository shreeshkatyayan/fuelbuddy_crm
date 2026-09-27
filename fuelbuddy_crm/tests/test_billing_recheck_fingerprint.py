# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""The upgrade guard's fingerprint and the CI script scripts/check_billing_walk_fingerprint.py (IDEV-3268).

Pure: no site needed (``python -m unittest fuelbuddy_crm.tests.test_billing_recheck_fingerprint``).
It also checks real checkouts when it can find them: the erpnext / frappe this interpreter can import
(``bench run-tests`` in the lab), or the directories named by BILLING_RECHECK_ERPNEXT and
BILLING_RECHECK_FRAPPE (checkouts of pinned versions, e.g. ``git worktree add ... v15.96.0``).
"""

import ast
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import tempfile
import textwrap
import unittest

from fuelbuddy_crm.billing_recheck import fingerprint as fp

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check_billing_walk_fingerprint.py"


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

	def test_compare(self):
		pins = {
			"frappe": {"1.0": {"segments": {"a": "x", "b": "y"}}, "2.0": {"segments": {"a": "z", "b": "y"}}}
		}
		self.assertEqual(fp.compare("frappe", "1.0", {"a": "x", "b": "y"}, pins), [])
		self.assertEqual(fp.compare("frappe", "1.0", {"a": "z", "b": "y"}, pins), ["frappe:a"])
		unpinned = fp.compare("frappe", "3.0", {"a": "z", "b": "w"}, pins)
		self.assertIn("not pinned", unpinned[0])
		self.assertEqual(unpinned[1], "frappe:differs from pinned 2.0 in: b")


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
			"runtime": {"pymysql": ["1.1.1"]},
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
		self.assertEqual(
			self.run_script("--erpnext", str(self.tmp / "nope"), "--frappe", str(self.frappe))[0], 2
		)

	def test_print(self):
		code, output = self.run_script(*self.args(), "--print")
		self.assertEqual(code, 0)
		self.assertIn('"callers": 2', output)


def _installed_root(app):
	spec = importlib.util.find_spec(app)  # locates the package without importing it
	return pathlib.Path(spec.origin).parent.parent if spec and spec.origin else None


def checkout_root(testcase, app):
	root = os.environ.get(f"BILLING_RECHECK_{app.upper()}")
	root = pathlib.Path(root) if root else _installed_root(app)
	if not root or not (root / app / "__init__.py").exists():
		testcase.skipTest(f"no {app} checkout (set BILLING_RECHECK_{app.upper()})")
	return root


class TestRealCheckouts(unittest.TestCase):
	"""The code actually installed (or named by the environment) must match its pin."""

	def check(self, app):
		root = checkout_root(self, app)
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
		fixture = (
			pathlib.Path(__file__).parent
			/ "fixtures"
			/ "erpnext_v15_96_0_update_billed_amount_based_on_so.py.txt"
		)
		stock = fp.find(ast.parse(fixture.read_text()), fp.WALK).body
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
		root = checkout_root(self, "erpnext")
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
