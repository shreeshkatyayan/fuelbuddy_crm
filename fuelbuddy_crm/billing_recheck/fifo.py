"""ERPNext's own FIFO arithmetic for one Sales Order line, and the write-skip rule (IDEV-3268).

Pure: no frappe import, so it is unit-tested against the stock function itself
(tests/test_billing_recheck_fifo.py). billing_recheck.walk feeds it the rows stock reads.
"""

# A DECIMAL(21,9) value read through frappe (as a float) and written back through set_value stores the
# same decimal only while half a float step is below 5e-10, i.e. below 2**23 (lab: 0 of 254,816 values
# below it moved, 41,622 of 53,829 above it did; IDEV-3268 lab evidence ROUNDTRIP.json). At or
# above it a rewrite by stock can move the stored value, so those rows are always written, as stock does.
EXACT_ROUND_TRIP = 2**23


def stock_values(billed_against_so, rows, direct, flt):
	"""The billed_amt that ERPNext v15.96.0's ``update_billed_amount_based_on_so`` writes to each row.

	The loop of delivery_note.py:772 statement for statement, the same float arithmetic in the same order
	(the fingerprint pins that function):

	- ``billed_against_so``: stock's first query result as stock reads it, ``res and res[0][0] or 0``.
	- ``rows``: stock's ``dn_details`` in stock's order (posting_date, posting_time, dn.name), objects
	  with ``name``, ``amount``, ``si_detail``.
	- ``direct``: {Delivery Note Item name: ``sum(amount)`` of its submitted Sales Invoice Items}, the
	  value stock's per-row query returns (missing or None when there is none).
	- ``flt``: frappe.utils.flt.
	"""
	values = []
	for dnd in rows:
		billed_amt_agianst_dn = 0

		# If delivered against Sales Invoice
		if dnd.si_detail:
			billed_amt_agianst_dn = flt(dnd.amount)
			billed_against_so -= billed_amt_agianst_dn
		else:
			# Get billed amount directly against Delivery Note (stock: `x and x[0][0] or 0` on [(sum,)])
			billed_amt_agianst_dn = direct.get(dnd.name) or 0

		# Distribute billed amount directly against SO between DNs based on FIFO
		if billed_against_so and billed_amt_agianst_dn < dnd.amount:
			pending_to_bill = flt(dnd.amount) - billed_amt_agianst_dn
			if pending_to_bill <= billed_against_so:
				billed_amt_agianst_dn += pending_to_bill
				billed_against_so -= pending_to_bill
			else:
				billed_amt_agianst_dn += billed_against_so
				billed_against_so = 0

		values.append(billed_amt_agianst_dn)
	return values


def needs_write(stored, value):
	"""False only when stock's write of ``value`` would store exactly ``stored`` again."""
	return stored is None or value != stored or abs(value) >= EXACT_ROUND_TRIP


def changed_rows(rows, values):
	"""[(row, value)] for the rows whose stored ``billed_amt`` stock's write would change."""
	return [
		(row, value) for row, value in zip(rows, values, strict=True) if needs_write(row.billed_amt, value)
	]
