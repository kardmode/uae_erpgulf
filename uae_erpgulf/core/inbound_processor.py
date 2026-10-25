"""
UAE Inbound E-Invoicing & Automated Procure-to-Pay (P2P) Engine.
Implements 15-point automated matching, PO tolerance checking,
supplier resolution, item hierarchical matching, and safe draft creation.
"""

import frappe
from frappe import _
import json
import re
import xml.etree.ElementTree as ET
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Any, List, Optional, Tuple
from uae_erpgulf.core.constants import CREDIT_NOTE_TYPES


class InboundProcessingError(frappe.ValidationError):
    pass


class DuplicateInboundInvoiceError(frappe.DuplicateEntryError):
    pass


# A PO reference matched against the end of PO names must have at least this many characters
MIN_PO_SUFFIX_LENGTH = 4
# Allowed gap between the supplier's totals and the draft ERPNext computes
TOTALS_TOLERANCE = 0.05


def normalize_po_reference(po_raw: Optional[str]) -> str:
    """
    Normalizes PO reference string by removing prefixes (PO#, PO, Purchase Order, #),
    spaces, dashes, and symbols.
    Example: 'PO# 00451' -> '00451', 'po-2026-00451' -> '202600451'.
    """
    if not po_raw:
        return ""
    cleaned = re.sub(r"(?i)^(purchase\s*order|po#|po|order|#)\s*[-:]?\s*", "", str(po_raw).strip())
    # Strip symbols
    cleaned = re.sub(r"[\s\-_#:/]", "", cleaned)
    return cleaned.lower()



def parse_inbound_payload(content: Any) -> Dict[str, Any]:
    """
    Parses inbound payload from either dict, JSON (Flick format), or standard PEPPOL UBL 2.1 XML.
    Returns normalized dictionary adhering to Canonical UAE Model.
    """
    if isinstance(content, dict):
        return _parse_json_inbound(content)

    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="ignore")

    content_clean = str(content).strip()

    if content_clean.startswith("{") or content_clean.startswith("["):
        return _parse_json_inbound(json.loads(content_clean))
    elif content_clean.startswith("<"):
        return _parse_xml_inbound(content_clean)
    else:
        raise InboundProcessingError(_("Unrecognized invoice file format. Expected JSON or UBL 2.1 XML."))



def _parse_json_inbound(data: Dict[str, Any]) -> Dict[str, Any]:
    """Parses Flick JSON schema format."""
    doc = data.get("document", data)

    # In an incoming invoice, the SELLER is the issuing_party!
    issuing_party = doc.get("issuing_party", {})
    receiving_party = doc.get("receiving_party", {})
    totals = doc.get("legal_monetary_total", {})

    lines = []
    for idx, l in enumerate(doc.get("invoice_lines", []), 1):
        lines.append({
            "idx": idx,
            "seller_item_id": l.get("hs_code") or l.get("commodity_code") or l.get("name"),
            "barcode": l.get("sac_code"),
            "name": l.get("name") or l.get("description"),
            "description": l.get("description") or l.get("name"),
            "quantity": float(l.get("invoiced_quantity") or 1.0),
            "uom": l.get("uom") or "NOS",
            "rate": float(l.get("unit_price") or 0.0),
            "amount": float(l.get("line_extension_amount") or (float(l.get("invoiced_quantity") or 1.0) * float(l.get("unit_price") or 0.0))),
            "vat_category": l.get("vat_category") or "S",
            "vat_rate": float(5.0 if l.get("vat_percentage") is None else l["vat_percentage"]),
        })

    # PO Reference from order_reference, po_reference, or buyer_reference
    po_ref = doc.get("order_reference") or doc.get("po_reference") or doc.get("buyer_reference")
    if isinstance(po_ref, dict):
        po_ref = po_ref.get("id")

    return {
        "document_type": str(doc.get("invoice_type_code", "380")),
        "document_identifier": doc.get("document_identifier"),
        "issue_date": doc.get("issue_date"),
        "due_date": doc.get("due_date"),
        "currency": doc.get("document_currency", "AED"),
        "exchange_rate": doc.get("currency_exchange_rate"),
        "is_return": str(doc.get("invoice_type_code")) in CREDIT_NOTE_TYPES + ("361",),
        "po_reference": po_ref,
        "seller": {
            "legal_name": issuing_party.get("legal_name") or issuing_party.get("trade_name"),
            "tax_id": issuing_party.get("vat_number") or issuing_party.get("tax_id"),
            "trade_license": issuing_party.get("legal_identifier"),
            "peppol_id": issuing_party.get("peppol_id"),
        },
        "buyer": {
            "legal_name": receiving_party.get("legal_name"),
            "tax_id": receiving_party.get("vat_number"),
        },
        "lines": lines,
        "monetary_totals": {
            "tax_exclusive_amount": float(totals.get("tax_exclusive_amount") or 0.0),
            "tax_inclusive_amount": float(totals.get("tax_inclusive_amount") or 0.0),
            "payable_amount": float(totals.get("payable_amount") or totals.get("tax_inclusive_amount") or 0.0),
            "allowance_total": float(totals.get("allowance_total_amount") or 0.0),
        }
    }


