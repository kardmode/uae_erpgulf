"""
Moves Company ASP credentials from plain Data fields into encrypted Password storage.

The custom fields are switched to Password first; the column keeps the old plaintext,
which is then encrypted into __Auth and replaced by the usual '*****' placeholder.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils.password import set_encrypted_password

from uae_erpgulf.core.credentials import SECRET_FIELDS
from uae_erpgulf.setup.custom_fields import CUSTOM_FIELDS


def execute():
    create_custom_fields({"Company": CUSTOM_FIELDS["Company"]}, update=True)

    fields = [f for f in SECRET_FIELDS if frappe.db.has_column("Company", f)]
    if not fields:
        return

    columns = ", ".join(f"`{f}`" for f in fields)
    for row in frappe.db.sql(f"SELECT name, {columns} FROM `tabCompany`", as_dict=True):
        for fieldname in fields:
            value = row.get(fieldname)
            if not value or set(str(value)) == {"*"}:
                continue
            set_encrypted_password("Company", row.name, value, fieldname)
            frappe.db.set_value("Company", row.name, fieldname, "*" * len(value), update_modified=False)

    frappe.db.commit()
