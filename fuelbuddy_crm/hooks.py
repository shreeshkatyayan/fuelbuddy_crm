app_name = "fuelbuddy_crm"
app_title = "Fuelbuddy CRM"
app_publisher = "Fuelbuddy"
app_description = "CRM customizations (Opportunity, Quotation, Lead, Customer)"
app_email = "shantanu.mishra@fuelbuddy.in"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "fuelbuddy_crm",
# 		"logo": "/assets/fuelbuddy_crm/logo.png",
# 		"title": "Fuelbuddy CRM",
# 		"route": "/fuelbuddy_crm",
# 		"has_permission": "fuelbuddy_crm.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/fuelbuddy_crm/css/fuelbuddy_crm.css"
# app_include_js = "/assets/fuelbuddy_crm/js/fuelbuddy_crm.js"

# include js, css files in header of web template
# web_include_css = "/assets/fuelbuddy_crm/css/fuelbuddy_crm.css"
# web_include_js = "/assets/fuelbuddy_crm/js/fuelbuddy_crm.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "fuelbuddy_crm/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# Opportunity form: replace the stock "Create" buttons with FuelBuddy's Quotation +
# Planning actions (the Quotation button creates a Draft Quotation from the Opportunity
# via fuelbuddy_crm.quotation_link.create_quotation_from_opportunity).
doctype_js = {
    "Opportunity": "public/js/opportunity.js",
    "Quotation": "public/js/quotation.js",
}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "fuelbuddy_crm/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "fuelbuddy_crm.utils.jinja_methods",
# 	"filters": "fuelbuddy_crm.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "fuelbuddy_crm.install.before_install"
# after_install = "fuelbuddy_crm.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "fuelbuddy_crm.uninstall.before_uninstall"
# after_uninstall = "fuelbuddy_crm.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "fuelbuddy_crm.utils.before_app_install"
# after_app_install = "fuelbuddy_crm.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "fuelbuddy_crm.utils.before_app_uninstall"
# after_app_uninstall = "fuelbuddy_crm.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "fuelbuddy_crm.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Discount <-> Quotation linked each other, so neither could be deleted first (deadlock).
# Making Discount's outgoing links non-blocking makes it one-way: delete the Quotation
# first, then the orphaned Discount.
ignore_links_on_delete = ["Discount"]

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"fuelbuddy_crm.tasks.all"
# 	],
# 	"daily": [
# 		"fuelbuddy_crm.tasks.daily"
# 	],
# 	"hourly": [
# 		"fuelbuddy_crm.tasks.hourly"
# 	],
# 	"weekly": [
# 		"fuelbuddy_crm.tasks.weekly"
# 	],
# 	"monthly": [
# 		"fuelbuddy_crm.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "fuelbuddy_crm.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "fuelbuddy_crm.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# Add FuelBuddy connections to the Connections tab: Finance Dossier + Business
# Documentation on Opportunity, Finance Dossier on Quotation (internal link via
# custom_finance_dossier). The override fn receives the base dashboard `data`
# dict and returns it augmented.
override_doctype_dashboards = {
    "Opportunity": "fuelbuddy_crm.dashboard_overrides.opportunity_dashboard",
    "Quotation": "fuelbuddy_crm.dashboard_overrides.quotation_dashboard",
}

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# IDEV-3268: install the billing re-check (fuelbuddy_crm.billing_recheck) in every web
# request and background job before any document code runs. Inert until switched on in
# site_config.json.
before_request = ["fuelbuddy_crm.billing_recheck.install.before_request"]
# after_request = ["fuelbuddy_crm.utils.after_request"]

# Job Events
# ----------
before_job = ["fuelbuddy_crm.billing_recheck.install.before_job"]
# after_job = ["fuelbuddy_crm.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"fuelbuddy_crm.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []


# Fixtures: all CRM customizations on Opportunity, Quotation, Lead, Customer.
# Sales Order carries only the read-only Discount tab custom fields (mirrored from
# the Quotation), so it is included in the Custom Field filter only -- not in the
# client/server-script or property-setter filters.
_CRM_DOCTYPES = ["Opportunity", "Quotation", "Lead", "Customer"]
# Custom Fields and Property Setters are also owned on Sales Order (the contract SO
# carries the CRM commercial fields and the form layout / naming-series for it).
# Sales Invoice Item carries only the read-only Force Majeure Pricing stamp (IDEV-3129).
_CUSTOM_FIELD_DOCTYPES = _CRM_DOCTYPES + ["Sales Order", "Sales Invoice Item"]
# Property Setters also cover "Opportunity Item": its rate/qty are derived from the
# Opportunity Value section and are made read-only there (BUG-010).
_PROPERTY_SETTER_DOCTYPES = _CUSTOM_FIELD_DOCTYPES + ["Opportunity Item"]
fixtures = [
    {"dt": "Custom Field", "filters": [["dt", "in", _CUSTOM_FIELD_DOCTYPES]]},
    {"dt": "Property Setter", "filters": [["doc_type", "in", _PROPERTY_SETTER_DOCTYPES]]},
    {"dt": "Client Script", "filters": [["dt", "in", _CRM_DOCTYPES]]},
]

