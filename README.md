### Fuelbuddy CRM

CRM customizations (Opportunity, Quotation, Lead, Customer)

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop
bench install-app fuelbuddy_crm
```

### Operations

- [Delivery Note key columns: safe setup](docs/dn-key-columns.md): the online step that adds the Delivery Note key columns, how to run it before `bench migrate`, and what to watch.

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