UBL_NS = {
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
}


def _ubl_document(root):
    """The Invoice / CreditNote element, unwrapping a Peppol StandardBusinessDocument envelope."""
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag in ("Invoice", "CreditNote"):
            return element
    return root


def _amount(node, path: str, default: float = 0.0) -> float:
    text = node.findtext(path, default="", namespaces=UBL_NS)
    return float(text) if text not in (None, "") else default


def _parse_party(node) -> Dict[str, Any]:
    if node is None:
        return {}
    endpoint = node.find("cbc:EndpointID", UBL_NS)
    peppol_id = None
    if endpoint is not None and endpoint.text:
        scheme = endpoint.get("schemeID")
        peppol_id = f"{scheme}:{endpoint.text.strip()}" if scheme else endpoint.text.strip()
    return {
        "legal_name": node.findtext("cac:PartyLegalEntity/cbc:RegistrationName", default="", namespaces=UBL_NS)
        or node.findtext("cac:PartyName/cbc:Name", default="", namespaces=UBL_NS),
        "tax_id": node.findtext("cac:PartyTaxScheme/cbc:CompanyID", default="", namespaces=UBL_NS),
        "trade_license": node.findtext("cac:PartyLegalEntity/cbc:CompanyID", default="", namespaces=UBL_NS),
        "peppol_id": peppol_id,
    }


