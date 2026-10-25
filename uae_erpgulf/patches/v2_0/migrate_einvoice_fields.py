"""
UAE E-Invoicing Migration Patch v2.0.
Executed automatically during 'bench migrate'.
Synchronizes programmatic custom fields and protects historical (pre-go-live) invoices.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from uae_erpgulf.setup.custom_fields import CUSTOM_FIELDS


def execute():
    """Executes schema synchronization and historical data protection."""
    frappe.reload_doc("custom", "doctype", "custom_field")

    # 1. Programmatic Custom Field Sync
    create_custom_fields(CUSTOM_FIELDS, update=True)
    frappe.db.commit()

    # 2. Historical Invoice Protection
    # If a company has e-invoicing enabled but no go-live date, set to today
    companies = frappe.get_all(
        "Company",
        filters={"custom_uae_einvoice_enabled": 1, "custom_einvoice_go_live_date": ["in", ["", None]]},
        pluck="name"
    )
    today = frappe.utils.today()
    for comp in companies:
        frappe.db.set_value("Company", comp, "custom_einvoice_go_live_date", today)

    # 3. Mark invoices submitted before each company's go-live as historical, so they are never
    # transmitted. The status column defaults to "Not Submitted", so blank is not enough.
    for comp in frappe.get_all("Company", fields=["name", "custom_einvoice_go_live_date"]):
        cutoff = comp.custom_einvoice_go_live_date or today
        for doctype in ("Sales Invoice", "Purchase Invoice"):
            if not frappe.db.has_column(doctype, "custom_uae_einvoice_status"):
                continue
            frappe.db.sql(
                f"""
                UPDATE `tab{doctype}`
                SET custom_uae_einvoice_status = 'Pre-Mandate / Historical'
                WHERE docstatus = 1
                  AND company = %s
                  AND posting_date < %s
                  AND IFNULL(custom_uae_einvoice_status, '') IN ('', 'Not Submitted')
                """,
                (comp.name, cutoff),
            )

    frappe.db.commit()
    print("UAE E-Invoicing v2.0 migration executed successfully.")
