# Delivery Note key columns: safe setup

**Bottom line:** two unique columns go onto `tabDelivery Note` through a short online step that is safe to stop and run again. Run it in a quiet moment just before `bench migrate`. If nobody does, the migrate runs it first thing.

## What it adds

| Column or index | What it is for | Shape |
|---|---|---|
| `custom_live_invoiced_item_id` | One live Delivery Note per invoiced item | `varchar(140)`, NULL, unique index of the same name |
| `custom_qc_idempotency_key` | One Delivery Note per quantity-correction episode | same |
| `custom_invoiced_item_id_index` | Looking Delivery Notes up by invoiced item. Only built if missing (production already has it) | plain index |

A new column does nothing until its Custom Field exists. The migrate creates the Custom Fields afterwards. Frappe then finds the columns in place and changes nothing in the table.

## Why not let Frappe add them

- Frappe adds a unique field with one `ALTER` that rebuilds the whole table. That took 17-33 s on a production-sized copy (~640k rows); read it as an order of magnitude.
- It saves the Custom Field before that `ALTER` starts. For the whole rebuild, every Delivery Note save from a worker that has seen the new field fails with "Unknown column".
- If that `ALTER` is interrupted, the failure sticks: a second `bench migrate` sees the field as done and never adds the column.

## How the step works

