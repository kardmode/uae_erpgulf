import frappe


@frappe.whitelist()
def bulk_send_invoices(invoices: list | str):
    """Bulk send from the Sales Invoice list: queued through the same background path as a single submission."""
    from uae_erpgulf.uae_erpgulf.queue import bulk_queue_invoices

    return bulk_queue_invoices("Sales Invoice", invoices)