def _parse_xml_inbound(xml_text: str) -> Dict[str, Any]:
    """Parses a PEPPOL UBL 2.1 / PINT-AE Invoice or CreditNote (optionally inside an SBD envelope)."""
    root = _ubl_document(ET.fromstring(xml_text))
    is_credit_note = root.tag.endswith("CreditNote")
    ns = UBL_NS

    doc_type = (
        root.findtext("cbc:CreditNoteTypeCode", default="", namespaces=ns)
        or root.findtext("cbc:InvoiceTypeCode", default="", namespaces=ns)
        or ("381" if is_credit_note else "380")
    )

    lines = []
    line_tag, qty_tag = ("cac:CreditNoteLine", "cbc:CreditedQuantity") if is_credit_note else ("cac:InvoiceLine", "cbc:InvoicedQuantity")
    for idx, l in enumerate(root.findall(line_tag, ns), 1):
        name = l.findtext("cac:Item/cbc:Name", default="Item", namespaces=ns)
        qty_node = l.find(qty_tag, ns)
        qty = float(qty_node.text) if qty_node is not None and qty_node.text else 1.0
        amount = _amount(l, "cbc:LineExtensionAmount")
        base_qty = _amount(l, "cac:Price/cbc:BaseQuantity", 1.0) or 1.0
        price_text = l.findtext("cac:Price/cbc:PriceAmount", default="", namespaces=ns)
        unit_price = (float(price_text) / base_qty) if price_text else (amount / qty if qty else 0.0)
        lines.append({
            "idx": idx,
            "seller_item_id": l.findtext("cac:Item/cac:SellersItemIdentification/cbc:ID", default="", namespaces=ns),
            "barcode": l.findtext("cac:Item/cac:StandardItemIdentification/cbc:ID", default="", namespaces=ns),
            "name": name,
            "description": l.findtext("cac:Item/cbc:Description", default="", namespaces=ns) or name,
            "quantity": qty,
            "unit_code": qty_node.get("unitCode") if qty_node is not None else None,
            "uom": None,
            "rate": unit_price,
            "amount": amount,
            "vat_category": l.findtext("cac:Item/cac:ClassifiedTaxCategory/cbc:ID", default="S", namespaces=ns),
            "vat_rate": _amount(l, "cac:Item/cac:ClassifiedTaxCategory/cbc:Percent", 0.0),
        })

    currency = root.findtext("cbc:DocumentCurrencyCode", default="AED", namespaces=ns)
    # The document-currency TaxTotal (a second one in AED may follow on foreign-currency invoices)
    tax_total = 0.0
    for total in root.findall("cac:TaxTotal", ns):
        amount_node = total.find("cbc:TaxAmount", ns)
        if amount_node is not None and amount_node.get("currencyID", currency) == currency:
            tax_total = float(amount_node.text or 0)
            break

    return {
        "document_type": doc_type,
        "document_identifier": root.findtext("cbc:ID", default="", namespaces=ns),
        "issue_date": root.findtext("cbc:IssueDate", default="", namespaces=ns),
        "due_date": root.findtext("cbc:DueDate", default="", namespaces=ns)
        or root.findtext("cac:PaymentMeans/cbc:PaymentDueDate", default="", namespaces=ns),
        "currency": currency,
        "exchange_rate": None,
        "is_return": is_credit_note or doc_type in CREDIT_NOTE_TYPES + ("361",),
        "po_reference": root.findtext("cac:OrderReference/cbc:ID", default="", namespaces=ns)
        or root.findtext("cbc:BuyerReference", default="", namespaces=ns),
        "billing_reference": root.findtext("cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID", default="", namespaces=ns),
        "seller": _parse_party(root.find("cac:AccountingSupplierParty/cac:Party", ns)),
        "buyer": _parse_party(root.find("cac:AccountingCustomerParty/cac:Party", ns)),
        "lines": lines,
        "monetary_totals": {
            "tax_exclusive_amount": _amount(root, "cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount"),
            "tax_amount": tax_total,
            "tax_inclusive_amount": _amount(root, "cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount"),
            "payable_amount": _amount(root, "cac:LegalMonetaryTotal/cbc:PayableAmount"),
        },
    }


def resolve_supplier(seller_info: Dict[str, Any]) -> str:
    """
    Resolves ERPNext Supplier by:
    1. 15-digit VAT TRN (tax_id)
    2. PEPPOL ID
    3. Trade License Number
    4. Exact or fuzzy Supplier Name
    """
    trn = (seller_info.get("tax_id") or "").strip()
    if trn:
        supplier = frappe.db.get_value("Supplier", {"tax_id": trn}, "name")
        if supplier:
            return supplier

    peppol_id = (seller_info.get("peppol_id") or "").strip()
    if peppol_id and frappe.db.has_column("Supplier", "custom_peppol_id"):
        supplier = frappe.db.get_value("Supplier", {"custom_peppol_id": peppol_id}, "name")
        if supplier:
            return supplier

    trade_license = (seller_info.get("trade_license") or "").strip()
    if trade_license and frappe.db.has_column("Supplier", "custom_trade_license_number"):
        supplier = frappe.db.get_value("Supplier", {"custom_trade_license_number": trade_license}, "name")
        if supplier:
            return supplier

    name = (seller_info.get("legal_name") or "").strip()
    if name:
        supplier = frappe.db.get_value("Supplier", {"supplier_name": name}, "name")
        if supplier:
            return supplier

    raise InboundProcessingError(
        _(
            "Supplier not found for incoming invoice. Tax TRN: '{0}', Name: '{1}'. "
            "Please create or update the Supplier in ERPNext before processing."
        ).format(trn, name)
    )


