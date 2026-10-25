"""
UAE E-Invoicing Testing Sandbox & Data Simulator.
Provides one-click demo data seeding (Supplier, Customer, Items, PO)
and interactive inbound/outbound simulation across PEPPOL UBL 2.1 XML and Flick JSON schemas.
"""

import os
from datetime import datetime
import json
from typing import Dict, Any, Optional

import frappe
from frappe import _
from uae_erpgulf.core.inbound_processor import InboundInvoiceProcessor, DuplicateInboundInvoiceError


FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures", "sample_payloads")


def _guard_simulator(company: str) -> None:
    """
    The simulator writes demo TRNs, suppliers, POs and invoices. Restrict it to System Managers
    on development/test sites (or sites that opt in with "uae_einvoice_allow_simulator": 1).
    """
    frappe.only_for("System Manager")
    if not (frappe.conf.developer_mode or frappe.flags.in_test or frappe.conf.get("uae_einvoice_allow_simulator")):
        frappe.throw(_("The UAE e-invoicing simulator is disabled on production sites."), frappe.PermissionError)
    frappe.has_permission("Company", "write", doc=company, throw=True)


@frappe.whitelist()
def seed_demo_data(company: str) -> Dict[str, Any]:
    """
    Seeds essential UAE master data on a test site:
    - Company demo TRN and Inbound Project defaults
    - Supplier with valid 15-digit TRN
    - Stock and Service items
    - An active, submitted Purchase Order ready for 3-way matching
    """
    _guard_simulator(company)
    results = {"created": [], "existing": []}

    comp_doc = frappe.get_doc("Company", company)
    if not comp_doc.tax_id:
        comp_doc.tax_id = "100987654321003"
        comp_doc.custom_trade_license_number = "TL-DEMO-BUYER"
        comp_doc.save(ignore_permissions=True)
        results["created"].append("Company Demo TRN (100987654321003)")

    # 1. Demo Project
    demo_proj_name = f"DEMO-UAE-{company[:5].upper()}"
    if not frappe.db.exists("Project", demo_proj_name):
        proj = frappe.new_doc("Project")
        proj.project_name = demo_proj_name
        proj.company = company
        proj.status = "Open"
        proj.flags.ignore_mandatory = True
        proj.insert(ignore_permissions=True)
        results["created"].append(f"Project: {demo_proj_name}")
    else:
        results["existing"].append(f"Project: {demo_proj_name}")

    if not getattr(comp_doc, "custom_default_inbound_project", None):
        comp_doc.db_set("custom_default_inbound_project", demo_proj_name)

    # 2. Demo Supplier
    supplier_name = "Al Najm Supplies & Equipment LLC"
    supplier_trn = "100234567890003"
    if not frappe.db.exists("Supplier", supplier_name):
        supp = frappe.new_doc("Supplier")
        supp.supplier_name = supplier_name
        supp.supplier_group = "All Supplier Groups" if frappe.db.exists("Supplier Group", "All Supplier Groups") else None
        supp.tax_id = supplier_trn
        supp.custom_trade_license_number = "TL-776655"
        supp.insert(ignore_permissions=True)
        results["created"].append(f"Supplier: {supplier_name} (TRN: {supplier_trn})")
    else:
        results["existing"].append(f"Supplier: {supplier_name}")

    # 3. Demo Stock Item
    item_code = "DEMO-STK-01"
    if not frappe.db.exists("Item", item_code):
        item = frappe.new_doc("Item")
        item.item_code = item_code
        item.item_name = "Heavy Duty Steel Bolts"
        item.item_group = "All Item Groups" if frappe.db.exists("Item Group", "All Item Groups") else None
        item.stock_uom = "Nos"
        item.is_stock_item = 1
        item.standard_rate = 100.0
        # Add item supplier mapping
        item.append("supplier_items", {
            "supplier": supplier_name,
            "supplier_part_no": "DEMO-STK-01"
        })
        item.insert(ignore_permissions=True)
        results["created"].append(f"Item: {item_code} (Stock)")
    else:
        results["existing"].append(f"Item: {item_code}")

    # 4. Demo Purchase Order
    po_name = "PO-2026-DEMO-01"
    if not frappe.db.exists("Purchase Order", po_name):
        try:
            po = frappe.new_doc("Purchase Order")
            po.name = po_name
            po.supplier = supplier_name
            po.company = company
            po.transaction_date = datetime.now().strftime("%Y-%m-%d")
            po.schedule_date = datetime.now().strftime("%Y-%m-%d")
            po.project = demo_proj_name
            po.append("items", {
                "item_code": item_code,
                "qty": 10,
                "rate": 100.0,
                "uom": "Nos",
                "schedule_date": datetime.now().strftime("%Y-%m-%d")
            })
            po.flags.ignore_mandatory = True
            po.insert(ignore_permissions=True)
            po.submit()
            results["created"].append(f"Submitted Purchase Order: {po_name} (10 units @ 100.0 AED)")
        except Exception as e:
            results["existing"].append(f"PO Creation note: {str(e)}")
    else:
        results["existing"].append(f"Purchase Order: {po_name}")

    return {
        "status": "success",
        "message": f"Demo UAE data ready for {company}",
        "details": results
    }


