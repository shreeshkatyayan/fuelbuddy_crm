#!/usr/bin/env python3
"""Pre-deploy check for any ERPNext or Frappe update: is this the code the billing re-check was proven
against? (IDEV-3268)

Run it against the exact erpnext and frappe apps that will be deployed, and block the deploy unless it
exits 0 (README.md: Before any ERPNext or Frappe update):

    python3 scripts/check_billing_walk_fingerprint.py --bench /home/frappe/frappe-bench
    python3 scripts/check_billing_walk_fingerprint.py --erpnext <erpnext checkout> --frappe <frappe checkout>

Per app it checks against fuelbuddy_crm/billing_recheck/fingerprint_pins.json: the version is pinned,
every pinned code segment has the pinned fingerprint, (erpnext) update_billed_amount_based_on_so has
exactly the pinned number of callers across the whole package, and (frappe) every database driver its
pyproject.toml installs is pinned at that version. It needs only the Python standard library.

Exit 0: PASS, everything matches. Exit 1: FAIL, something differs; do not deploy. Exit 2: the check
could not run (usage or read error); do not deploy either.

When an upgrade is intended: review the diff of every segment listed, re-test, then add the version to
the pins file from the block ``--print`` shows. Deployed without that, billing stays correct but slow:
the runtime guard runs stock ERPNext code on anything that is not pinned.
"""

import argparse
import importlib.util
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
FINGERPRINT = HERE.parent / "fuelbuddy_crm" / "billing_recheck" / "fingerprint.py"
README = 'README.md, "Before any ERPNext or Frappe update"'


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


def checked(fp, app, root, version, hashes, pins, scan_callers):
	"""What an ``ok`` line covered, for the summary."""
	out = [f"{len(hashes)} code segments"]
	if app == "erpnext":
		callers = (pins.get(app, {}).get(version) or {}).get("callers")
		out.append(
			f"{callers} callers of {fp.WALK}" if scan_callers else "callers not checked (--no-callers)"
		)
	if app == "frappe":
		drivers = ", ".join(f"{driver} {v}" for driver, v in fp.declared_drivers(root) or [])
		out.append(f"database driver {drivers}")
	return ", ".join(out)


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

	pins_file = args.pins or fp.PINS_FILE
	try:
		pins = fp.load_pins(pins_file)
		results = {}
		for app, path in targets.items():
			root = app_root(path, app)
			version, hashes, mismatches = fp.check_checkout(root, app, pins, scan_callers=not args.no_callers)
			results[app] = (root, version, hashes, mismatches)
	except (OSError, SyntaxError, ValueError) as exc:
		print(f"error: {exc}", file=sys.stderr)
		print(f"FAIL: the check could not run. Do not deploy until it passes ({README}).", file=sys.stderr)
		return 2

	print(f"Billing re-check pre-deploy check against {pins_file}")
	failed = []
	for app, (root, version, hashes, mismatches) in results.items():
		if mismatches:
			failed.append(app)
			print(f"  {app} {version}: MISMATCH at {root}")
			for reason in mismatches:
				print(f"    - {reason}")
		else:
			print(
				f"  {app} {version}: ok ({checked(fp, app, root, version, hashes, pins, not args.no_callers)})"
			)
			print(f"    at {root}")
		if args.print:
			block = {version: {"segments": hashes}}
			if app == "erpnext":
				block[version]["callers"] = fp.tree_callers(root, app)[0]
			print(json.dumps({app: block}, indent=1))
	sys.stdout.flush()  # the verdict goes to stderr: keep it after the report
	if failed:
		print(
			f"FAIL: {' and '.join(failed)} {'does' if len(failed) == 1 else 'do'} not match the pins. The "
			f"billing re-check was not proven against this code, so do not deploy this update yet ({README}).",
			file=sys.stderr,
		)
		return 1
	print("PASS: this is the ERPNext and Frappe code the billing re-check was proven against.")
	return 0


if __name__ == "__main__":
	sys.exit(main())
