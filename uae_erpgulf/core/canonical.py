"""
Canonical UAE Invoice Model (CUIM) Builder
Extracts and normalizes ERPNext Sales and Purchase Invoices into a vendor-agnostic,
UAE FTA PINT-AE / PEPPOL UBL 2.1-compliant intermediate model.

Amounts are taken from ERPNext's own calculation (net_amount after header discount,
total_taxes_and_charges, rounding_adjustment) so the e-invoice always reconciles with
the printed invoice and the GL. Credit notes are emitted with positive amounts, as UBL
requires; ERPNext stores returns as negatives.
"""

import json
import frappe
from frappe import _
from frappe.contacts.doctype.address.address import get_default_address
from frappe.utils import cint, flt, get_time, getdate
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Any, List, Optional
from uae_erpgulf.core.constants import (
    DOC_TYPE_COMMERCIAL_INVOICE,
    DOC_TYPE_CREDIT_NOTE,
    DOC_TYPE_DEBIT_NOTE,
    DOC_TYPE_SELF_BILLED_INVOICE,
    DOC_TYPE_SELF_BILLED_CREDIT_NOTE,
    DOC_TYPE_OUT_OF_SCOPE_INVOICE,
    DOC_TYPE_OUT_OF_SCOPE_CREDIT_NOTE,
    VAT_CATEGORY_STANDARD,
    VAT_CATEGORY_ZERO,
    VAT_CATEGORY_EXEMPT,
    VAT_CATEGORY_REVERSE_CHARGE,
    VAT_CATEGORY_OUT_OF_SCOPE,
    VAT_CATEGORY_MARGIN,
    VAT_CATEGORY_MAP,
    PAYMENT_MEANS_MAP,
    EMIRATE_CODE_MAP,
)

TWO_PLACES = Decimal("0.01")

# Categories that never carry VAT on the invoice itself (reverse charge is accounted by the buyer)
ZERO_VAT_CATEGORIES = (
    VAT_CATEGORY_ZERO,
    VAT_CATEGORY_EXEMPT,
    VAT_CATEGORY_OUT_OF_SCOPE,
    VAT_CATEGORY_REVERSE_CHARGE,
)

# Allowed gap between ERPNext's row-level VAT and the per-category VAT (BR-S-09 rounding)
VAT_RECONCILIATION_TOLERANCE = Decimal("0.01")


def r2(val: Any) -> float:
    """Format and round decimal numbers to 2 decimal places using ROUND_HALF_UP."""
    if val is None:
        return 0.0
    return float(_q2(val))


