"""
Reporting status now holds the normalised state (reported / pending / failed) instead of the
provider's raw value. Normalise existing accepted invoices, and schedule the pending ones
(e.g. previously "queued") for status polling instead of treating them as reported.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import now_datetime

from uae_erpgulf.adapters.base import FAILED, PENDING, classify_reporting_status
from uae_erpgulf.setup.custom_fields import CUSTOM_FIELDS


def execute():
    for doctype in ("Sales Invoice", "Purchase Invoice"):
        # Polling fields must exist before scheduling (after_migrate runs only after patches)
        create_custom_fields({doctype: CUSTOM_FIELDS[doctype]}, update=True)

        rows = frappe.get_all(
            doctype,
            filters={"docstatus": 1, "custom_uae_einvoice_status": ["in", ["Success", "Reported", "Cleared"]]},
            fields=["name", "custom_reporting_status"],
        )
        for row in rows:
            state = classify_reporting_status(row.custom_reporting_status)
            values = {"custom_reporting_status": state, "custom_uae_einvoice_status": "Success"}
            if state == PENDING:
                values["custom_einvoice_retry_count"] = 0
                values["custom_einvoice_next_retry"] = now_datetime()
            elif state == FAILED:
                values["custom_uae_einvoice_status"] = "Failed"
            frappe.db.set_value(doctype, row.name, values, update_modified=False)
