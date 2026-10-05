"""Per-path counters, logs and alerts for the billing re-check (IDEV-3268).

Nothing here writes inside the business transaction, and nothing here can fail a submit:
- counters are a redis hash (``billing_recheck:counters``, one field per path), read by
  install.status();
- log lines go to the site's ``billing_recheck`` log file;
- alerts are Error Logs inserted with ``defer_insert`` (frappe queues them in redis and its scheduler
  inserts them later, outside this transaction), at most one per key per hour;
- every function swallows its own errors.
"""

import sys

import frappe

PATHS = (
	"fast",
	"fifo",
	"bulk",
	"fallback_multi_item",
	"fallback_si_detail",
	"guard_disabled",
	"not_installed",
	"stale_line",  # line_guard refused an event whose snapshot missed a committed event on its line
)
# the paths that run at stock cost: an Error Log when the Sales Order line is big (config.alert_rows)
SLOW = frozenset({"fallback_multi_item", "fallback_si_detail", "guard_disabled", "not_installed"})
COUNTERS = "billing_recheck:counters"
ALERT_EVERY_S = 3600
_WARNED = set()


def logger():
	return frappe.logger("billing_recheck", allow_site=True)


def log(message, level="info"):
	try:
		getattr(logger(), level)(message)
	except Exception:
		pass


def count(path, so_detail=None, rows=None, **info):
	"""Count one pass through ``path``; log it; alert when a slow path hits a big line."""
	detail = " ".join(f"{k}={v}" for k, v in info.items())
	message = f"{path} so_detail={so_detail} rows={rows} {detail}".strip()
	try:
		frappe.cache.hincrby(frappe.cache.make_key(COUNTERS), path, 1)
	except Exception:
		pass
	log(message, "warning" if path in SLOW else "info")
	if path in SLOW:
		from fuelbuddy_crm.billing_recheck import config

		try:
			big = rows is None or rows >= config.alert_rows()
		except Exception:
			big = True
		if big:
			key = path if path in ("guard_disabled", "not_installed") else f"{path}:{so_detail}"
			alert(key, f"Billing re-check ran at stock cost: {path}", message)


def alert(key, title, message):
	"""One deferred Error Log per ``key`` per hour on this site."""
	try:
		if not frappe.cache.set(
			frappe.cache.make_key(f"billing_recheck:alerted:{key}"), 1, ex=ALERT_EVERY_S, nx=True
		):
			return
		frappe.log_error(title=title, message=message, defer_insert=True)
	except Exception:
		pass


def warn_once(key, message):
	"""Log ``message`` once per process (stderr and the log file) and raise one alert per hour."""
	if key in _WARNED:
		return
	_WARNED.add(key)
	try:
		print(f"billing_recheck: {message}", file=sys.stderr, flush=True)
	except Exception:
		pass
	log(message, "warning")
	alert(key, "Billing re-check warning", message)


def counters():
	import redis

	try:
		# plain redis HGETALL: frappe's RedisWrapper.hgetall unpickles values, hincrby stores integers
		raw = redis.Redis.hgetall(frappe.cache, frappe.cache.make_key(COUNTERS)) or {}
	except Exception:
		return {}
	return {
		(k.decode() if isinstance(k, bytes) else k): int(v.decode() if isinstance(v, bytes) else v)
		for k, v in raw.items()
	}
