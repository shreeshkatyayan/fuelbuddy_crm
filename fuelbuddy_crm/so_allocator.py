# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Sales Order allocator: the Python copy of erp-functions' FIFO allocator.

Ported from erp-functions ``src/erp/allocateSalesOrders.js`` (``buildSalesOrderQuery``,
``remainingLitres``, ``allocate``) and ``src/erp/reconcileDeliveryNoteLines.js``
(``reconcileDeliveryNoteLines``) for the quantity-correction amend (IDEV-3266), which has
to reshape a Delivery Note's lines inside one ERP transaction.

The two copies must not drift. ``tests/fixtures/so_allocator_cases.json`` holds one shared
set of cases whose expected values were produced by executing the JavaScript functions with
node; ``tests/test_so_allocator.py`` replays them here. Change the rules in both repos and
regenerate the cases, never in one.

Everything in this module is pure: no frappe, no DB. Arithmetic follows the JS operation for
operation (sequential sums, ``Number(x) || fallback`` coercion) so results match to the bit.
"""

import math

# Floating-point dust threshold (erp-functions EPSILON): a residual below this is zero.
EPSILON = 1e-9


def _number(value, fallback):
	"""JavaScript ``Number(value) || fallback``: unparseable, NaN and 0 all give ``fallback``."""
	if value is None or value is False:
		number = 0.0
	elif value is True:
		number = 1.0
	elif isinstance(value, int | float):
		number = float(value)
	else:
		text = str(value).strip()
		if not text:
			number = 0.0
		else:
			try:
				number = float(text)
			except ValueError:
				number = math.nan
	if math.isnan(number) or number == 0:
		return fallback
	return number


def cf(line):
	"""Conversion factor of a line, defaulting to 1 (Litre, or a missing/zero factor)."""
	return _number(line.get("conversion_factor"), 1)


def line_litres(line):
	"""A line's quantity in LITRES: qty in its transaction UOM x conversion_factor."""
	return _number(line.get("qty"), 0) * cf(line)


def build_sales_order_query(customer, billing_address, reference_date):
	"""Candidate Sales Orders for a Delivery Note, oldest first (creation FIFO).

	Same customer + billing address, submitted, and the DN posting date inside the SO validity
	window ``transaction_date <= reference_date <= delivery_date``. Deal type and quantity are
	not filtered here; remaining qty is computed per SO line (``remaining_litres``)."""
	return {
		"filters": [
			["customer", "=", customer],
			["customer_address", "=", billing_address],
			["docstatus", "=", 1],
			["transaction_date", "<=", reference_date],
			["delivery_date", ">=", reference_date],
		],
		"fields": ["name"],
		"order_by": "creation asc",
		"limit": 0,
	}


def remaining_litres(so_item):
	"""Remaining undelivered quantity on a Sales Order line, in LITRES.

	remaining(txn)    = qty - delivered_qty - custom_delivery_note_qty_in_draft
	remaining(litres) = remaining(txn) * conversion_factor

	returned_qty is not added back: delivered_qty is already net of submitted returns."""
	qty = _number(so_item.get("qty"), 0)
	delivered = _number(so_item.get("delivered_qty"), 0)
	draft = _number(so_item.get("custom_delivery_note_qty_in_draft"), 0)
	conversion_factor = _number(so_item.get("conversion_factor"), 1)
	remaining_txn = qty - delivered - draft
	return remaining_txn * conversion_factor


def allocate(dispensed_litres, candidates):
	"""FIFO-allocate ``dispensed_litres`` across candidate SO lines.

	``candidates`` is ``[{"so": {...}, "soItem": {...}}]`` in FIFO order. Returns
	``{"allocations": [{"so", "soItem", "allocatedLitres"}], "leftover": litres}``; allocations
	holds only lines that received > 0, leftover is what could not be placed (0 when all fit)."""
	leftover = dispensed_litres
	allocations = []
	for candidate in candidates:
		if leftover <= EPSILON:
			break
		available = remaining_litres(candidate["soItem"])
		if available <= 0:
			continue
		take = min(available, leftover)
		allocations.append({"so": candidate["so"], "soItem": candidate["soItem"], "allocatedLitres": take})
		leftover -= take
	if leftover < EPSILON:
		leftover = 0
	return {"allocations": allocations, "leftover": leftover}