def match_purchase_order(supplier: str, po_raw: Optional[str], company: str) -> Tuple[Optional[Any], Optional[str]]:
    """
    Matches incoming PO reference string against open Purchase Orders for this supplier.
    Verifies that the matched PO is not already 100% fully billed.
    Returns: (matched_po_doc or None, warning_message or None)
    """
    if not po_raw:
        return None, None

    normalized_key = normalize_po_reference(po_raw)
    if not normalized_key:
        return None, None

    # Search POs for this supplier & company
    pos = frappe.get_all(
        "Purchase Order",
        filters={
            "supplier": supplier,
            "company": company,
            "docstatus": 1,
        },
        fields=["name", "status", "per_billed"],
        order_by="creation desc"
    )

    # Exact match on the PO name, or the reference is the PO's trailing number (e.g. "PO# 00451"
    # for PUR-ORD-2026-00451). Short or ambiguous references are never guessed.
    candidates = []
    for po in pos:
        po_name = po.get("name") if isinstance(po, dict) else getattr(po, "name", str(po))
        po_norm = normalize_po_reference(po_name)
        if po_name.lower() == str(po_raw).strip().lower() or po_norm == normalized_key:
            candidates = [po_name]
            break
        if len(normalized_key) >= MIN_PO_SUFFIX_LENGTH and po_norm.endswith(normalized_key):
            candidates.append(po_name)

    if len(candidates) != 1:
        reason = _("matches several Purchase Orders") if candidates else _("was not found among Purchase Orders")
        return None, _("PO Reference '{0}' {1} for {2}; staged without a PO link.").format(po_raw, reason, supplier)
    matched_name = candidates[0]

    po_doc = frappe.get_doc("Purchase Order", matched_name)

    # Edge Case 2: Already fully billed PO
    if po_doc.status in ["Completed", "Closed"] or (po_doc.per_billed and po_doc.per_billed >= 100):
        return None, _("Purchase Order {0} is already 100% billed. Staged without linking to avoid duplicate payment.").format(po_doc.name)

    return po_doc, None


def resolve_line_item(line: Dict[str, Any], supplier: str, company: str) -> Dict[str, Any]:
    """
    Hierarchical item matching:
    1. Item Supplier table (supplier_part_no)
    2. Item Barcode (GTIN)
    3. Item Code / Item Name
    4. Service / Expense fallback
    """
    seller_item_id = (line.get("seller_item_id") or "").strip()
    barcode = (line.get("barcode") or "").strip()
    name = (line.get("name") or "").strip()

    item_code = None

    # Step 1: Check Item Supplier table
    if seller_item_id:
        item_code = frappe.db.get_value(
            "Item Supplier",
            {"supplier": supplier, "supplier_part_no": seller_item_id},
            "parent"
        )

    # Step 2: Check Barcode table
    if not item_code and barcode:
        item_code = frappe.db.get_value("Item Barcode", {"barcode": barcode}, "parent")

    # Step 3: Match exact item code or name
    if not item_code and seller_item_id:
        item_code = frappe.db.get_value("Item", {"item_code": seller_item_id}, "name")

    if not item_code and name:
        item_code = frappe.db.get_value("Item", {"item_name": name}, "name")

    # Step 4: Fallback to Expense Account for services / unknown overheads
    expense_account = None
    if not item_code:
        # Check Supplier default expense account
        supplier_doc = frappe.get_cached_doc("Supplier", supplier)
        if hasattr(supplier_doc, "accounts") and supplier_doc.accounts:
            for acc in supplier_doc.accounts:
                if acc.company == company:
                    expense_account = acc.account
                    break

        if not expense_account:
            # Fallback to Company default Cost of Goods Sold or General Expense
            expense_account = frappe.db.get_value(
                "Account",
                {"account_type": "Cost of Goods Sold", "company": company, "is_group": 0},
                "name"
            ) or frappe.db.get_value(
                "Account",
                {"account_type": "Expense Account", "company": company, "is_group": 0},
                "name"
            )

    return {
        "item_code": item_code,
        "item_name": name or (item_code if item_code else "Service Item"),
        "description": line.get("description") or name,
        "expense_account": expense_account,
        "is_unmapped": (item_code is None and expense_account is None)
    }


