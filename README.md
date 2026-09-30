### Fuelbuddy CRM

CRM customizations (Opportunity, Quotation, Lead, Customer)

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop
bench install-app fuelbuddy_crm
```

### Before any ERPNext or Frappe update

The billing re-check (`fuelbuddy_crm/billing_recheck`) replaces part of ERPNext's billing code at runtime. It is proven equal to ERPNext only for the exact ERPNext and Frappe code pinned in `fuelbuddy_crm/billing_recheck/fingerprint_pins.json`. So **ERPNext and Frappe updates wait until the re-check has been confirmed against the new code.**

1. Run the check against the exact apps that will be deployed, and block the deploy unless it exits 0:

   ```bash
   python3 scripts/check_billing_walk_fingerprint.py --bench /home/frappe/frappe-bench
   # or: --erpnext <erpnext checkout> --frappe <frappe checkout>
   ```

   | Exit | Meaning |
   | --- | --- |
   | 0 | PASS: the code the re-check was proven against. Deploy |
   | 1 | FAIL: a version, a code segment or a database driver differs from the pins. Do not deploy |
   | 2 | The check could not run. Do not deploy |

2. On FAIL, an engineer confirms the re-check against the new code before the update ships:
   - review the diff of every segment the check lists, and adjust crm's copy where ERPNext changed;
   - run the pure tests against the new code (they read ERPNext's and Frappe's own source):
     `BILLING_RECHECK_ERPNEXT=<new erpnext> BILLING_RECHECK_FRAPPE=<new frappe> python3 -m unittest fuelbuddy_crm.tests.test_billing_recheck_fifo fuelbuddy_crm.tests.test_billing_recheck_fingerprint fuelbuddy_crm.tests.test_billing_recheck_guard`;
   - run the site tests on a test site with the new versions (`bench --site <test site> run-tests --app fuelbuddy_crm`);
   - pin the version in `fingerprint_pins.json` from the block `--print` shows. Pin a new database driver or driver version (`runtime.db_driver`) only after a test on a site using it shows it stores billed amounts exactly as `written_decimal` in `billing_recheck/fifo.py` predicts.
3. After the deploy, `/api/method/fuelbuddy_crm.billing_recheck.install.status`, opened as a System Manager, should show `"guard_ok": true`.

Skipping this does not corrupt billing. On anything not pinned the re-check switches itself off, ERPNext's own billing runs (slow on large Sales Order lines), and an hourly "Billing re-check warning" Error Log says why.

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/fuelbuddy_crm
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### License

mit
