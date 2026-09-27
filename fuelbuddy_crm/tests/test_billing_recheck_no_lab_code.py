# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""No lab-only code ships (IDEV-3268).

The billing re-check was prototyped on local lab branches with whitelisted lab_* endpoints (one could
submit a Delivery Note with billing skipped), a source "tamper" switch for the upgrade guard, internal
state added to every HTTP response and an off-by-default deferred refresh. None of it may reach the
app: this test fails if any marker of it appears anywhere in the package or scripts/.

Pure: ``python -m unittest fuelbuddy_crm.tests.test_billing_recheck_no_lab_code``.
"""

import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
ROOTS = (REPO / "fuelbuddy_crm", REPO / "scripts")
SUFFIXES = {".py", ".js", ".json", ".txt", ".md", ".html"}
FORBIDDEN = {
	"lab-only endpoint or helper": re.compile(r"\blab_\w+"),
	"prototype switch or module name": re.compile(r"\boptiona(?:_\w+)?\b", re.IGNORECASE),
	"guard tamper hook": re.compile(r"tamper", re.IGNORECASE),
	"internal state in the HTTP response": re.compile(r"local\.response\s*\["),
	"deferred refresh prototype": re.compile(r"defer(?:red)?_refresh", re.IGNORECASE),
	"experiment marker": re.compile(r"\bEXPERIMENT\b|never push", re.IGNORECASE),
}


def scanned_files():
	me = pathlib.Path(__file__).resolve()
	for root in ROOTS:
		for path in sorted(root.rglob("*")):
			if (
				path.is_file()
				and path.suffix in SUFFIXES
				and path.resolve() != me
				and "__pycache__" not in path.parts
			):
				yield path


class TestNoLabCode(unittest.TestCase):
	def test_no_lab_only_markers(self):
		hits = []
		for path in scanned_files():
			for lineno, text in enumerate(path.read_text(errors="replace").splitlines(), 1):
				for what, pattern in FORBIDDEN.items():
					if pattern.search(text):
						hits.append(f"{path.relative_to(REPO)}:{lineno}: {what}: {text.strip()[:100]}")
		self.assertEqual(hits, [], "\n" + "\n".join(hits))

	def test_the_scan_sees_the_billing_code(self):
		names = {p.name for p in scanned_files()}
		self.assertTrue({"walk.py", "bulk.py", "guard.py", "check_billing_walk_fingerprint.py"} <= names)

	def test_patterns_catch_the_prototype(self):
		samples = (
			"def lab_guard_state():",
			"@frappe.whitelist()\ndef lab_submit_with_skip_flag(name):",
			'TAMPER = "optiona_lab_tamper"',
			'frappe.local.response["optiona"] = notes',
			'DEFER_SWITCH = "optiona_defer_refresh_over"',
			"# EXPERIMENT (local-only branch, never push)",
		)
		for sample in samples:
			with self.subTest(sample=sample):
				self.assertTrue(any(p.search(sample) for p in FORBIDDEN.values()))
		for fine in ('for s in doc.get("custom_slab_discount"):', "optionally ignoring one DN"):
			with self.subTest(fine=fine):
				self.assertFalse(any(p.search(fine) for p in FORBIDDEN.values()))


if __name__ == "__main__":
	unittest.main()