def validate_duplication(supplier: str, bill_no: str, document_id: Optional[str]):
    """
    Tri-lock duplication prevention:
    1. Checks if document_id already exists in Purchase Invoice.
    2. Checks if supplier + bill_no combination already exists.
    """
    if document_id and frappe.db.has_column("Purchase Invoice", "custom_document_id"):
        existing = frappe.db.get_value(
            "Purchase Invoice",
            {"custom_document_id": document_id, "docstatus": ["!=", 2]},
            "name"
        )
        if existing:
            raise DuplicateInboundInvoiceError(
                _("An invoice with Document ID '{0}' already exists: {1}.").format(document_id, existing)
            )

    if supplier and bill_no:
        existing_bill = frappe.db.get_value(
            "Purchase Invoice",
            {"supplier": supplier, "bill_no": bill_no, "docstatus": ["!=", 2]},
            "name"
        )
        if existing_bill:
            raise DuplicateInboundInvoiceError(
                _("Supplier Invoice '{0}' for '{1}' has already been processed: {2}.").format(bill_no, supplier, existing_bill)
            )


def resolve_company(buyer_info: Dict[str, Any]) -> str:
    """The receiving Company: the one whose TRN (or TIN) the invoice is addressed to."""
    from uae_erpgulf.core.canonical import tin_from_trn

    trn = (buyer_info.get("tax_id") or "").strip()
    if trn:
        company = frappe.db.get_value("Company", {"tax_id": trn}, "name")
        if company:
            return company

    peppol_tin = str(buyer_info.get("peppol_id") or "").split(":")[-1]
    tin = tin_from_trn(trn) or (peppol_tin if len(peppol_tin) == 10 else None)
    if tin:
        for row in frappe.get_all("Company", filters={"tax_id": ["like", f"{tin}%"]}, fields=["name", "tax_id"]):
            if tin_from_trn(row.tax_id) == tin:
                return row.name

    companies = frappe.get_all("Company", pluck="name")
    if len(companies) == 1:
        return companies[0]
    raise InboundProcessingError(
        _("No Company matches the buyer of this invoice (TRN '{0}'). Set the Company's Tax ID.").format(trn)
    )


@frappe.whitelist()
def process_inbound_record(inbound_record_name: str) -> Dict[str, Any]:
    """
    Main API endpoint: processes a staged UAE Incoming Invoices record and creates a
    Draft Purchase Invoice from it.
    """
    from uae_erpgulf.core.credentials import check_doc_permission

    check_doc_permission("UAE Incoming Invoices", inbound_record_name, "write")
    frappe.has_permission("Purchase Invoice", "create", throw=True)
    staged_doc = frappe.get_doc("UAE Incoming Invoices", inbound_record_name)

    if not staged_doc.incoming_invoice_file:
        frappe.throw(_("No incoming invoice file attached to this record."))

    file_doc = frappe.get_doc("File", {"file_url": staged_doc.incoming_invoice_file})
    payload = parse_inbound_payload(file_doc.get_content())

    result = create_purchase_invoice_from_payload(payload, document_id=staged_doc.document_id)

    staged_doc.db_set("status", "Draft Created")
    if frappe.db.has_column("UAE Incoming Invoices", "purchase_invoice"):
        staged_doc.db_set("purchase_invoice", result["purchase_invoice"])
    staged_doc.db_set("submit_response", f"Purchase Invoice {result['purchase_invoice']} created as Draft.")
    return result


