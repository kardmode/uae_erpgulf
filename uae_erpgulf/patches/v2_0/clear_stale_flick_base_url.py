"""
Companies using a provider other than Flick may still carry the Flick sandbox URL that used
to be the Base URL default. Clear it so the provider's own default API URL is used and the
invoice submit check (Flick URL requires the Flick provider) does not block them.
"""

import frappe


def execute():
    if not frappe.db.has_column("Company", "custom_base_url"):
        return

    for company in frappe.get_all(
        "Company",
        filters={
            "custom_base_url": ["like", "%flick.network%"],
            "custom_accredited_service_providers": ["!=", "Flick Network L.L.C"],
        },
        pluck="name",
    ):
        frappe.db.set_value("Company", company, "custom_base_url", None, update_modified=False)