def _q2(val: Any) -> Decimal:
    return Decimal(str(val or 0)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def resolve_vat_category(label: Optional[str]) -> str:
    """Normalize VAT Category label into UN/ECE 5305 code (S, Z, E, AE, O, N)."""
    if not label:
        return VAT_CATEGORY_STANDARD
    cleaned = str(label).strip().lower()
    return VAT_CATEGORY_MAP.get(cleaned, VAT_CATEGORY_STANDARD)


def resolve_emirate_code(name: Optional[str]) -> Optional[str]:
    """Map UAE Emirate name to PEPPOL ISO subdivision code (e.g. Dubai -> DXB)."""
    if not name:
        return None
    return EMIRATE_CODE_MAP.get(str(name).strip().lower())


# Customer / Supplier "Legal Registration Identifier Type" labels -> PINT-AE codes
LEGAL_REGISTRATION_TYPES = {
    "commercial/trade license": "TL",
    "emirates id": "EID",
    "passport": "PAS",
    "cabinet decision": "CD",
}

# Invoice transaction type flags, in PINT-AE order (position 1..8)
TRANSACTION_TYPE_FLAGS = (
    "free trade zone",
    "deemed supply",
    "margin scheme",
    "summary",
    "continuous",
    "disclosed agent",
    "e-commerce",
    "export",
)

# Common ERPNext UOM names -> UN/ECE Recommendation 20 codes. A UOM's own
# "UN/ECE Unit Code" field always wins over this table.
UN_ECE_UNIT_CODES = {
    "nos": "H87", "unit": "H87", "units": "H87", "piece": "H87", "pcs": "H87", "each": "H87",
    "kg": "KGM", "kilogram": "KGM", "gram": "GRM", "g": "GRM", "tonne": "TNE", "ton": "TNE",
    "litre": "LTR", "liter": "LTR", "l": "LTR", "millilitre": "MLT", "ml": "MLT",
    "meter": "MTR", "metre": "MTR", "m": "MTR", "centimeter": "CMT", "millimeter": "MMT",
    "square meter": "MTK", "sq meter": "MTK", "cubic meter": "MTQ",
    "hour": "HUR", "hours": "HUR", "day": "DAY", "days": "DAY", "week": "WEE", "month": "MON", "year": "ANN",
    "box": "BX", "pair": "PR", "set": "SET", "dozen": "DZN", "pack": "PK", "roll": "RO",
}


def tin_from_trn(trn: Optional[str]) -> Optional[str]:
    """The 10-digit TIN: the first 10 digits of a 15-digit TRN (or a TIN given directly)."""
    digits = "".join(ch for ch in str(trn or "") if ch.isdigit())
    return digits[:10] if len(digits) >= 10 else None


def resolve_transaction_type_code(label: Optional[str]) -> str:
    """The 8-flag transaction type code (e.g. '00000001' for an export) from the invoice's selection."""
    flags = ["0"] * len(TRANSACTION_TYPE_FLAGS)
    text = str(label or "").lower()
    for position, keyword in enumerate(TRANSACTION_TYPE_FLAGS):
        if keyword in text:
            flags[position] = "1"
    return "".join(flags)


def resolve_unit_code(uom: Optional[str]) -> str:
    """UN/ECE unit code for an ERPNext UOM; H87 (piece) when unknown."""
    if not uom:
        return "H87"
    code = frappe.get_cached_value("UOM", uom, "custom_un_ece_unit_code") if frappe.db.exists("UOM", uom) else None
    return (code or UN_ECE_UNIT_CODES.get(str(uom).strip().lower()) or "H87").strip().upper()


def _code_prefix(label: Optional[str]) -> Optional[str]:
    """'DL8.61.1.A-Cancellation' / 'DL8.46.1 - Certain financial services' -> the code part."""
    if not label:
        return None
    return str(label).replace(" - ", "-").split("-")[0].strip() or None


def build_canonical_uae_invoice(doc) -> Dict[str, Any]:
    """
    Main builder: compiles an ERPNext Sales or Purchase Invoice into the Canonical Model.
    It has no side effects, so it is also safe for previews and validation.

    Purchase Invoices are treated as self-billed: the supplier is the seller and the
    company is the buyer.
    """
    is_sales = (doc.doctype == "Sales Invoice")
    company_doc = frappe.get_cached_doc("Company", doc.company)

    # 1. Resolve Document Type Code
    vat_cat_header = resolve_vat_category(doc.get("custom_vat_category"))
    is_return = bool(doc.get("is_return"))
    sign = Decimal(-1) if is_return else Decimal(1)

    if is_sales:
        if is_return and vat_cat_header == VAT_CATEGORY_OUT_OF_SCOPE:
            document_type = DOC_TYPE_OUT_OF_SCOPE_CREDIT_NOTE
        elif is_return:
            document_type = DOC_TYPE_CREDIT_NOTE
        elif vat_cat_header == VAT_CATEGORY_OUT_OF_SCOPE:
            document_type = DOC_TYPE_OUT_OF_SCOPE_INVOICE
        else:
            document_type = DOC_TYPE_COMMERCIAL_INVOICE
    else:
        # Purchase / Self-Billing
        if is_return:
            document_type = DOC_TYPE_SELF_BILLED_CREDIT_NOTE
        else:
            document_type = DOC_TYPE_SELF_BILLED_INVOICE

    # 2. Dates & Times
    posting_date = getdate(doc.posting_date).isoformat() if doc.posting_date else None
    due_date = getdate(doc.due_date).isoformat() if doc.get("due_date") else None
    issue_time = get_time(doc.posting_time).strftime("%H:%M:%S") if doc.get("posting_time") else "00:00:00"

    # Supply Date / Tax Point Date
    tax_point_date = posting_date

    # 4. Currency & Exchange Rate
    currency = (doc.currency or "AED").upper()
    exchange_rate = None
    if currency != "AED":
        rate = doc.get("conversion_rate") or 1.0
        exchange_rate = float(Decimal(str(rate)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))

    # 5. Parties: the company is the seller on sales and the buyer on self-billed purchases
    company_party = _extract_company_party(doc, company_doc)
    counterparty = _extract_counterparty(doc, is_sales)
    seller, buyer = (company_party, counterparty) if is_sales else (counterparty, company_party)
    _validate_seller(seller, doc)

    # 6. Lines & Tax Aggregation
    lines, tax_subtotals, total_line_extension, total_tax = _extract_line_items(doc, vat_cat_header, sign)
    _reconcile_vat_with_erpnext(doc, total_tax, sign)

    # 7. Monetary Totals. Header discounts are already distributed into each line's
    # net_amount by ERPNext, so no document-level allowance is emitted.
    tax_exclusive_amount = total_line_extension
    tax_inclusive_amount = tax_exclusive_amount + total_tax
    rounding_amount = Decimal("0.00")
    if not cint(doc.get("disable_rounded_total")):
        rounding_amount = _q2(doc.get("rounding_adjustment")) * sign
    payable_amount = tax_inclusive_amount + rounding_amount

    monetary_totals = {
        "line_extension_amount": float(total_line_extension),
        "tax_exclusive_amount": float(tax_exclusive_amount),
        "tax_inclusive_amount": float(tax_inclusive_amount),
        "tax_amount": float(total_tax),
        "allowance_total_amount": 0.0,
        "charge_total_amount": 0.0,
        "payable_rounding_amount": float(rounding_amount),
        "payable_amount": float(payable_amount),
    }
    # PINT-AE requires the VAT total in AED as well when invoicing in another currency
    if exchange_rate:
        monetary_totals["tax_amount_aed"] = float(_q2(doc.get("base_total_taxes_and_charges")) * sign)

    # 8. Payment Means
    payment_means = _extract_payment_means(doc)

    # 9. Billing References (Original Invoice if Credit/Debit Note)
    billing_reference = None
    if is_return:
        billing_reference = _extract_billing_reference(doc)

    # 10. Billing period: ERPNext's service period (From / To Date), never the due date
    invoice_period = None
    if doc.get("from_date") and doc.get("to_date"):
        invoice_period = {
            "start_date": getdate(doc.from_date).isoformat(),
            "end_date": getdate(doc.to_date).isoformat(),
            "description_code": doc.get("custom_frequency_billing_code_list") or None,
        }

    return {
        "document_type": document_type,
        "document_identifier": doc.name,
        "source_id": doc.name,
        "buyer_reference": doc.get("po_no") or None,
        "transaction_type_code": resolve_transaction_type_code(doc.get("custom_invoice_transaction_type_code")),
        "credit_note_reason_code": _code_prefix(doc.get("custom_credit_note_reason_code")) if is_return else None,
        "issue_date": posting_date,
        "issue_time": issue_time,
        "tax_point_date": tax_point_date,
        "due_date": due_date,
        "document_currency": currency,
        "currency_exchange_rate": exchange_rate,
        "seller": seller,
        "buyer": buyer,
        "lines": lines,
        "tax_subtotals": tax_subtotals,
        "monetary_totals": monetary_totals,
        "payment_means": payment_means,
        "billing_reference": billing_reference,
        "invoice_period": invoice_period,
        "notes": doc.get("custom_invoice_note") or doc.get("remarks") or None,
    }


def _extract_company_party(doc, company_doc) -> Dict[str, Any]:
    """The company's legal identity, using the invoice's company address or the company default."""
    address_name = (
        doc.get("company_address")
        or doc.get("billing_address")
        or get_default_address("Company", company_doc.name)
    )
    tax_id = company_doc.tax_id or ""
    return _build_party(
        legal_name=company_doc.company_name,
        tax_id=tax_id,
        trade_license=company_doc.get("custom_trade_license_number"),
        legal_registration_type=company_doc.get("custom_legal_registration_identifier_type"),
        peppol_id=company_doc.get("custom_peppol_id"),
        address_name=address_name,
        email=company_doc.get("email"),
        phone=company_doc.get("phone_no"),
    )


def _extract_counterparty(doc, is_sales: bool) -> Dict[str, Any]:
    """The customer (sales) or supplier (self-billed purchase)."""
    if is_sales:
        party_type, party_name = "Customer", doc.customer
        address_name = doc.get("customer_address")
    else:
        party_type, party_name = "Supplier", doc.supplier
        address_name = doc.get("supplier_address")

    party_doc = frappe.get_cached_doc(party_type, party_name)
    legal_name = party_doc.get("customer_name") or party_doc.get("supplier_name") or party_name
    tax_id = party_doc.tax_id or ""

    party = _build_party(
        legal_name=legal_name,
        tax_id=tax_id,
        trade_license=party_doc.get("custom_trade_license_number"),
        legal_registration_type=party_doc.get("custom_legal_registration_identifier_type"),
        peppol_id=party_doc.get("custom_peppol_id"),
        address_name=address_name or get_default_address(party_type, party_name),
        email=party_doc.get("email_id"),
        phone=party_doc.get("mobile_no"),
    )
    party["fz_beneficiary_id"] = party_doc.get("custom_fz_beneficiary_id")
    return party


def _build_party(
    legal_name, tax_id, trade_license, peppol_id, address_name, email, phone, legal_registration_type=None
) -> Dict[str, Any]:
    """
    Flattens a party and its Address into the canonical party shape.
    Missing values stay empty: no invented Dubai / 00000 defaults, and the trade licence
    is never replaced by the TRN.
    """
    address = frappe.get_cached_doc("Address", address_name) if address_name else None

    country_code = ""
    if address and address.country:
        country_code = (frappe.db.get_value("Country", address.country, "code") or "").upper()

    emirate_code = None
    if address and country_code == "AE":
        emirate_code = resolve_emirate_code(address.get("emirate") or address.get("state"))

    street = ""
    if address:
        street = ", ".join(filter(None, [address.address_line1, address.get("address_line2")]))

    # The UAE Peppol participant ID is 0235 + the 10-digit TIN (the first 10 digits of the TRN).
    # Only UAE parties have one; a foreign VAT number is not a TIN.
    tin = tin_from_trn(tax_id) if country_code in ("AE", "") else None
    legal_registration = None
    if trade_license:
        legal_registration = {
            "type": LEGAL_REGISTRATION_TYPES.get(str(legal_registration_type or "").strip().lower(), "TL"),
            "id": trade_license,
        }

    return {
        "legal_name": legal_name,
        "trade_name": legal_name,
        "tax_id": tax_id,
        "tin": tin,
        "trade_license": trade_license,
        "legal_registration": legal_registration,
        "peppol_id": peppol_id or (f"0235:{tin}" if tin else None),
        "street_address": street,
        "city": (address.city if address else "") or "",
        "emirate_code": emirate_code,
        "postal_code": (address.pincode if address else "") or "",
        "country_code": country_code,
        "email": (address.get("email_id") if address else None) or email or "",
        "phone": (address.get("phone") if address else None) or phone or "",
    }


def _validate_seller(seller: Dict[str, Any], doc) -> None:
    """The seller identity is mandatory in PINT-AE; refuse to transmit an invented one."""
    missing = []
    if not seller.get("legal_name"):
        missing.append(_("Legal Name"))
    if not seller.get("tax_id"):
        missing.append(_("Tax ID (TRN)"))
    if not seller.get("street_address"):
        missing.append(_("Address Line 1"))
    if not seller.get("city"):
        missing.append(_("City"))
    if not seller.get("country_code"):
        missing.append(_("Country"))
    elif seller["country_code"] == "AE" and not seller.get("emirate_code"):
        missing.append(_("Emirate"))

    if missing:
        party = doc.company if doc.doctype == "Sales Invoice" else doc.supplier
        frappe.throw(
            _("Cannot build UAE e-invoice {0}: seller {1} is missing {2}. Set them on its Address / master record.").format(
                doc.name, party, ", ".join(missing)
            ),
            title=_("Seller details incomplete"),
        )


def _vat_tax_row(doc):
    """
    The invoice's VAT row: the first percentage-based tax row.
    Fixed 'Actual' rows (freight etc.) carry no VAT treatment and are rejected,
    since they would otherwise silently disappear from the e-invoice.
    """
    vat_row = None
    for t in doc.get("taxes") or []:
        if t.charge_type == "Actual":
            if flt(t.tax_amount):
                frappe.throw(
                    _("Row {0} ({1}) is a fixed 'Actual' charge. UAE e-invoices need a VAT treatment for every "
                      "charge: add it as an item line instead.").format(t.idx, t.description or t.account_head),
                    title=_("Unsupported charge row"),
                )
            continue
        if vat_row is None:
            vat_row = t
    return vat_row


def _item_vat_rate(item, vat_row) -> Optional[Decimal]:
    """Item-specific rate from the Item Tax Template, as ERPNext itself applied it."""
    if not vat_row or not item.get("item_tax_rate"):
        return None
    try:
        rates = json.loads(item.item_tax_rate) if isinstance(item.item_tax_rate, str) else item.item_tax_rate
    except ValueError:
        return None
    if vat_row.account_head in (rates or {}):
        return Decimal(str(rates[vat_row.account_head] or 0))
    return None


def _extract_line_items(doc, default_vat_category: str, sign: Decimal = Decimal(1)):
    """
    Processes line items and aggregates tax totals per VAT Category.
    Line amounts use ERPNext's net_amount (after header discount, excluding inclusive tax).
    VAT per category is taxable amount x rate, rounded once per category (BR-S-09).
    """
    lines = []
    category_buckets: Dict[tuple, Dict[str, Any]] = {}
    total_line_extension = Decimal("0.00")

    vat_row = _vat_tax_row(doc)
    default_tax_rate = Decimal(str(vat_row.rate or 0)) if vat_row else Decimal("0.00")

    for idx, item in enumerate(doc.items, 1):
        qty = Decimal(str(item.qty or 0)) * sign
        net_amount = _q2(item.get("net_amount") if item.get("net_amount") is not None else item.amount) * sign
        net_rate = Decimal(str(item.get("net_rate") if item.get("net_rate") is not None else item.rate or 0))
        total_line_extension += net_amount

        # Item Classification
        item_type = item.get("custom_item_type_codes") or "G - Goods"
        hs_code = item.get("custom_hs_code_") or item.get("custom_hs_code")
        sac_code = item.get("custom_sac_code")

        commodity_code = "G"
        if "Service" in str(item_type):
            commodity_code = "S"
        elif "Both" in str(item_type):
            commodity_code = "B"

        # VAT Category & Rate resolution
        vat_category = default_vat_category
        tax_rate = default_tax_rate

        exemption_reason = doc.get("custom_vat_exemption_reason_code")
        rcm_goods_type = doc.get("custom_rcm_nature_code")
        if item.get("item_tax_template"):
            itt = frappe.get_cached_doc("Item Tax Template", item.item_tax_template)
            if itt.get("custom_vat_category"):
                vat_category = resolve_vat_category(itt.custom_vat_category)
            item_rate = _item_vat_rate(item, vat_row)
            if item_rate is not None:
                tax_rate = item_rate
            exemption_reason = itt.get("custom_vat_exemption_reason_code") or exemption_reason
            rcm_goods_type = itt.get("custom_rcm_nature_code") or rcm_goods_type

        if vat_category in ZERO_VAT_CATEGORIES:
            tax_rate = Decimal("0.00")
        elif vat_category == VAT_CATEGORY_STANDARD and not tax_rate:
            frappe.throw(
                _("Row {0} ({1}) is Standard rated but no VAT rate applies to it. "
                  "Add the VAT tax row or set the correct VAT category.").format(item.idx, item.item_code),
                title=_("Missing VAT rate"),
            )

        line_tax = _q2(net_amount * tax_rate / Decimal("100"))

        # Aggregate tax buckets
        key = (vat_category, tax_rate)
        if key not in category_buckets:
            category_buckets[key] = {
                "category": vat_category,
                "rate": float(tax_rate),
                "taxable_amount": Decimal("0.00"),
            }
        category_buckets[key]["taxable_amount"] += net_amount

        lines.append({
            "id": str(idx),
            "name": item.item_name or item.item_code,
            "description": item.description or item.item_name or item.item_code,
            "quantity": float(qty),
            "item_code": item.item_code,
            "uom": item.uom or "NOS",
            "unit_code": resolve_unit_code(item.uom),
            "gtin": item.get("barcode") or None,
            "exemption_reason_code": _code_prefix(exemption_reason) if vat_category == VAT_CATEGORY_EXEMPT else None,
            "exemption_reason": (str(exemption_reason).split(" - ", 1)[-1] if exemption_reason and vat_category == VAT_CATEGORY_EXEMPT else None),
            "rcm_goods_type": _code_prefix(rcm_goods_type) if vat_category == VAT_CATEGORY_REVERSE_CHARGE else None,
            "unit_price": float(net_rate),
            "line_extension_amount": float(net_amount),
            "commodity_code": commodity_code,
            "hs_code": hs_code,
            "sac_code": sac_code,
            "vat_category": vat_category,
            "vat_percentage": float(tax_rate),
            "vat_amount": float(line_tax),
        })

    # Prepare formatted tax subtotals
    tax_subtotals = []
    total_tax_amount = Decimal("0.00")
    for bucket in category_buckets.values():
        taxable = _q2(bucket["taxable_amount"])
        tax_amount = _q2(taxable * Decimal(str(bucket["rate"])) / Decimal("100"))
        total_tax_amount += tax_amount
        tax_subtotals.append({
            "category": bucket["category"],
            "rate": bucket["rate"],
            "taxable_amount": float(taxable),
            "tax_amount": float(tax_amount),
        })

    return lines, tax_subtotals, total_line_extension, total_tax_amount


def _reconcile_vat_with_erpnext(doc, canonical_tax: Decimal, sign: Decimal) -> None:
    """
    The VAT reported to the FTA must equal the VAT on the invoice ERPNext posted.
    A mismatch means a category / template misconfiguration, so stop rather than report it.
    """
    erpnext_tax = _q2(doc.get("total_taxes_and_charges")) * sign
    if abs(erpnext_tax - canonical_tax) > VAT_RECONCILIATION_TOLERANCE:
        frappe.throw(
            _("VAT mismatch on {0}: ERPNext charged {1} but the VAT categories on the lines give {2}. "
              "Check the VAT category of the invoice, its items and Item Tax Templates.").format(
                doc.name, erpnext_tax, canonical_tax
            ),
            title=_("VAT does not reconcile"),
        )


def _extract_billing_reference(doc) -> Optional[Dict[str, Any]]:
    """Original invoice for a credit note, with the original invoice's own issue date."""
    ref_invoice = doc.get("return_against")
    if ref_invoice:
        original_date, original_document_id = frappe.db.get_value(
            doc.doctype, ref_invoice, ["posting_date", "custom_document_id"]
        ) or (None, None)
        return {
            "id": ref_invoice,
            "issue_date": getdate(original_date).isoformat() if original_date else None,
            # The ASP's ID for the original, when it was transmitted through the ASP
            "document_id": original_document_id if original_document_id not in (None, "", "Not Submitted") else None,
        }

    # Original issued outside this system: only its number is known
    external_ref = doc.get("custom_return_against_for_uae_einvoice")
    if external_ref:
        return {"id": external_ref, "issue_date": None, "document_id": None}
    return None


def _extract_payment_means(doc) -> List[Dict[str, Any]]:
    """
    Payment means (UN/ECE 4461) from the invoice's own code or its Mode of Payment.
    Nothing configured -> no payment means, so the ASP applies its default (credit transfer)
    instead of the invoice silently claiming cash.
    """
    code_label = doc.get("custom_payment_means_codes")
    if not code_label:
        mode = doc.get("mode_of_payment") or next((p.mode_of_payment for p in doc.get("payments") or []), None)
        if mode:
            code_label = frappe.db.get_value("Mode of Payment", mode, "custom_payment_means_codes")

    payment_code = str(code_label or "").split(" - ")[0].strip()
    if payment_code not in PAYMENT_MEANS_MAP:
        return []

    means_dict = {
        "payment_means_code": payment_code,
        "payment_means_name": PAYMENT_MEANS_MAP[payment_code],
    }

    # Account to pay into, for transfers: the IBAN field, never the local account number
    if payment_code in ("30", "42", "58") and doc.get("company_bank_account"):
        bank = frappe.get_cached_doc("Bank Account", doc.company_bank_account)
        if bank.get("iban"):
            means_dict["payee_financial_account"] = {
                "iban": bank.iban,
                "name": bank.account_name,
                "branch": bank.branch_code or "",
            }

    return [means_dict]