1. **Reads first.** It checks `information_schema` and stops, changing nothing, if the table is in a state it should not touch (see [If it stops](#if-it-stops)).
2. **Adds each missing column instantly.** `ALGORITHM=INSTANT` (0.03-0.11 s measured), or `NOCOPY` if INSTANT is refused. Never a table rebuild.
3. **Builds each unique index online.** `ALGORITHM=INPLACE, LOCK=NONE`: reads and writes carry on during the build (5.6-7.2 s measured). Adding an index does not rebuild the table, so INPLACE takes the same path as the `NOCOPY` form that was timed.
4. **Caps every lock wait at 5 s.** Each `ALTER` needs the table's metadata lock for a moment at its start and end, and while it waits, every Delivery Note read and write queues behind it. With `lock_wait_timeout = 5` that queue lasts 5 s at most; the `ALTER` then fails (MariaDB error 1205) having changed nothing, and is tried again after 2, 4, 8 and 16 s. Five tries in all.
5. **Checks the result** is exactly what Frappe would build, so the migrate has nothing left to change.

Each `ALTER` completes or changes nothing. Wherever the step stops, the table is valid, Delivery Note saves keep working, and running it again carries on from there.

## Running it on production

### 1. Read-only checks, any time before

```sql
SELECT VERSION();
SELECT @@global.innodb_instant_alter_column_allowed;
SELECT @@global.wsrep_on;
SHOW INDEX FROM `tabDelivery Note`
  WHERE Column_name IN ('custom_invoiced_item_id', 'custom_live_invoiced_item_id', 'custom_qc_idempotency_key');
SELECT `unique`, search_index FROM `tabCustom Field` WHERE name = 'Delivery Note-custom_invoiced_item_id';
```

| Check | Go | Stop and re-plan |
|---|---|---|
| Version | MariaDB 10.6 or later (measured on 10.6, 10.11 and 11.4) | Older than 10.3 |
| `innodb_instant_alter_column_allowed` | `add_last` or `add_drop_reorder` | `never` |
| `wsrep_on` | `OFF`, or an "unknown variable" error | `ON`: Galera runs every `ALTER` across the cluster and blocks the table while it does |
| The stamp field's `unique` | `0` | `1` with no unique index on `custom_invoiced_item_id` |

Two things this step does not cover:

- **Other Delivery Note columns.** Creating the Custom Fields makes Frappe re-check the whole table. Any other column that has drifted from its definition gets its own `ALTER`, with no cap and possibly a rebuild. Preview that before the release (the release runbook's DDL preview) and expect nothing but the new fields.
- **Replicas.** A replica applies each `ALTER` in one go, so it lags by about the index build time (a few seconds each).

### 2. Pick a quiet moment

Right before running the step:

```sql
SELECT trx_id, trx_started, trx_mysql_thread_id
FROM information_schema.innodb_trx
WHERE trx_started < NOW() - INTERVAL 30 SECOND;

SHOW FULL PROCESSLIST;
```

- Go when no transaction is older than about 30 s, and no invoice run, bulk cancel, backup or open `bench console` is working on Delivery Notes.
- One open transaction on the table blocks the step. Each try then costs up to 5 s of queued saves (not failed ones).

### 3. Run the step

In the deploy, after pulling the app code and before `bench migrate` (no restart needed):

```bash
bench --site <site> execute fuelbuddy_crm.dn_key_columns.run
```

- About 15 s at production size: two instant column adds, two index builds.
- It prints each statement, how long it took, and every retry. If it stops, it exits non-zero and says why.
- To change the cap or the number of tries: `--kwargs "{'lock_wait': 5, 'attempts': 5}"`. A longer cap means fewer retries but longer queues when something holds the table.

What a run looks like (shortened):

```text
Delivery Note key columns (tabDelivery Note):
  custom_live_invoiced_item_id: missing
  custom_qc_idempotency_key: missing
  custom_invoiced_item_id: Custom Field unique=0 search_index=1, plain index custom_invoiced_item_id_index
  lock_wait_timeout = 5 s for this session (was 86400)
  ALTER TABLE `tabDelivery Note` ADD COLUMN IF NOT EXISTS `custom_live_invoiced_item_id` varchar(140) DEFAULT NULL, ALGORITHM=INSTANT
    done in 0.05 s
  ALTER TABLE `tabDelivery Note` ADD UNIQUE INDEX IF NOT EXISTS `custom_live_invoiced_item_id` (`custom_live_invoiced_item_id`), ALGORITHM=INPLACE, LOCK=NONE
    MariaDB 1205 after 5.0 s: nothing changed; try 2 of 5 in 2 s
    done in 6.31 s
  ...
  lock_wait_timeout back to 86400
  ready: Frappe's sync will not alter these columns
```

### 4. Verify

```sql
SELECT column_name, column_type, column_default, is_nullable, column_key
FROM information_schema.columns
WHERE table_schema = DATABASE() AND table_name = 'tabDelivery Note'
  AND column_name IN ('custom_live_invoiced_item_id', 'custom_qc_idempotency_key');
```

Expect two rows, each `varchar(140)`, `NULL`, `YES`, `UNI`. Any other shape makes Frappe alter the column during the migrate.

### 5. Migrate

`bench --site <site> migrate`, then clear the cache and restart, as in every deploy.

- The first patch, `precreate_dn_key_columns`, prints "ready: nothing to change". It only reads `information_schema`, so saves are not held up.
- The patches that create the key fields' Custom Fields make Frappe issue no `ALTER` for them.

## What to watch while it runs

| Where | Normal | Act when |
|---|---|---|
| The step's output | Column adds "done" in well under a second, index builds in about 10 s or less | Retry after retry: something keeps the table open (see below) |
| `SHOW PROCESSLIST` | Some sessions in "Waiting for table metadata lock", gone within 5 s | Sessions waiting much longer than 5 s: not this step (its waits are capped). Look for another `ALTER` or a long transaction |
| The app | Delivery Note saves may pause for up to 5 s per try | Saves fail: stop and look. The step never makes a save fail |

To find what holds the table: `SELECT * FROM information_schema.innodb_trx ORDER BY trx_started;`. The oldest transaction is usually it.

## If it stops

It always says why, the table is valid, and running it again is safe.

| The message starts with | Meaning | What to do |
|---|---|---|
| `Gave up after 5 tries` | Something kept the table busy for more than 5 s at every try. It lists what this run did finish. | Wait for a quieter moment (step 2) and run it again. Raising the cap only makes the queue longer. |
| `MariaDB would add ... only by rebuilding the table` | INSTANT and NOCOPY were both refused, usually because `innodb_instant_alter_column_allowed` is `never`. Nothing changed. | Stop. Do not add the column without an `ALGORITHM` clause on a busy table: that is the rebuild this step exists to avoid. Plan a maintenance window instead. |
| `Nothing was changed. The table is in a state this step does not change by itself` | A key column already exists with another shape (type, NOT NULL, a default, a plain index), or the stamp's Custom Field says Unique while the column has no unique index. | A person decides. A key column is unused until its Custom Field exists, so it can be fixed by hand, with the same 5 s cap. The stamp should not be unique (cancelled originals and returns share it): confirm with the owner before changing its Custom Field. |
| `... holds one value on two rows` | A key column already holds a duplicate, so it cannot be made unique. Only possible if its Custom Field existed before its column. | Find the rows with the query in the message, resolve them with the owner, then run it again. |
| `The ALTERs ran but the table is not ready` | An `ALTER` reported success but the check disagrees, for example an index with the right name on the wrong column. | Look at `SHOW INDEX` for that column and fix it by hand. |

## Undo (before the migrate only)

While no Custom Field uses them, the new columns can go again, with the same cap:

```sql
SET SESSION lock_wait_timeout = 5;
ALTER TABLE `tabDelivery Note` DROP INDEX IF EXISTS `custom_live_invoiced_item_id`, DROP COLUMN IF EXISTS `custom_live_invoiced_item_id`;
ALTER TABLE `tabDelivery Note` DROP INDEX IF EXISTS `custom_qc_idempotency_key`, DROP COLUMN IF EXISTS `custom_qc_idempotency_key`;
```

After the migrate, leave them: the Custom Fields and the code rely on them.

## Days ahead, without the new app code

The same statements by hand, one at a time and only for what is missing. A statement that finds its column or index already there still takes the lock for a moment. If one fails with error 1205, nothing changed: wait and run it again.

```sql
SET SESSION lock_wait_timeout = 5;
ALTER TABLE `tabDelivery Note` ADD COLUMN IF NOT EXISTS `custom_live_invoiced_item_id` varchar(140) DEFAULT NULL, ALGORITHM=INSTANT;
ALTER TABLE `tabDelivery Note` ADD UNIQUE INDEX IF NOT EXISTS `custom_live_invoiced_item_id` (`custom_live_invoiced_item_id`), ALGORITHM=INPLACE, LOCK=NONE;
ALTER TABLE `tabDelivery Note` ADD COLUMN IF NOT EXISTS `custom_qc_idempotency_key` varchar(140) DEFAULT NULL, ALGORITHM=INSTANT;
ALTER TABLE `tabDelivery Note` ADD UNIQUE INDEX IF NOT EXISTS `custom_qc_idempotency_key` (`custom_qc_idempotency_key`), ALGORITHM=INPLACE, LOCK=NONE;
```

Then verify (step 4). At the migrate, the step finds everything in place and only reads.

## Other sites (staging, lab, restored copies)

Nothing to do: `bench migrate` runs the step first, and on a site that is already ready it only reads.

## A site where Delivery Note saves already fail with "Unknown column"

That is the state the old setup could leave behind: a key field's Custom Field exists but its column does not. Run the step (section 3). It adds the missing column first, which ends the failures at once, then builds the unique index. If the index build stops on a duplicate value (saves wrote keys in the few seconds before the index existed), the message gives the query to find the rows.

## Tests

- No site needed, from the app directory: `python -m unittest fuelbuddy_crm.tests.test_dn_key_columns`
- On a site: `bench --site <site> run-tests --app fuelbuddy_crm --module fuelbuddy_crm.tests.test_dn_live_key`

## Where the numbers come from

A production-sized copy of the table (~640k rows) on MariaDB 10.6, 10.11 and 11.4, under a steady Delivery Note write load, in the release review of 29 Sep 2026. The comparisons hold (a rebuild against the online steps, the lock queue, the cap). The seconds depend on production's disk and memory, and the table grows by about 3,500 Delivery Notes a day.
