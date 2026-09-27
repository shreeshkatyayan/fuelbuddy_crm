#!/usr/bin/env python3
"""Fail when the ERPNext / frappe code the billing re-check relies on differs from what it was proven
against (IDEV-3268).

Run it in CI or before a deploy against the exact erpnext and frappe checkouts that will run in prod:

    python scripts/check_billing_walk_fingerprint.py --bench /home/frappe/frappe-bench
    python scripts/check_billing_walk_fingerprint.py --erpnext <erpnext checkout> --frappe <frappe checkout>

It checks, per app: the version is pinned in fuelbuddy_crm/billing_recheck/fingerprint_pins.json, every
pinned code segment has the pinned fingerprint, and (erpnext) update_billed_amount_based_on_so has
exactly the pinned number of callers across the whole package. Exit 0 when everything matches, 1 on
any mismatch, 2 on a usage or read error. It needs only the Python standard library.

When an upgrade is intended: review the diff of every mismatching segment, then add the version to the
pins file from the block ``--print`` shows. The runtime guard runs stock ERPNext code on any version
that is not pinned, so an unpinned upgrade is safe but loses the speed-up.
"""

import argparse
import importlib.util
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
FINGERPRINT = HERE.parent / "fuelbuddy_crm" / "billing_recheck" / "fingerprint.py"


def load_fingerprint():
	spec = importlib.util.spec_from_file_location("billing_recheck_fingerprint", FINGERPRINT)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def app_root(path, app):
	"""Accept the checkout (containing the ``app`` package) or the package directory itself."""
	path = pathlib.Path(path).resolve()
	if (path / app / "__init__.py").is_file():
		return path
	if path.name == app and (path / "__init__.py").is_file():
		return path.parent
	raise FileNotFoundError(f"no {app} package under {path}")


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
	parser.add_argument("--bench", help="bench directory (uses apps/erpnext and apps/frappe)")
	parser.add_argument("--erpnext", help="erpnext checkout")
	parser.add_argument("--frappe", help="frappe checkout")
	parser.add_argument("--pins", help="pins file (default: the one in this repo)")
	parser.add_argument("--print", action="store_true", help="print the computed fingerprints as JSON")
	parser.add_argument("--no-callers", action="store_true", help="skip the whole-package caller count")
	args = parser.parse_args(argv)

	fp = load_fingerprint()
	targets = {}
	if args.bench:
		targets = {app: pathlib.Path(args.bench, "apps", app) for app in ("erpnext", "frappe")}
	for app in ("erpnext", "frappe"):
		if getattr(args, app):
			targets[app] = getattr(args, app)
	if set(targets) != {"erpnext", "frappe"}:
		parser.print_usage(sys.stderr)
		print("error: give --bench, or both --erpnext and --frappe", file=sys.stderr)
		return 2

	try:
		pins = fp.load_pins(args.pins) if args.pins else fp.load_pins()
		results = {}
		for app, path in targets.items():
			root = app_root(path, app)
			version, hashes, mismatches = fp.check_checkout(root, app, pins, scan_callers=not args.no_callers)
			results[app] = (root, version, hashes, mismatches)
	except (OSError, SyntaxError, ValueError) as exc:
		print(f"error: {exc}", file=sys.stderr)
		return 2

	failed = False
	for app, (root, version, hashes, mismatches) in results.items():
		status = "MISMATCH" if mismatches else "ok"
		print(f"{app} {version} at {root}: {status}")
		for reason in mismatches:
			print(f"  - {reason}")
		failed = failed or bool(mismatches)
		if args.print:
			block = {version: {"segments": hashes}}
			if app == "erpnext":
				block[version]["callers"] = fp.tree_callers(root, app)[0]
			print(json.dumps({app: block}, indent=1))
	if failed:
		print(
			"FAIL: the billing re-check was not proven against this code. Review the listed segments before "
			"pinning this version (see fuelbuddy_crm/billing_recheck/fingerprint_pins.json).",
			file=sys.stderr,
		)
		return 1
	return 0


if __name__ == "__main__":
	sys.exit(main())