doc_events = {
    "Opportunity": {
        "validate": [
            "fuelbuddy_crm.validations.validate_non_negative_opportunity_values",
            "fuelbuddy_crm.validations.apply_opportunity_calculations",
            "fuelbuddy_crm.validations.validate_hse_checks",
            "fuelbuddy_crm.validations.validate_discount_values",
            "fuelbuddy_crm.validations.validate_opportunity_valid_till",
            "fuelbuddy_crm.discount_sync.guard_opportunity_discount",
        ],
        "before_save": [
            "fuelbuddy_crm.validations.sync_opportunity_value_item",
            "fuelbuddy_crm.discount_sync.writeback_opportunity_discount",
        ],
        "on_update": "fuelbuddy_crm.discount_sync.propagate_opportunity_discount",
    },
    "Discount": {
        "validate": "fuelbuddy_crm.validations.validate_discount",
    },
    "Quotation": {
        "before_validate": "fuelbuddy_crm.quotation_link.guard_totals",
        "validate": [
            "fuelbuddy_crm.quotation_link.enforce_one_per_opportunity",
            "fuelbuddy_crm.validations.validate_discount_values",
            "fuelbuddy_crm.validations.default_discount_upto_date",
        ],
        "after_insert": [
            "fuelbuddy_crm.discount_sync.ensure_quotation_discount",
            "fuelbuddy_crm.finance_dossier.create_for_quotation",
        ],
        "on_update": [
            "fuelbuddy_crm.discount_sync.propagate_quotation_discount",
            "fuelbuddy_crm.quotation_link.sync_status_to_opportunity",
        ],
        # FD-first flow: the Finance Dossier must be submitted BEFORE the Quotation;
        # the Quotation submit is what starts the contract SO automation.
        "before_submit": "fuelbuddy_crm.finance_dossier.require_submitted_dossier",
        "on_submit": [
            "fuelbuddy_crm.sales_automation.on_quotation_submit",
            "fuelbuddy_crm.discount_sync.submit_quotation_discount",
        ],
        "on_update_after_submit": "fuelbuddy_crm.sales_automation.on_quotation_submit",
        "on_cancel": "fuelbuddy_crm.discount_sync.cancel_quotation_discount",
    },
    "Sales Order": {
        "before_insert": "fuelbuddy_crm.validations.block_manual_sales_order",
    },
    "Delivery Note": {
        # DN punching guards (moved here from the repo-less fuelbuddy_dubai app):
        # app-level dedup — custom_invoiced_item_id is deliberately NOT unique
        # (versioned amendments reuse it), so enforce "one live DN per invoiced
        # item" in code; and amendment versioning — custom_version is no_copy,
        # so a UI amend resets it to "1" unless recomputed as parent+1.
        # enforce_so_headroom is the authoritative over-delivery gate: ERPNext's own
        # Stock Settings "over_delivery_receipt_allowance" is 1000 (i.e. 1000% tolerated),
        # and the allocator's headroom check is client-side and racy.
        "validate": [
            "fuelbuddy_crm.dn_validation.enforce_single_active_dn",
            "fuelbuddy_crm.dn_validation.enforce_so_headroom",
        ],
        "before_insert": "fuelbuddy_crm.dn_versioning.set_amended_version",
        # Keep Sales Order Item.custom_delivery_note_qty_in_draft (which the allocator
        # subtracts from the SO headroom) in step with the live draft DNs -- including
        # RELEASING it on cancel/delete, which the old Server Script never did.
        # dn_invoice_link: a DN that is submitted, cancelled or deleted inside an already-
        # invoiced window re-runs the invoices that window belongs to.
        "on_update": [
            "fuelbuddy_crm.dn_validation.sync_draft_reservation",
            "fuelbuddy_crm.dn_invoice_link.on_delivery_note_update",
        ],
        # billing_recheck.install.count_not_installed: counts a switched-on billing
        # re-check that ran stock code in a process that was never installed (IDEV-3268).
        "on_submit": [
            "fuelbuddy_crm.dn_validation.sync_draft_reservation",
            "fuelbuddy_crm.billing_recheck.install.count_not_installed",
        ],
        "on_cancel": [
            "fuelbuddy_crm.dn_validation.sync_draft_reservation",
            "fuelbuddy_crm.dn_invoice_link.on_delivery_note_cancel",
            "fuelbuddy_crm.billing_recheck.install.count_not_installed",
        ],
        "on_trash": [
            "fuelbuddy_crm.dn_validation.sync_draft_reservation",
            "fuelbuddy_crm.dn_invoice_link.on_delivery_note_trash",
        ],
    },
    "Finance Dossier": {
        # Keep Quotation.custom_finance_dossier pointing at the current dossier
        # (creation AND manual amendments) — server-side, replacing the old
        # after_save JS writeback that only ran for browser saves.
        "after_insert": "fuelbuddy_crm.finance_dossier.sync_source_reference",
        "on_submit": "fuelbuddy_crm.finance_dossier.sync_source_reference",
        "on_cancel": "fuelbuddy_crm.finance_dossier.sync_source_reference",
    },
    "Sales Invoice": {
        # Manual period invoice: rebuild the lines from the DN date range, split per
        # delivery for Force Majeure (IDEV-3129). Replaces the "Auto Pick of DN at
        # Sales Invoice and Update of Qty" Server Script (removed by patch).
        "before_validate": "fuelbuddy_crm.auto_invoicing.rebuild_lines_from_dn_range",
        # Any invoice (manual or auto, even a Draft) advances the SO's
        # last-invoiced date so the auto-invoicing scheduler never re-bills a
        # period already covered (IDEV-3000).
        # lock_so_lines first (IDEV-3268): update_so_last_invoiced writes the SO header, and a DN
        # submit locks the SO line before the header, so the invoice takes the line first too.
        "after_insert": [
            "fuelbuddy_crm.dn_invoice_link.lock_so_lines",
            "fuelbuddy_crm.auto_invoicing.update_so_last_invoiced",
        ],
        "on_submit": [
            "fuelbuddy_crm.auto_invoicing.update_so_last_invoiced",
            "fuelbuddy_crm.billing_recheck.install.count_not_installed",
        ],
        # IDEV-3129: Force Majeure is decided per delivery and lands on the invoice.
        # Manual invoices are re-rated here per DN-linked line; auto-invoicing splits
        # its own lines in _make_draft_invoice.
        "validate": "fuelbuddy_crm.force_majeure.apply_force_majeure",
        # Manually punched invoices get the same deal discount as scheduler ones;
        # before_save runs after the live DN-qty-rewrite Server Script (validate),
        # before_submit re-applies against the final submitted quantities.
        "before_save": "fuelbuddy_crm.auto_invoicing.apply_manual_invoice_discount_save",
        "before_submit": "fuelbuddy_crm.auto_invoicing.apply_manual_invoice_discount_submit",
        # DN -> Sales Invoice link: every save (draft, submit, allow-on-submit edits of the
        # DN window) re-allocates the DNs this invoice billed, writing only the ones whose link
        # or litres change; cancel/delete clears them. Both lock the SO line(s) first.
        "on_update": "fuelbuddy_crm.dn_invoice_link.allocate_sales_invoice",
        "on_update_after_submit": "fuelbuddy_crm.dn_invoice_link.allocate_sales_invoice",
        "on_cancel": [
            "fuelbuddy_crm.dn_invoice_link.clear_sales_invoice",
            "fuelbuddy_crm.billing_recheck.install.count_not_installed",
        ],
        "on_trash": "fuelbuddy_crm.dn_invoice_link.clear_sales_invoice",
    },
}

scheduler_events = {
    "cron": {
        # 12:00 pm site time; thin wrapper -> long queue, 2h timeout
        "0 12 * * *": [
            "fuelbuddy_crm.auto_invoicing.enqueue_generate_sales_invoices",
        ],
    },
    "monthly": [
        "fuelbuddy_crm.sales_automation.generate_monthly_contract_sales_orders",
    ],
    # IDEV-3268: read-only check that DN per_billed / status / billed_amt and the DN -> invoice
    # links match what the code that owns them would write; Error Log "Billing drift: ..." if not.
    "daily_long": [
        "fuelbuddy_crm.billing_repair.nightly_drift_audit",
    ],
}