def create_purchase_invoice_from_payload(
    payload: Dict[str, Any], document_id: Optional[str] = None, company: Optional[str] = None
) -> Dict[str, Any]:
    """Creates a Draft Purchase Invoice (or debit note) from a parsed incoming e-invoice."""
    company = company or resolve_company(payload.get("buyer") or {})

    # 1. Supplier Resolution
    supplier = resolve_supplier(payload["seller"])

    # 2. Duplicate Check
    bill_no = payload["document_identifier"]
    doc_id = document_id or bill_no
    validate_duplication(supplier, bill_no, doc_id)

    is_return = bool(payload.get("is_return"))
    warnings = []

    # 3. PO Matching (never on credit notes)
    matched_po, po_warning = (None, None) if is_return else match_purchase_order(supplier, payload.get("po_reference"), company)
    if po_warning:
        warnings.append(po_warning)

    # 4. Accounting defaults
    company_cost_center = frappe.get_cached_value("Company", company, "cost_center") or frappe.db.get_value(
        "Cost Center", {"company": company, "is_group": 0}, "name"
    )
    credit_to = (
        frappe.db.get_value("Party Account", {"parent": supplier, "parenttype": "Supplier", "company": company}, "account")
        or frappe.get_cached_value("Company", company, "default_payable_account")
        or frappe.db.get_value("Account", {"account_type": "Payable", "company": company, "is_group": 0}, "name")
    )

    # Project only from the matched PO or the Company's configured default, never a guess
    project = getattr(matched_po, "project", None) if matched_po else None
    if not project and frappe.db.has_column("Company", "custom_default_inbound_project"):
        project = frappe.get_cached_value("Company", company, "custom_default_inbound_project")
    if not project and "mrp" in frappe.get_installed_apps():
        # The MRP app refuses Purchase Invoice drafts without a project: say how to fix it here
        raise InboundProcessingError(
            _(
                "No project for incoming invoice {0} from {1}: it matches no Purchase Order with a project, "
                "and Company {2} has no Default Inbound E-Invoice Project. Set that field on the Company "
                "(e.g. an 'Unallocated Inbound Invoices' project), then process the invoice again."
            ).format(bill_no, supplier, company)
        )

    pi = frappe.new_doc("Purchase Invoice")
    pi.company = company
    pi.supplier = supplier
    pi.posting_date = payload.get("issue_date") or frappe.utils.today()
    pi.set_posting_time = 1
    if payload.get("due_date") and not is_return:
        pi.due_date = payload["due_date"]
    pi.currency = payload.get("currency") or "AED"
    pi.bill_no = bill_no
    pi.bill_date = payload.get("issue_date")
    pi.is_return = 1 if is_return else 0
    if is_return and payload.get("billing_reference"):
        original = frappe.db.get_value(
            "Purchase Invoice",
            {"supplier": supplier, "bill_no": payload["billing_reference"], "docstatus": 1, "is_return": 0},
            "name",
        )
        if original:
            pi.return_against = original
        else:
            warnings.append(
                _("Credited invoice '{0}' was not found; check the debit note manually.").format(payload["billing_reference"])
            )
    if credit_to:
        pi.credit_to = credit_to
    if project:
        pi.project = project
    if frappe.db.has_column("Purchase Invoice", "custom_document_id"):
        pi.custom_document_id = doc_id
    if frappe.db.has_column("Purchase Invoice", "custom_uae_einvoice_status"):
        pi.custom_uae_einvoice_status = "Inbound Cleared"

    # 5. Lines, with each line's own VAT rate (0 for Z/E/O/AE) against the input VAT account
    vat_account = _input_vat_account(company)
    has_vat = any(float(line.get("vat_rate") or 0) for line in payload["lines"])
    if has_vat and not vat_account:
        warnings.append(_("No input VAT account found for {0}; VAT was not added.").format(company))

    sign = -1 if is_return else 1
    for line in payload["lines"]:
        matched_item = resolve_line_item(line, supplier, company)

        line_uom = line.get("uom") or _uom_for_unit_code(line.get("unit_code"))
        if matched_item["item_code"]:
            stock_uom = frappe.db.get_value("Item", matched_item["item_code"], "stock_uom")
            if stock_uom and line_uom != stock_uom and not frappe.db.exists(
                "UOM Conversion Detail", {"parent": matched_item["item_code"], "uom": line_uom}
            ):
                line_uom = stock_uom

        row = {
            "qty": sign * abs(line["quantity"]),
            "rate": line["rate"],
            "uom": line_uom,
            "item_name": matched_item["item_name"],
            "description": matched_item["description"],
            "cost_center": company_cost_center,
        }
        if project:
            row["project"] = project
        if matched_item["item_code"]:
            row["item_code"] = matched_item["item_code"]
        elif matched_item["expense_account"]:
            row["expense_account"] = matched_item["expense_account"]
        if vat_account and has_vat:
            line_rate = 0.0 if line.get("vat_category") in ("Z", "E", "O", "AE") else float(line.get("vat_rate") or 0)
            row["item_tax_rate"] = frappe.as_json({vat_account: line_rate})

        if matched_po:
            row["purchase_order"] = matched_po.name
            for po_item in matched_po.items:
                if (matched_item["item_code"] and po_item.item_code == matched_item["item_code"]) or po_item.item_name == line["name"]:
                    row["po_detail"] = po_item.name
                    if po_item.rate and abs(line["rate"] - po_item.rate) > po_item.rate * 0.01:
                        warnings.append(
                            f"Rate variance on '{line['name']}': PO rate {po_item.rate} vs invoiced rate {line['rate']}"
                        )
                    remaining = (po_item.qty or 0) - (po_item.get("billed_qty") or 0)
                    if abs(line["quantity"]) > remaining:
                        warnings.append(f"Quantity on '{line['name']}' exceeds what remains to bill on {matched_po.name}")
                    break

        pi.append("items", row)

    if vat_account and has_vat:
        pi.append("taxes", {
            "charge_type": "On Net Total",
            "account_head": vat_account,
            "description": "VAT",
            "rate": max(float(line.get("vat_rate") or 0) for line in payload["lines"]),
            "category": "Total",
            "add_deduct_tax": "Add",
        })

    # 6. Totals check against what the supplier invoiced
    pi.set_missing_values()
    pi.calculate_taxes_and_totals()
    totals = payload.get("monetary_totals") or {}
    for label, expected, actual in (
        ("VAT", totals.get("tax_amount"), pi.total_taxes_and_charges),
        ("total with VAT", totals.get("tax_inclusive_amount"), pi.grand_total),
    ):
        if expected and abs(abs(float(expected)) - abs(float(actual or 0))) > TOTALS_TOLERANCE:
            warnings.append(f"Supplier {label} {expected} differs from the draft's {actual}")

    if warnings:
        pi.remarks = "UAE Inbound Validation Warnings:\n- " + "\n- ".join(warnings)

    pi.flags.ignore_mandatory = True
    pi.insert()

    return {
        "status": "success",
        "purchase_invoice": pi.name,
        "warnings": warnings,
        "message": _("Draft Purchase Invoice {0} created successfully.").format(pi.name),
    }


