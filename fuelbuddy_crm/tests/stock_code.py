# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Where the billing re-check tests read ERPNext's and frappe's own code (IDEV-3268).

The tests compare the re-check with the real stock code, never with a copy kept in this repo. The code
comes from the checkout named by BILLING_RECHECK_ERPNEXT / BILLING_RECHECK_FRAPPE (for example a
``git worktree add ... v15.96.0``), else from the package this interpreter would import (a bench's
virtualenv), found without importing it. When there is neither, the test is skipped and says why.
"""

import importlib.util
import os
import pathlib
import unittest


def installed_root(app):
	"""The directory that holds the ``app`` package this interpreter would import, or None."""
	spec = importlib.util.find_spec(app)  # locates the package without importing it
	return pathlib.Path(spec.origin).parent.parent if spec and spec.origin else None


def checkout_root(app):
	"""The ``app`` checkout to read; raises unittest.SkipTest (a skip, also from setUpClass) without one."""
	env = f"BILLING_RECHECK_{app.upper()}"
	root = pathlib.Path(os.environ[env]) if os.environ.get(env) else installed_root(app)
	if not root or not (root / app / "__init__.py").is_file():
		raise unittest.SkipTest(
			f"{app} is not installed: this test reads {app}'s own code. Run it where {app} is importable "
			f"(bench run-tests), or set {env} to a checkout of {app}."
		)
	return root
