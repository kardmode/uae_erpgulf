"""
Company credential storage for ASP integrations.

API keys, OAuth secrets, access tokens and webhook secrets are Password fields: Frappe keeps
them encrypted in __Auth and shows only asterisks on the Company form. Always read them through
these helpers. Reading the attribute (company_doc.custom_xflickauthkey) returns the asterisks.
"""

from typing import Dict, Optional

import frappe
from frappe import _

SECRET_FIELDS = (
    "custom_xflickauthkey",
    "custom_client_secret",
    "custom_access_token",
    "custom_secret_of_webhook",
    "custom_webhook_token",
    "custom_asp_api_key",
)


def get_company_secret(company: str, fieldname: str) -> Optional[str]:
    """Decrypted value of a Company Password field, or None when unset."""
    from frappe.utils.password import get_decrypted_password

    return get_decrypted_password("Company", company, fieldname, raise_exception=False) or None


def set_company_secret(company: str, fieldname: str, value: Optional[str]) -> None:
    """Store a secret without loading/saving the whole Company (e.g. from background jobs)."""
    if not value:
        frappe.db.set_value("Company", company, fieldname, None, update_modified=False)
        return
    from frappe.utils.password import set_encrypted_password

    set_encrypted_password("Company", company, value, fieldname)
    frappe.db.set_value("Company", company, fieldname, "*" * len(value), update_modified=False)


def get_flick_headers(company: str) -> Dict[str, str]:
    """Flick auth headers: the X-Flick-Auth-Key when set, otherwise an OAuth bearer token."""
    auth_key = get_company_secret(company, "custom_xflickauthkey")
    if auth_key:
        return {"X-Flick-Auth-Key": auth_key}

    from uae_erpgulf.uae_erpgulf.verify_token import get_valid_flick_token

    token = get_valid_flick_token(company)
    if not token:
        frappe.throw(_("Both X-Flick Auth Key and Access Token are missing in Company"))
    return {"Authorization": f"Bearer {token}"}


def check_company_permission(company: str, ptype: str = "write") -> None:
    """Integration settings and credentials are Company configuration: require Company access."""
    if not company:
        frappe.throw(_("Company is required"))
    frappe.has_permission("Company", ptype, doc=company, throw=True)


def check_doc_permission(doctype: str, name: str, ptype: str = "read") -> None:
    """Endpoints taking a document name must enforce that document's permissions themselves."""
    if not name:
        frappe.throw(_("{0} is required").format(doctype))
    frappe.has_permission(doctype, ptype, doc=name, throw=True)
