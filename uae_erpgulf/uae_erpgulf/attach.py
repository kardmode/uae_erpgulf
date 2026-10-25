import frappe
from frappe.utils.file_manager import save_file


def save_einvoice_artifacts(doc, artifacts: dict):
    """
    Attaches the cleared XML / PDF returned by any ASP adapter's get_document_artifacts()
    and records their URLs, replacing earlier versions.
    """
    for kind, fieldname in (("xml", "custom_document_xml"), ("pdf", "custom_document_pdf")):
        content = (artifacts or {}).get(kind)
        if not content:
            continue

        previous = doc.get(fieldname)
        if previous:
            for f in frappe.get_all("File", filters={"file_url": previous}, pluck="name"):
                frappe.delete_doc("File", f, force=1, ignore_permissions=True)

        label = "XML" if kind == "xml" else "PDF"
        file_doc = save_file(
            fname=f"Submitted-{label}-file {doc.name}.{kind}",
            content=content,
            dt=doc.doctype,
            dn=doc.name,
            is_private=1
        )
        doc.db_set(fieldname, file_doc.file_url)