def _input_vat_account(company: str) -> Optional[str]:
    """The company's input VAT account: from its default purchase tax template, else a Tax account."""
    template = frappe.db.get_value(
        "Purchase Taxes and Charges Template", {"company": company, "is_default": 1, "disabled": 0}, "name"
    )
    if template:
        account = frappe.db.get_value(
            "Purchase Taxes and Charges",
            {"parent": template, "parenttype": "Purchase Taxes and Charges Template"},
            "account_head",
        )
        if account:
            return account
    return frappe.db.get_value("Account", {"company": company, "account_type": "Tax", "is_group": 0}, "name")


def _uom_for_unit_code(unit_code: Optional[str]) -> str:
    """ERPNext UOM for a UN/ECE unit code (the UOM's own code field first, then the common table)."""
    from uae_erpgulf.core.canonical import UN_ECE_UNIT_CODES

    if unit_code:
        uom = frappe.db.get_value("UOM", {"custom_un_ece_unit_code": unit_code}, "name")
        if uom:
            return uom
        for name, code in UN_ECE_UNIT_CODES.items():
            if code == unit_code and frappe.db.exists("UOM", name.title()):
                return name.title()
    return "Nos"


class InboundInvoiceProcessor:
    """Convenience wrapper: parse an incoming e-invoice and create its Draft Purchase Invoice."""

    def __init__(self, company: Optional[str] = None):
        self.company = company

    def process(self, content: Any, content_type: str = "xml"):
        parsed = parse_inbound_payload(content)
        res = create_purchase_invoice_from_payload(parsed, company=self.company)
        return frappe.get_doc("Purchase Invoice", res["purchase_invoice"])
