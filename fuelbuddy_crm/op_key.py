# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""The FuelBuddy app's operation key on depot stock posts (IDEV-3201).

erp-functions posts a depot GRN as a Purchase Receipt and a depot transfer as a Stock Entry, and
stamps each with custom_app_op_key: the key of the app operation behind it. The column is uniquely
indexed (patches/add_app_op_key), so ERP accepts a key once. erp-functions looks the key up before
posting and adopts what it finds, so a retried post never books the same fuel twice.

The key stays on a cancelled document, so a re-push finds it cancelled and is refused rather than
posted again. An amendment is a new document, and Frappe copies no_copy fields onto it when it
amends, so the copy would collide with the cancelled original; clear_on_amend drops it first.
"""

OP_KEY_FIELD = "custom_app_op_key"
OP_KEY_DOCTYPES = ("Purchase Receipt", "Stock Entry")


def clear_on_amend(doc, method=None):
	"""before_insert: an amendment starts without the op key of the document it amends."""
	if doc.get("amended_from") and doc.get(OP_KEY_FIELD):
		doc.set(OP_KEY_FIELD, None)
