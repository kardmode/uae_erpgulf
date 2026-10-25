"""
Generic PEPPOL / UBL 2.1 Reference ASP Adapter.
Produces UAE PINT-AE UBL 2.1 XML from the canonical model. It renders XML only:
it has no transport, so it never reports an invoice as transmitted. ASP adapters
that accept raw UBL (e.g. TronStride) reuse transform_payload.
"""

from typing import Dict, Any, List, Tuple, Optional
from xml.sax.saxutils import escape, quoteattr
from uae_erpgulf.adapters.base import BaseASPAdapter, value_or
from uae_erpgulf.core.constants import CREDIT_NOTE_TYPES

PINT_AE_CUSTOMIZATION_ID = "urn:peppol:pint:billing-1@ae-1"
PINT_PROFILE_ID = "urn:peppol:bis:billing"

NOT_TRANSMITTED_MESSAGE = (
    "The Generic UBL adapter only renders PINT-AE XML; it does not transmit invoices. "
    "Select an accredited service provider on the Company to report invoices to the FTA."
)


def _t(value: Any) -> str:
    """Escaped element text."""
    return escape("" if value is None else str(value))


def _amt(value: Any) -> str:
    return f"{float(value or 0.0):.2f}"


class GenericUBLAdapter(BaseASPAdapter):
    """Reference adapter producing standard PEPPOL UBL 2.1 XML (render only, no transport)."""

    def test_connection(self) -> Tuple[bool, str]:
        return False, NOT_TRANSMITTED_MESSAGE

    def transform_payload(self, canonical_doc: Dict[str, Any]) -> str:
        """
        Builds UBL 2.1 Invoice / CreditNote XML.
        """
        doc_type = str(canonical_doc.get("document_type") or "380")
        is_credit_note = doc_type in CREDIT_NOTE_TYPES
        root_tag = "CreditNote" if is_credit_note else "Invoice"
        line_tag = "CreditNoteLine" if is_credit_note else "InvoiceLine"
        qty_tag = "CreditedQuantity" if is_credit_note else "InvoicedQuantity"
        curr = canonical_doc.get("document_currency") or "AED"
        cur_attr = quoteattr(curr)

        xml_lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            f'<{root_tag} xmlns="urn:oasis:names:specification:ubl:schema:xsd:{root_tag}-2"',
            '    xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"',
            '    xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">',
            f'    <cbc:CustomizationID>{PINT_AE_CUSTOMIZATION_ID}</cbc:CustomizationID>',
            f'    <cbc:ProfileID>{PINT_PROFILE_ID}</cbc:ProfileID>',
            f'    <cbc:ID>{_t(canonical_doc.get("document_identifier"))}</cbc:ID>',
            f'    <cbc:IssueDate>{_t(canonical_doc.get("issue_date"))}</cbc:IssueDate>',
            f'    <cbc:IssueTime>{_t(canonical_doc.get("issue_time") or "00:00:00")}</cbc:IssueTime>',
        ]
        if canonical_doc.get("due_date") and not is_credit_note:
            xml_lines.append(f'    <cbc:DueDate>{_t(canonical_doc["due_date"])}</cbc:DueDate>')
        xml_lines.append(f'    <cbc:{root_tag}TypeCode>{_t(doc_type)}</cbc:{root_tag}TypeCode>')
        if canonical_doc.get("notes"):
            xml_lines.append(f'    <cbc:Note>{_t(canonical_doc["notes"])}</cbc:Note>')
        if canonical_doc.get("tax_point_date"):
            xml_lines.append(f'    <cbc:TaxPointDate>{_t(canonical_doc["tax_point_date"])}</cbc:TaxPointDate>')
        xml_lines.append(f'    <cbc:DocumentCurrencyCode>{_t(curr)}</cbc:DocumentCurrencyCode>')
        if curr != "AED":
            xml_lines.append('    <cbc:TaxCurrencyCode>AED</cbc:TaxCurrencyCode>')

        # Billing Reference for Credit/Debit Notes
        billing_ref = canonical_doc.get("billing_reference")
        if billing_ref:
            if not isinstance(billing_ref, dict):
                billing_ref = {"id": billing_ref}
            xml_lines.extend([
                '    <cac:BillingReference>',
                '        <cac:InvoiceDocumentReference>',
                f'            <cbc:ID>{_t(billing_ref.get("id"))}</cbc:ID>',
            ])
            if billing_ref.get("issue_date"):
                xml_lines.append(f'            <cbc:IssueDate>{_t(billing_ref["issue_date"])}</cbc:IssueDate>')
            xml_lines.extend([
                '        </cac:InvoiceDocumentReference>',
                '    </cac:BillingReference>',
            ])

        xml_lines.extend(self._party_xml("AccountingSupplierParty", canonical_doc.get("seller") or {}))
        xml_lines.extend(self._party_xml("AccountingCustomerParty", canonical_doc.get("buyer") or {}))

        # TaxTotal (document currency, plus AED when invoicing in a foreign currency)
        totals = canonical_doc.get("monetary_totals", {}) or {}
        subtotals = self._tax_subtotals(canonical_doc)
        tax_amount = value_or(totals, "tax_amount", sum(s["tax_amount"] for s in subtotals))
        xml_lines.append('    <cac:TaxTotal>')
        xml_lines.append(f'        <cbc:TaxAmount currencyID={cur_attr}>{_amt(tax_amount)}</cbc:TaxAmount>')
        for sub in subtotals:
            xml_lines.extend([
                '        <cac:TaxSubtotal>',
                f'            <cbc:TaxableAmount currencyID={cur_attr}>{_amt(sub["taxable_amount"])}</cbc:TaxableAmount>',
                f'            <cbc:TaxAmount currencyID={cur_attr}>{_amt(sub["tax_amount"])}</cbc:TaxAmount>',
                *self._tax_category_xml("TaxCategory", sub["category"], sub["rate"], "            "),
                '        </cac:TaxSubtotal>',
            ])
        xml_lines.append('    </cac:TaxTotal>')
        if curr != "AED" and totals.get("tax_amount_aed") is not None:
            xml_lines.extend([
                '    <cac:TaxTotal>',
                f'        <cbc:TaxAmount currencyID="AED">{_amt(totals["tax_amount_aed"])}</cbc:TaxAmount>',
                '    </cac:TaxTotal>',
            ])

        # LegalMonetaryTotal
        xml_lines.extend([
            '    <cac:LegalMonetaryTotal>',
            f'        <cbc:LineExtensionAmount currencyID={cur_attr}>{_amt(totals.get("line_extension_amount"))}</cbc:LineExtensionAmount>',
            f'        <cbc:TaxExclusiveAmount currencyID={cur_attr}>{_amt(totals.get("tax_exclusive_amount"))}</cbc:TaxExclusiveAmount>',
            f'        <cbc:TaxInclusiveAmount currencyID={cur_attr}>{_amt(totals.get("tax_inclusive_amount"))}</cbc:TaxInclusiveAmount>',
        ])
        if float(totals.get("allowance_total_amount") or 0):
            xml_lines.append(f'        <cbc:AllowanceTotalAmount currencyID={cur_attr}>{_amt(totals["allowance_total_amount"])}</cbc:AllowanceTotalAmount>')
        if float(totals.get("charge_total_amount") or 0):
            xml_lines.append(f'        <cbc:ChargeTotalAmount currencyID={cur_attr}>{_amt(totals["charge_total_amount"])}</cbc:ChargeTotalAmount>')
        if float(totals.get("payable_rounding_amount") or 0):
            xml_lines.append(f'        <cbc:PayableRoundingAmount currencyID={cur_attr}>{_amt(totals["payable_rounding_amount"])}</cbc:PayableRoundingAmount>')
        xml_lines.extend([
            f'        <cbc:PayableAmount currencyID={cur_attr}>{_amt(totals.get("payable_amount"))}</cbc:PayableAmount>',
            '    </cac:LegalMonetaryTotal>',
        ])

        # Lines
        for idx, line in enumerate(canonical_doc.get("lines") or [], 1):
            category = line.get("vat_category") or line.get("tax_category") or "S"
            rate = value_or(line, "vat_percentage", value_or(line, "tax_rate", 0.0))
            xml_lines.extend([
                f'    <cac:{line_tag}>',
                f'        <cbc:ID>{_t(line.get("id") or line.get("idx") or idx)}</cbc:ID>',
                f'        <cbc:{qty_tag} unitCode={quoteattr(str(line.get("uom") or "C62"))}>{float(line.get("quantity") or 0)}</cbc:{qty_tag}>',
                f'        <cbc:LineExtensionAmount currencyID={cur_attr}>{_amt(value_or(line, "line_extension_amount", line.get("line_amount")))}</cbc:LineExtensionAmount>',
                '        <cac:Item>',
                f'            <cbc:Name>{_t(line.get("name") or line.get("item_name"))}</cbc:Name>',
                *self._tax_category_xml("ClassifiedTaxCategory", category, rate, "            "),
                '        </cac:Item>',
                '        <cac:Price>',
                f'            <cbc:PriceAmount currencyID={cur_attr}>{_amt(value_or(line, "unit_price", line.get("rate")))}</cbc:PriceAmount>',
                '        </cac:Price>',
                f'    </cac:{line_tag}>',
            ])

        xml_lines.append(f'</{root_tag}>')
        return "\n".join(xml_lines)

    @staticmethod
    def _tax_subtotals(canonical_doc: Dict[str, Any]) -> List[Dict[str, Any]]:
        subtotals = []
        for sub in canonical_doc.get("tax_subtotals") or canonical_doc.get("tax_totals") or []:
            subtotals.append({
                "category": sub.get("category") or sub.get("tax_category") or "S",
                "rate": value_or(sub, "rate", value_or(sub, "tax_rate", 0.0)),
                "taxable_amount": float(sub.get("taxable_amount") or 0.0),
                "tax_amount": float(sub.get("tax_amount") or 0.0),
            })
        return subtotals

    @staticmethod
    def _tax_category_xml(tag: str, category: str, rate: Any, indent: str) -> List[str]:
        return [
            f'{indent}<cac:{tag}>',
            f'{indent}    <cbc:ID>{_t(category)}</cbc:ID>',
            f'{indent}    <cbc:Percent>{float(rate or 0.0):.2f}</cbc:Percent>',
            f'{indent}    <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>',
            f'{indent}</cac:{tag}>',
        ]

    @staticmethod
    def _party_xml(wrapper: str, party: Dict[str, Any]) -> List[str]:
        lines = [f'    <cac:{wrapper}>', '        <cac:Party>']

        peppol_id = party.get("peppol_id") or ""
        if ":" in peppol_id:
            scheme, endpoint = peppol_id.split(":", 1)
            lines.append(f'            <cbc:EndpointID schemeID={quoteattr(scheme)}>{_t(endpoint)}</cbc:EndpointID>')

        lines.extend([
            '            <cac:PartyName>',
            f'                <cbc:Name>{_t(party.get("trade_name") or party.get("legal_name"))}</cbc:Name>',
            '            </cac:PartyName>',
            '            <cac:PostalAddress>',
            f'                <cbc:StreetName>{_t(party.get("street_address"))}</cbc:StreetName>',
            f'                <cbc:CityName>{_t(party.get("city"))}</cbc:CityName>',
        ])
        if party.get("postal_code"):
            lines.append(f'                <cbc:PostalZone>{_t(party["postal_code"])}</cbc:PostalZone>')
        subentity = party.get("emirate_code") or party.get("state")
        if subentity:
            lines.append(f'                <cbc:CountrySubentity>{_t(subentity)}</cbc:CountrySubentity>')
        lines.extend([
            '                <cac:Country>',
            f'                    <cbc:IdentificationCode>{_t(party.get("country_code") or party.get("country"))}</cbc:IdentificationCode>',
            '                </cac:Country>',
            '            </cac:PostalAddress>',
        ])
        if party.get("tax_id"):
            lines.extend([
                '            <cac:PartyTaxScheme>',
                f'                <cbc:CompanyID>{_t(party["tax_id"])}</cbc:CompanyID>',
                '                <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>',
                '            </cac:PartyTaxScheme>',
            ])
        lines.extend([
            '            <cac:PartyLegalEntity>',
            f'                <cbc:RegistrationName>{_t(party.get("legal_name"))}</cbc:RegistrationName>',
        ])
        if party.get("trade_license"):
            lines.append(f'                <cbc:CompanyID>{_t(party["trade_license"])}</cbc:CompanyID>')
        lines.extend([
            '            </cac:PartyLegalEntity>',
            '        </cac:Party>',
            f'    </cac:{wrapper}>',
        ])
        return lines

    def submit_document(self, canonical_doc: Dict[str, Any]) -> Dict[str, Any]:
        # Render so configuration problems still surface, but never claim the invoice was reported
        xml_content = self.transform_payload(canonical_doc)
        return {
            "success": False,
            "status_code": None,
            "document_id": None,
            "reporting_status": None,
            "exchange_status": None,
            "response_data": {"error": NOT_TRANSMITTED_MESSAGE, "xml_length": len(xml_content)},
            "error_message": NOT_TRANSMITTED_MESSAGE,
            "retryable": False,
        }

    def get_document_status(self, document_id: str) -> Dict[str, Any]:
        return {
            "success": False,
            "status_code": None,
            "reporting_status": None,
            "exchange_status": None,
            "reporting_reference": None,
            "response_data": {"error": NOT_TRANSMITTED_MESSAGE},
            "error_message": NOT_TRANSMITTED_MESSAGE,
            "retryable": False,
        }

    def get_document_artifacts(self, document_id: str) -> Dict[str, Optional[bytes]]:
        return {"xml": None, "pdf": None}

    def parse_webhook_payload(self, raw_data: bytes, headers: Dict[str, str]) -> Dict[str, Any]:
        return {}