def build_line(candidate, allocated_litres, template_line):
	"""A new Delivery Note line against one SO line (erp-functions ``buildDeliveryNoteItem``).

	The SO line's uom and conversion factor are copied (ERPNext rejects a DN line whose uom
	differs from its so_detail's), qty is litres in that uom, and the rate and discount are the
	SO line's. Asset, warehouse, cost centre and division come from ``template_line`` (the DN's
	last line): the spill-over is the same delivery, only billed to another order."""
	so, so_item = candidate["so"], candidate["soItem"]
	conversion_factor = so_item.get("conversion_factor") or 1
	line = {
		"item_code": so_item.get("item_code"),
		"item_name": so_item.get("item_name"),
		"qty": allocated_litres / conversion_factor,
		"uom": so_item.get("uom"),
		"conversion_factor": conversion_factor,
		"rate": so_item.get("rate"),
		"price_list_rate": so_item.get("price_list_rate"),
		"base_price_list_rate": so_item.get("price_list_rate"),
		"discount_percentage": so_item.get("discount_percentage"),
		"discount_amount": so_item.get("discount_amount"),
		"base_rate": so_item.get("rate"),
		"against_sales_order": so.get("name"),
		"so_detail": so_item.get("name"),
	}
	for field in ("custom_customer_asset", "warehouse", "cost_center", "divisions"):
		if template_line.get(field) is not None:
			line[field] = template_line.get(field)
	return line


def reconcile_delivery_note_lines(existing_lines, new_qty_litres, delta_candidates=()):
	"""Reshape a Delivery Note's lines to a new TOTAL in litres.

	- delta ~ 0: unchanged.
	- delta < 0: trim from the LAST line backwards; lines emptied to ~0 are dropped. Emptying
	  the whole DN is ``REDUCTION_TO_ZERO_QTY`` (cancel or delete instead).
	- delta > 0: consume ``delta_candidates`` FIFO (the last line's own SO first): grow the
	  matching line within its SO line's headroom, else append a line. ``leftover`` > 0 means
	  the increase exceeds every SO's headroom.

	Lines are plain dicts (qty in the line UOM); the input list is not modified."""
	if not existing_lines:
		return {"changed": False, "error": "NO_EXISTING_LINES"}

	existing_total = 0
	for line in existing_lines:
		existing_total = existing_total + line_litres(line)
	delta = new_qty_litres - existing_total

	if abs(delta) < EPSILON:
		return {"changed": False, "lines": existing_lines, "leftover": 0}

	lines = [dict(line) for line in existing_lines]

	if delta < 0:
		to_remove = -delta
		i = len(lines) - 1
		while i >= 0 and to_remove > EPSILON:
			litres = line_litres(lines[i])
			trim = min(litres, to_remove)
			lines[i]["qty"] = (litres - trim) / cf(lines[i])
			to_remove -= trim
			i -= 1
		remaining = [line for line in lines if line_litres(line) > EPSILON]
		if not remaining:
			return {"changed": False, "error": "REDUCTION_TO_ZERO_QTY"}
		return {"changed": True, "lines": remaining, "leftover": 0}

	template_line = lines[-1]
	remaining_delta = delta
	for candidate in delta_candidates:
		if remaining_delta <= EPSILON:
			break
		headroom = remaining_litres(candidate["soItem"])
		if headroom <= 0:
			continue
		take = min(headroom, remaining_delta)
		existing = next(
			(
				line
				for line in lines
				if line.get("against_sales_order") == candidate["so"].get("name")
				and line.get("so_detail") == candidate["soItem"].get("name")
			),
			None,
		)
		if existing is not None:
			existing["qty"] = (line_litres(existing) + take) / cf(existing)
		else:
			lines.append(build_line(candidate, take, template_line))
		remaining_delta -= take

	if remaining_delta < EPSILON:
		remaining_delta = 0
	return {"changed": True, "lines": lines, "leftover": remaining_delta}
