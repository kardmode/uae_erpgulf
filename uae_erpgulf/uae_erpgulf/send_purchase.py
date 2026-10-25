import frappe


@frappe.whitelist()
def bulk_send_invoices(invoices: list | str):
    """Bulk send from the Purchase Invoice list: queued through the same background path as a single submission."""
    from uae_erpgulf.uae_erpgulf.queue import bulk_queue_invoices

    return bulk_queue_invoices("Purchase Invoice", invoices)


@frappe.whitelist()
def get_document_status(invoice_name: str):
    """Kept for existing callers: the status now comes from the Company's configured provider."""
    from uae_erpgulf.uae_erpgulf.queue import refresh_einvoice_status

    return refresh_einvoice_status("Purchase Invoice", invoice_name)
