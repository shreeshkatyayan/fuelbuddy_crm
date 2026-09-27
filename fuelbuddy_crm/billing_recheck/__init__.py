"""Fast Delivery Note / Sales Invoice billing re-check on large Sales Order lines (IDEV-3268).

ERPNext v15.96.0 re-walks every Delivery Note on a Sales Order line at each Delivery Note or Sales
Invoice event, and refreshes every one of them inside the event's transaction. On a line with ~34k
Delivery Notes that holds the Sales Order line lock for minutes. This package gives the same stored
billing values while writing only what changes:

- walk.py: the replacement for update_billed_amount_based_on_so (fast path, changed-rows-only FIFO with
  ERPNext's own arithmetic, stock fallbacks) and the return add-on;
- bulk.py: the invoice-side set-based refresh of per_billed / status / Label comments;
- guard.py + fingerprint.py + fingerprint_pins.json: runs stock code unless the running ERPNext and
  frappe code is exactly the code this was proven against (scripts/check_billing_walk_fingerprint.py
  runs the same check in CI / before a deploy);
- install.py: patches each process from crm's before_request / before_job hooks; ``status()`` is the
  System Manager health check;
- line_guard.py: with a switch on, a Delivery Note / Sales Invoice submit or cancel takes the Sales Order
  line lock first and refuses (retryable) when its snapshot misses an event committed on the line;
- config.py: the site_config.json switches (everything is off until they are set);
- observe.py: per-path counters, logs and deferred alerts;
- api.py: stock_would_change / recompute_line / header_drift / refresh_dns for the repair job and the
  drift audit.

This module imports nothing, so the pure parts (fifo, fingerprint) load without frappe.
"""
