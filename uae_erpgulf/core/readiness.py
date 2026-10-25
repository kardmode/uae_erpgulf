"""
UAE E-Invoicing & P2P Company Readiness Diagnostic Tool.
Audits Company configuration, accounting defaults, MRP controller compatibility,
ASP credentials, and master data consistency.
"""

import frappe
from frappe import _
import re
from typing import Dict, Any, List


@frappe.whitelist()
def check_company_readiness(company: str) -> Dict[str, Any]:
    """
    Performs comprehensive pre-flight compliance & integration diagnostic
    for a given Company. Returns structured checklist with PASS, WARN, FAIL.
    """
    from uae_erpgulf.core.credentials import check_company_permission

    check_company_permission(company, "read")
    doc = frappe.get_doc("Company", company)
    checks: List[Dict[str, Any]] = []

    def add_check(category: str, item: str, status: str, message: str, action: str = ""):
        checks.append({
            "category": category,
            "item": item,
            "status": status,  # "PASS", "WARN", "FAIL"
            "message": message,
            "action": action
        })

    # =========================================================================
    # 1. Legal Entity & FTA Identity Check
    # =========================================================================
    trn = (getattr(doc, "tax_id", None) or "").strip()
    if not trn:
        add_check("FTA Identity", "VAT Registration Number (TRN)", "FAIL",
                  "Tax ID (TRN) is missing in Company.",
                  "Enter 15-digit TRN in Company > Tax ID.")
    elif not (len(trn) == 15 and trn.isdigit()):
        add_check("FTA Identity", "VAT Registration Number (TRN)", "FAIL",
                  f"Invalid UAE TRN format: '{trn}'. Must be exactly 15 digits.",
                  "Correct Tax ID in Company settings.")
    else:
        add_check("FTA Identity", "VAT Registration Number (TRN)", "PASS",
                  f"Valid 15-digit UAE TRN: {trn}")

    trade_license = getattr(doc, "custom_trade_license_number", None)
    if not trade_license:
        add_check("FTA Identity", "Trade License Number", "WARN",
                  "Trade License Number is missing. Mandatory for B2B e-invoicing clearance.",
                  "Enter the Legal Registration Number in Company > UAE E-invoicing Setting.")
    else:
        add_check("FTA Identity", "Trade License Number", "PASS", f"Trade License: {trade_license}")

    # The seller address on every e-invoice comes from the Company's Address (as the invoice
    # builder reads it), so check that address rather than a separate emirate field.
    address = _company_address(company)
    emirate = (address.get("emirate") or address.get("state")) if address else None
    missing = [label for label, value in (
        ("Address Line 1", address.get("address_line1") if address else None),
        ("City", address.get("city") if address else None),
        ("Emirate", emirate),
    ) if not value]
    if missing:
        add_check("FTA Identity", "Company Address", "WARN",
                  f"Company address is missing: {', '.join(missing)}. Invoices cannot be sent without it.",
                  "Add a primary Address linked to the Company, with its Emirate set.")
    else:
        add_check("FTA Identity", "Company Address", "PASS", f"Emirate: {emirate}")

    # =========================================================================
    # 2. Accredited Service Provider (ASP) Connectivity Check
    # =========================================================================
    from uae_erpgulf.adapters.factory import ASPFactory

    provider = getattr(doc, "custom_accredited_service_providers", "") or ""
    if not provider:
        add_check("ASP Setup", "Selected Provider", "FAIL", "No Accredited Service Provider selected.",
                  "Select your ASP on the Company.")
    elif not ASPFactory.adapter_class(provider):
        add_check("ASP Setup", "Selected Provider", "FAIL",
                  f"{provider} has no integration in this app; invoices cannot be transmitted.",
                  "Select Flick, Wafeq, Complyance or TronStride, or add an adapter for this provider.")
    else:
        add_check("ASP Setup", "Selected Provider", "PASS", f"Configured ASP: {provider}")

    if provider == "Wafeq":
        # Wafeq identifies the seller by its taxpayer and grant, not a participant ID
        for fieldname, label, prefix in (
            ("custom_wafeq_taxpayer_id", "Wafeq Taxpayer ID", "txpay_"),
            ("custom_wafeq_grant_id", "Wafeq Grant ID", "tgr_"),
        ):
            value = getattr(doc, fieldname, None)
            if not value or not str(value).startswith(prefix):
                add_check("ASP Setup", label, "FAIL", f"{label} ({prefix}...) is missing or invalid.",
                          "Copy it from the Wafeq portal (or GET /grants) into the Company.")
            else:
                add_check("ASP Setup", label, "PASS", f"{label}: {value}")
    else:
        # UAE Peppol participant ID = 0235 + the 10-digit TIN (first 10 digits of the TRN)
        participant_id = getattr(doc, "custom_participant_id", None)
        if not participant_id:
            add_check("ASP Setup", "PEPPOL Participant ID", "FAIL",
                      "Participant ID missing. Needed for 5-corner PEPPOL addressing.",
                      "Enter Participant ID (e.g. '0235:1002345678').")
        elif not re.match(r"^0235:\d{10}$", str(participant_id).strip()):
            add_check("ASP Setup", "PEPPOL Participant ID", "WARN",
                      f"Participant ID '{participant_id}' does not match the '0235:<10-digit TIN>' format.",
                      "Verify Participant ID with your ASP.")
        else:
            add_check("ASP Setup", "PEPPOL Participant ID", "PASS", f"PEPPOL ID: {participant_id}")

    # Connectivity test
    try:
        from uae_erpgulf.adapters.factory import ASPFactory
        adapter = ASPFactory.get_adapter(company)
        conn_ok, conn_msg = adapter.test_connection()
        if conn_ok:
            add_check("ASP Setup", "API Handshake & Authentication", "PASS", conn_msg)
        else:
            add_check("ASP Setup", "API Handshake & Authentication", "FAIL", conn_msg,
                      "Check API Key / Base URL in Company.")
    except Exception as e:
        add_check("ASP Setup", "API Handshake & Authentication", "FAIL", str(e))

    # =========================================================================
    # 3. Procure-to-Pay (P2P) Accounting & MRP Defaults Check
    # =========================================================================
    # Payable Account
    payable_acc = getattr(doc, "default_payable_account", None)
    if not payable_acc:
        # Check if any payable account exists
        payable_exists = frappe.db.get_value("Account", {"account_type": "Payable", "company": company, "is_group": 0}, "name")
        if payable_exists:
            add_check("P2P Accounting", "Default Payable Account", "WARN",
                      f"Default Payable Account not explicitly set on Company. System will fall back to '{payable_exists}'.",
                      "Set Default Payable Account in Company > Accounts Settings.")
        else:
            add_check("P2P Accounting", "Default Payable Account", "FAIL",
                      "No Payable Account found for Company. Inbound Purchase Invoice creation will fail.",
                      "Create or link a Payable Account.")
    else:
        add_check("P2P Accounting", "Default Payable Account", "PASS", f"Payable Account: {payable_acc}")

    # Expense Account
    expense_acc = getattr(doc, "default_expense_account", None)
    if not expense_acc:
        expense_exists = frappe.db.get_value("Account", {"account_type": ["in", ["Cost of Goods Sold", "Expense Account"]], "company": company, "is_group": 0}, "name")
        if expense_exists:
            add_check("P2P Accounting", "Default Expense Account", "WARN",
                      f"Default Expense Account not set on Company. System will fall back to '{expense_exists}'.",
                      "Set Default Expense Account in Company.")
        else:
            add_check("P2P Accounting", "Default Expense Account", "FAIL",
                      "No Expense Account found for Company. Inbound service invoices cannot be processed.",
                      "Set Default Expense Account in Company.")
    else:
        add_check("P2P Accounting", "Default Expense Account", "PASS", f"Expense Account: {expense_acc}")

    # Cost Center
    cost_center = getattr(doc, "cost_center", None)
    if not cost_center:
        cc_exists = frappe.db.get_value("Cost Center", {"company": company, "is_group": 0}, "name")
        if cc_exists:
            add_check("P2P Accounting", "Default Cost Center", "WARN",
                      f"Default Cost Center not set on Company. System will fall back to '{cc_exists}'.",
                      "Set Cost Center in Company.")
        else:
            add_check("P2P Accounting", "Default Cost Center", "FAIL",
                      "No Cost Center found for Company. Line item booking will fail ERPNext validation.",
                      "Create a Cost Center for this company.")
    else:
        add_check("P2P Accounting", "Default Cost Center", "PASS", f"Cost Center: {cost_center}")

    # MRP Project Allocation Check
    default_proj = getattr(doc, "custom_default_inbound_project", None)
    open_projects = frappe.get_all("Project", filters={"company": company, "status": "Open"}, limit=1, pluck="name")
    if default_proj:
        add_check("MRP Compatibility", "Mandatory Project Policy", "PASS",
                  f"Default Inbound Project configured: {default_proj}")
    elif open_projects:
        add_check("MRP Compatibility", "Mandatory Project Policy", "WARN",
                  f"No explicit Default Inbound Project set. System will fall back to active project '{open_projects[0]}'.",
                  "Configure 'Default Inbound E-Invoice Project' in Company.")
    else:
        add_check("MRP Compatibility", "Mandatory Project Policy", "FAIL",
                  "No open Projects exist for this company. MRP validate_accounts_controller_mrp will block all Draft Purchase Invoices.",
                  "Create at least one active Project for this company.")

    # Standard VAT 5% Account Check
    vat_account = frappe.db.get_value("Account", {"account_name": ["like", "%VAT 5%%"], "company": company}, "name")
    if not vat_account:
        vat_account = frappe.db.get_value("Account", {"account_type": "Tax", "company": company, "is_group": 0}, "name")
    if vat_account:
        add_check("Tax Accounts", "UAE VAT 5% Account Head", "PASS", f"VAT Tax Account: {vat_account}")
    else:
        add_check("Tax Accounts", "UAE VAT 5% Account Head", "FAIL",
                  "No VAT 5% or Tax Account found for Company. Invoices with VAT cannot be calculated.",
                  "Create 'VAT 5%' under Current Liabilities > Duties and Taxes.")

    # Calculate overall readiness score
    total_checks = len(checks)
    failures = sum(1 for c in checks if c["status"] == "FAIL")
    warnings = sum(1 for c in checks if c["status"] == "WARN")
    passes = sum(1 for c in checks if c["status"] == "PASS")

    overall_status = "READY" if failures == 0 else "ACTION_REQUIRED"

    return {
        "company": company,
        "overall_status": overall_status,
        "score_summary": {
            "total": total_checks,
            "pass": passes,
            "warn": warnings,
            "fail": failures
        },
        "checks": checks
    }


def _company_address(company: str):
    """The Company's primary (else any) linked Address, as a dict, or None."""
    names = frappe.get_all(
        "Dynamic Link",
        filters={"link_doctype": "Company", "link_name": company, "parenttype": "Address"},
        pluck="parent",
    )
    if not isinstance(names, list) or not names:
        return None
    rows = frappe.get_all(
        "Address",
        filters={"name": ["in", names]},
        fields=["address_line1", "city", "emirate", "state", "is_primary_address"],
        order_by="is_primary_address desc",
        limit=1,
    )
    return rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
