"""
UAE E-Invoice to VAT 201 Reconciliation Report.
Cross-references submitted Sales Invoices in ERPNext General Ledger
against official FTA E-Invoicing clearance statuses, UUIDs, and Emirate Box 1 breakdowns.
"""

import frappe
from frappe import _
from typing import Dict, Any, List, Tuple, Optional


def execute(filters: Optional[Dict[str, Any]] = None) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str, Dict[str, Any], List[Dict[str, Any]]]:
    if not filters:
        filters = {}

    columns = get_columns()
    data = get_data(filters)
    chart = get_chart_data(data)
    report_summary = get_report_summary(data)

    return columns, data, None, chart, report_summary


def get_columns() -> List[Dict[str, Any]]:
    return [
        {
            "label": _("Invoice"),
            "fieldname": "name",
            "fieldtype": "Link",
            "options": "Sales Invoice",
            "width": 150
        },
        {
            "label": _("Date"),
            "fieldname": "posting_date",
            "fieldtype": "Date",
            "width": 95
        },
        {
            "label": _("Customer"),
            "fieldname": "customer_name",
            "fieldtype": "Data",
            "width": 180
        },
        {
            "label": _("Customer TRN"),
            "fieldname": "tax_id",
            "fieldtype": "Data",
            "width": 130
        },
        {
            "label": _("Emirate (Box 1)"),
            "fieldname": "emirate",
            "fieldtype": "Data",
            "width": 110
        },
        {
            "label": _("Taxable Amount (AED)"),
            "fieldname": "taxable_amount",
            "fieldtype": "Currency",
            "options": "AED",
            "width": 130
        },
        {
            "label": _("Output VAT 5% (AED)"),
            "fieldname": "vat_amount",
            "fieldtype": "Currency",
            "options": "AED",
            "width": 130
        },
        {
            "label": _("Total (AED)"),
            "fieldname": "grand_total",
            "fieldtype": "Currency",
            "options": "AED",
            "width": 130
        },
        {
            "label": _("E-Invoice Status"),
            "fieldname": "einvoice_status",
            "fieldtype": "Data",
            "width": 140
        },
        {
            "label": _("FTA Clearance UUID"),
            "fieldname": "document_id",
            "fieldtype": "Data",
            "width": 180
        },
        {
            "label": _("Reconciliation"),
            "fieldname": "reconciliation_badge",
            "fieldtype": "Data",
            "width": 160
        }
    ]


def get_data(filters: Dict[str, Any]) -> List[Dict[str, Any]]:
    company = filters.get("company")
    from_date = filters.get("from_date")
    to_date = filters.get("to_date")
    emirate_filter = filters.get("emirate")
    status_filter = filters.get("status")

    conditions = ["s.docstatus = 1", "s.company = %(company)s"]
    params = {"company": company, "from_date": from_date, "to_date": to_date}

    if from_date:
        conditions.append("s.posting_date >= %(from_date)s")
    if to_date:
        conditions.append("s.posting_date <= %(to_date)s")

    # Has columns check
    has_status_col = frappe.db.has_column("Sales Invoice", "custom_uae_einvoice_status")
    has_uuid_col = frappe.db.has_column("Sales Invoice", "custom_document_id")

    status_select = "s.custom_uae_einvoice_status" if has_status_col else "''"
    uuid_select = "s.custom_document_id" if has_uuid_col else "''"

    query = f"""
        SELECT
            s.name,
            s.posting_date,
            s.customer,
            s.customer_name,
            COALESCE(s.tax_id, c.tax_id, '') AS tax_id,
            COALESCE(NULLIF(TRIM(s.vat_emirate), ''), c.custom_emirates, 'Dubai') AS emirate,
            CASE
                WHEN s.is_return = 1 THEN -ABS(s.base_net_total)
                ELSE s.base_net_total
            END AS taxable_amount,
            CASE
                WHEN s.is_return = 1 THEN -ABS(s.base_total_taxes_and_charges)
                ELSE s.base_total_taxes_and_charges
            END AS vat_amount,
            CASE
                WHEN s.is_return = 1 THEN -ABS(s.base_grand_total)
                ELSE s.base_grand_total
            END AS grand_total,
            COALESCE({status_select}, '') AS einvoice_status,
            COALESCE({uuid_select}, '') AS document_id
        FROM `tabSales Invoice` s
        LEFT JOIN `tabCustomer` c ON c.name = s.customer
        WHERE {" AND ".join(conditions)}
        ORDER BY s.posting_date DESC, s.name DESC
    """

    rows = frappe.db.sql(query, params, as_dict=True)
    filtered_rows = []

    for r in rows:
        r_emirate = r.get("emirate")
        r_status = r.get("einvoice_status") or ""
        r_doc_id = r.get("document_id")

        if emirate_filter and r_emirate != emirate_filter:
            continue
        if status_filter and r_status != status_filter:
            continue

        # Determine reconciliation badge
        status = r_status.upper()
        if status in ["CLEARED", "REPORTED"] or r_doc_id:
            r["reconciliation_badge"] = '<span style="color:#28a745; font-weight:700;">✔ FTA Cleared</span>'
        elif status == "PRE-MANDATE / HISTORICAL":
            r["reconciliation_badge"] = '<span style="color:#6c757d; font-weight:600;">ℹ Pre-Mandate</span>'
        elif status in ["PENDING SUBMISSION", "QUEUED"]:
            r["reconciliation_badge"] = '<span style="color:#fd7e14; font-weight:700;">⏳ Queue Pending</span>'
        elif status in ["FAILED", "REJECTED"]:
            r["reconciliation_badge"] = '<span style="color:#dc3545; font-weight:700;">✘ Transmit Failed</span>'
        else:
            r["reconciliation_badge"] = '<span style="color:#ffc107; font-weight:700;">⚠ Clearance Pending</span>'

        filtered_rows.append(r)

    return filtered_rows


def get_report_summary(data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    total_taxable = sum(r.get("taxable_amount", 0.0) for r in data)
    total_vat = sum(r.get("vat_amount", 0.0) for r in data)
    cleared_count = sum(1 for r in data if "✔ FTA Cleared" in r.get("reconciliation_badge", ""))
    pending_count = len(data) - cleared_count

    return [
        {"value": f"AED {total_taxable:,.2f}", "label": _("Box 1 Taxable Sales"), "datatype": "Currency"},
        {"value": f"AED {total_vat:,.2f}", "label": _("Box 1 Output VAT (5%)"), "datatype": "Currency"},
        {"value": cleared_count, "label": _("Cleared E-Invoices"), "datatype": "Int", "indicator": "green"},
        {"value": pending_count, "label": _("Pending / Pre-Mandate"), "datatype": "Int", "indicator": "orange" if pending_count else "blue"}
    ]


def get_chart_data(data: List[Dict[str, Any]]) -> Dict[str, Any]:
    emirate_totals: Dict[str, float] = {}
    for r in data:
        em = r.get("emirate") or "Dubai"
        emirate_totals[em] = emirate_totals.get(em, 0.0) + float(r.get("taxable_amount", 0.0))

    labels = list(emirate_totals.keys())
    values = [emirate_totals[l] for l in labels]

    return {
        "data": {
            "labels": labels,
            "datasets": [{"name": _("Taxable Sales by Emirate"), "values": values}]
        },
        "type": "bar",
        "colors": ["#0284c7"]
    }
