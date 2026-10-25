import frappe
import json
from frappe import _


@frappe.whitelist()
def get_fta_incoming_invoices():
    """Fetch UAE Incoming Invoices with status Not Submitted"""
    fta_invoices = frappe.get_list(
        'UAE Incoming Invoices',
        filters={'status': 'Not Submitted'},
        fields=['name', 'document_id', 'incoming_invoice_file']
    )
    return fta_invoices


@frappe.whitelist()
def create_purchase_invoice_from_fta(docname: str):
    """
    Processes an incoming UAE e-invoice record via the P2P engine
    and creates a linked Draft Purchase Invoice with PO matching and validation.
    """
    try:
        from uae_erpgulf.core.inbound_processor import process_inbound_record
        result = process_inbound_record(docname)
        return result
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "create_purchase_invoice_from_fta")
        return {
            "error": True,
            "message": str(e)
        }