@frappe.whitelist()
def simulate_inbound_invoice(company: str, scenario: str = "standard_po") -> Dict[str, Any]:
    """
    Executes an end-to-end inbound e-invoice simulation.
    Reads realistic sample payloads, executes duplicate checks, 3-way PO matching,
    hierarchical item resolution, VAT tax line construction, and saves a Draft Purchase Invoice.

    Scenarios:
    - 'standard_po': PEPPOL UBL 2.1 XML matching PO-2026-DEMO-01 (10 units).
    - 'service_expense': PEPPOL UBL 2.1 XML courier fee with no PO (general expense fallback).
    - 'flick_json': Flick format JSON invoice for partial PO billing (5 units).
    - 'credit_note': PEPPOL UBL 2.1 Credit Note for returned goods.
    - 'duplicate_guard': Tests duplicate blocking on repeated invoice IDs.
    """
    _guard_simulator(company)
    processor = InboundInvoiceProcessor(company=company)
    timestamp = datetime.now().strftime("%H%M%S")

    if scenario == "standard_po":
        xml_path = os.path.join(FIXTURES_DIR, "sample_peppol_ubl_standard.xml")
        with open(xml_path, "r", encoding="utf-8") as f:
            content = f.read()
        unique_inv_no = f"SIM-UBL-{timestamp}"
        content = content.replace("SIM-UBL-2026-001", unique_inv_no)
        pi = processor.process(content, content_type="xml")
        return {
            "status": "success",
            "scenario": scenario,
            "invoice_name": pi.name,
            "bill_no": pi.bill_no,
            "grand_total": pi.grand_total,
            "currency": pi.currency,
            "po_matched": pi.items[0].purchase_order if pi.items else None,
            "items_count": len(pi.items),
            "remarks": pi.remarks,
            "message": f"Draft Purchase Invoice {pi.name} created and matched to PO."
        }

    elif scenario == "service_expense":
        xml_path = os.path.join(FIXTURES_DIR, "sample_peppol_ubl_service.xml")
        with open(xml_path, "r", encoding="utf-8") as f:
            content = f.read()
        unique_inv_no = f"SIM-SRV-{timestamp}"
        content = content.replace("SIM-SRV-2026-002", unique_inv_no)
        pi = processor.process(content, content_type="xml")
        return {
            "status": "success",
            "scenario": scenario,
            "invoice_name": pi.name,
            "bill_no": pi.bill_no,
            "grand_total": pi.grand_total,
            "expense_account": pi.items[0].expense_account if pi.items else None,
            "message": f"Draft Service Invoice {pi.name} created with Expense routing."
        }

    elif scenario == "flick_json":
        json_path = os.path.join(FIXTURES_DIR, "sample_flick_inbound.json")
        with open(json_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        unique_inv_no = f"SIM-FLK-{timestamp}"
        payload["data"]["document_identifier"] = unique_inv_no
        payload["data"]["id"] = f"doc-{timestamp}"
        pi = processor.process(payload, content_type="json")
        return {
            "status": "success",
            "scenario": scenario,
            "invoice_name": pi.name,
            "bill_no": pi.bill_no,
            "grand_total": pi.grand_total,
            "po_matched": pi.items[0].purchase_order if pi.items else None,
            "message": f"Draft Flick JSON Invoice {pi.name} created successfully."
        }

    elif scenario == "credit_note":
        xml_path = os.path.join(FIXTURES_DIR, "sample_peppol_ubl_credit_note.xml")
        with open(xml_path, "r", encoding="utf-8") as f:
            content = f.read()
        unique_cr_no = f"SIM-CR-{timestamp}"
        content = content.replace("SIM-CR-2026-003", unique_cr_no)
        pi = processor.process(content, content_type="xml")
        return {
            "status": "success",
            "scenario": scenario,
            "invoice_name": pi.name,
            "bill_no": pi.bill_no,
            "is_return": getattr(pi, "is_return", 0),
            "grand_total": pi.grand_total,
            "message": f"Draft Purchase Credit Note {pi.name} created successfully."
        }

    elif scenario == "duplicate_guard":
        xml_path = os.path.join(FIXTURES_DIR, "sample_peppol_ubl_standard.xml")
        with open(xml_path, "r", encoding="utf-8") as f:
            content = f.read()
        dup_inv_no = f"SIM-DUP-{timestamp}"
        content = content.replace("SIM-UBL-2026-001", dup_inv_no)

        # 1st time: must succeed
        pi1 = processor.process(content, content_type="xml")

        # 2nd time: must be blocked by duplicate guard
        try:
            processor.process(content, content_type="xml")
            return {
                "status": "fail",
                "message": "Duplicate invoice was NOT blocked! Check duplicate guard."
            }
        except DuplicateInboundInvoiceError as e:
            return {
                "status": "success",
                "scenario": scenario,
                "first_invoice": pi1.name,
                "blocked_message": str(e),
                "message": f"Duplicate guard verified: second submission of {dup_inv_no} was blocked."
            }

    else:
        frappe.throw(_("Unknown simulation scenario '{0}'. Valid options: standard_po, service_expense, flick_json, credit_note, duplicate_guard").format(scenario))
