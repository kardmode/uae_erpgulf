"""
Unit tests for UAE Inbound E-Invoicing & Automated Procure-to-Pay (P2P) Engine.
Validates 15-point edge-case matching matrix, duplicate blocking,
over-billing protection, hierarchical item matching, and expense fallbacks.
"""

import sys
import unittest
from unittest.mock import MagicMock, patch

from uae_erpgulf.tests import REAL_FRAPPE

# Ensure frappe mock if running standalone
if "frappe" in sys.modules and isinstance(sys.modules["frappe"], MagicMock):
    frappe_mock = sys.modules["frappe"]
else:
    frappe_mock = MagicMock()
    frappe_mock.__path__ = []
    frappe_mock._ = lambda x, *args: x

    def _whitelist(*args, **kwargs):
        def decorator(fn):
            return fn
        if args and callable(args[0]):
            return args[0]
        return decorator

    frappe_mock.whitelist = _whitelist
    frappe_mock.ValidationError = type("ValidationError", (Exception,), {})
    frappe_mock.DuplicateEntryError = type("DuplicateEntryError", (Exception,), {})
    frappe_mock.db = MagicMock()
    if not REAL_FRAPPE:
        sys.modules["frappe"] = frappe_mock

if "frappe.utils" not in sys.modules:
    if not REAL_FRAPPE:
        sys.modules["frappe.utils"] = MagicMock()

for mod in ["pytz", "requests"]:
    if mod not in sys.modules:
        try:
            __import__(mod)
        except ImportError:
            if not REAL_FRAPPE:
                sys.modules[mod] = MagicMock()

from uae_erpgulf.core.inbound_processor import (
    normalize_po_reference,
    parse_inbound_payload,
    resolve_supplier,
    validate_duplication,
    match_purchase_order,
    resolve_line_item,
    InboundProcessingError,
    DuplicateInboundInvoiceError,
)


@unittest.skipIf(REAL_FRAPPE, "standalone mock tests: run with python -m unittest, not bench")
class TestInboundP2PEngine(unittest.TestCase):
    def setUp(self):
        self.sample_flick_json = {
            "document": {
                "document_identifier": "INV-2026-9001",
                "invoice_type_code": "388",
                "issue_date": "2026-10-25",
                "document_currency": "AED",
                "po_reference": "PO# 451",
                "issuing_party": {
                    "legal_name": "Al Rostamani Trading LLC",
                    "vat_number": "100345678900003",
                    "peppol_id": "0235:100345678900003"
                },
                "receiving_party": {
                    "legal_name": "My Company UAE LLC",
                    "vat_number": "100111222333003"
                },
                "legal_monetary_total": {
                    "line_extension_amount": "5000.00",
                    "tax_exclusive_amount": "5000.00",
                    "tax_inclusive_amount": "5250.00",
                    "payable_amount": "5250.00"
                },
                "invoice_lines": [
                    {
                        "name": "Industrial Safety Valve",
                        "description": "DN50 PN16 Flanged",
                        "invoiced_quantity": "10",
                        "uom": "NOS",
                        "unit_price": "500.00",
                        "line_extension_amount": "5000.00",
                        "hs_code": "VLV-SUP-01",
                        "sac_code": "6291100123456",
                        "vat_percentage": "5.0"
                    }
                ]
            }
        }

        self.sample_ubl_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
    xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
    xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
    <cbc:ID>INV-UBL-7700</cbc:ID>
    <cbc:IssueDate>2026-10-25</cbc:IssueDate>
    <cbc:DocumentCurrencyCode>AED</cbc:DocumentCurrencyCode>
    <cac:OrderReference>
        <cbc:ID>PO-2026-991</cbc:ID>
    </cac:OrderReference>
    <cac:AccountingSupplierParty>
        <cac:Party>
            <cbc:EndpointID schemeID="0235">100555666777003</cbc:EndpointID>
            <cac:PartyName>
                <cbc:Name>Al Naboodah General Trading</cbc:Name>
            </cac:PartyName>
            <cac:PartyTaxScheme>
                <cbc:CompanyID>100555666777003</cbc:CompanyID>
                <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
            </cac:PartyTaxScheme>
        </cac:Party>
    </cac:AccountingSupplierParty>
    <cac:AccountingCustomerParty>
        <cac:Party>
            <cac:PartyName>
                <cbc:Name>My Company UAE LLC</cbc:Name>
            </cac:PartyName>
        </cac:Party>
    </cac:AccountingCustomerParty>
    <cac:LegalMonetaryTotal>
        <cbc:LineExtensionAmount currencyID="AED">2000.00</cbc:LineExtensionAmount>
        <cbc:TaxExclusiveAmount currencyID="AED">2000.00</cbc:TaxExclusiveAmount>
        <cbc:TaxInclusiveAmount currencyID="AED">2100.00</cbc:TaxInclusiveAmount>
        <cbc:PayableAmount currencyID="AED">2100.00</cbc:PayableAmount>
    </cac:LegalMonetaryTotal>
    <cac:InvoiceLine>
        <cbc:ID>1</cbc:ID>
        <cbc:InvoicedQuantity unitCode="HUR">20</cbc:InvoicedQuantity>
        <cbc:LineExtensionAmount currencyID="AED">2000.00</cbc:LineExtensionAmount>
        <cac:Item>
            <cbc:Name>Electrical Maintenance Service</cbc:Name>
            <cac:ClassifiedTaxCategory>
                <cbc:ID>S</cbc:ID>
                <cbc:Percent>5.0</cbc:Percent>
            </cac:ClassifiedTaxCategory>
        </cac:Item>
        <cac:Price>
            <cbc:PriceAmount currencyID="AED">100.00</cbc:PriceAmount>
        </cac:Price>
    </cac:InvoiceLine>
</Invoice>"""

    def test_normalize_po_reference(self):
        """Validates PO string normalization across human & supplier prefix variants."""
        self.assertEqual(normalize_po_reference("PO# 00451"), "00451")
        self.assertEqual(normalize_po_reference("po-2026-00451"), "202600451")
        self.assertEqual(normalize_po_reference("Purchase Order: 9918"), "9918")
        self.assertEqual(normalize_po_reference(" # 7712 "), "7712")
        self.assertEqual(normalize_po_reference(""), "")
        self.assertEqual(normalize_po_reference(None), "")

    def test_parse_json_inbound_correct_supplier(self):
        """Validates that issuing_party is parsed as Seller, not receiving_party."""
        payload = parse_inbound_payload(self.sample_flick_json)
        self.assertEqual(payload["document_identifier"], "INV-2026-9001")
        self.assertEqual(payload["seller"]["tax_id"], "100345678900003")
        self.assertEqual(payload["seller"]["legal_name"], "Al Rostamani Trading LLC")
        self.assertEqual(payload["po_reference"], "PO# 451")
        self.assertEqual(len(payload["lines"]), 1)
        self.assertEqual(payload["lines"][0]["seller_item_id"], "VLV-SUP-01")
        self.assertEqual(payload["lines"][0]["barcode"], "6291100123456")

    def test_parse_ubl_xml_inbound(self):
        """Validates XML parsing for PEPPOL UBL 2.1 invoices."""
        payload = parse_inbound_payload(self.sample_ubl_xml)
        self.assertEqual(payload["document_identifier"], "INV-UBL-7700")
        self.assertEqual(payload["seller"]["tax_id"], "100555666777003")
        self.assertEqual(payload["seller"]["legal_name"], "Al Naboodah General Trading")
        self.assertEqual(payload["po_reference"], "PO-2026-991")
        self.assertEqual(payload["monetary_totals"]["payable_amount"], 2100.00)
        self.assertEqual(len(payload["lines"]), 1)
        self.assertEqual(payload["lines"][0]["name"], "Electrical Maintenance Service")
        self.assertEqual(payload["lines"][0]["quantity"], 20.0)
        self.assertEqual(payload["lines"][0]["rate"], 100.0)

    @patch.object(frappe_mock.db, "get_value")
    def test_resolve_supplier_by_trn(self, mock_get_value):
        """Validates supplier resolution by 15-digit UAE TRN."""
        mock_get_value.return_value = "SUP-AL-ROSTAMANI"
        seller_info = {"tax_id": "100345678900003", "legal_name": "Al Rostamani"}
        res = resolve_supplier(seller_info)
        self.assertEqual(res, "SUP-AL-ROSTAMANI")

    @patch.object(frappe_mock.db, "get_value")
    def test_resolve_supplier_not_found(self, mock_get_value):
        """Validates that missing supplier throws InboundProcessingError."""
        mock_get_value.return_value = None
        seller_info = {"tax_id": "999999999999999", "legal_name": "Ghost Corp"}
        with self.assertRaises(InboundProcessingError):
            resolve_supplier(seller_info)

    @patch.object(frappe_mock.db, "get_value")
    def test_duplicate_check_blocks(self, mock_get_value):
        """Validates that identical bill_no or custom_document_id raises DuplicateInboundInvoiceError."""
        mock_get_value.return_value = "PINV-2026-00012"
        with self.assertRaises(DuplicateInboundInvoiceError):
            validate_duplication(supplier="SUP-01", bill_no="INV-9001", document_id="DOC-9001")

    @patch.object(frappe_mock, "get_all")
    @patch.object(frappe_mock, "get_doc")
    def test_po_overbilling_guard(self, mock_get_doc, mock_get_all):
        """Validates that PO matching guards against 100% billed POs."""
        mock_get_all.return_value = [{"name": "PO-2026-00451"}]
        mock_po = MagicMock()
        mock_po.docstatus = 1
        mock_po.grand_total = 5000.00
        mock_po.per_billed = 100.0
        mock_po.billed_amount = 5000.00
        mock_get_doc.return_value = mock_po

        matched_po, warning = match_purchase_order("SUP-01", "PO-2026-00451", "Company A")
        self.assertIsNone(matched_po)
        self.assertIn("already 100% billed", warning)

    @patch.object(frappe_mock, "get_all")
    @patch.object(frappe_mock, "get_doc")
    def test_po_partial_billed_allowed(self, mock_get_doc, mock_get_all):
        """Validates that partially billed POs are safely matched."""
        mock_get_all.return_value = [{"name": "PO-2026-00451"}]
        mock_po = MagicMock()
        mock_po.docstatus = 1
        mock_po.grand_total = 10000.00
        mock_po.per_billed = 50.0
        mock_po.billed_amount = 5000.00
        mock_get_doc.return_value = mock_po

        matched_po, warning = match_purchase_order("SUP-01", "PO-2026-00451", "Company A")
        self.assertIsNotNone(matched_po)
        self.assertIsNone(warning)

    @patch.object(frappe_mock.db, "get_value")
    def test_resolve_line_item_hierarchical(self, mock_get_value):
        """Validates hierarchical resolution: Supplier Item -> Barcode -> Item Code -> Expense Fallback."""
        # Case 1: Supplier Item match
        mock_get_value.side_effect = lambda dt, filters, field: "ITEM-VALVE-01" if dt == "Item Supplier" else None
        line_data = {"seller_item_id": "VLV-SUP-01", "name": "Valve", "description": "Valve"}
        res = resolve_line_item(line_data, supplier="SUP-01", company="Company A")
        self.assertEqual(res["item_code"], "ITEM-VALVE-01")

        # Case 2: Expense fallback when no inventory item matches (service item)
        mock_get_value.side_effect = lambda dt, filters, field: "5111 - Operating Expenses - CO" if dt == "Account" else None
        line_service = {"name": "Audit Consulting", "description": "Annual Audit", "seller_item_id": None}
        res_service = resolve_line_item(line_service, supplier="SUP-01", company="Company A")
        self.assertIsNone(res_service["item_code"])
        self.assertEqual(res_service["expense_account"], "5111 - Operating Expenses - CO")

    @patch("uae_erpgulf.core.inbound_processor.match_purchase_order", return_value=(None, None))
    @patch("uae_erpgulf.core.inbound_processor.validate_duplication")
    @patch("uae_erpgulf.core.inbound_processor.resolve_supplier", return_value="Al Najm Supplies")
    def test_missing_project_with_mrp_names_the_company_setting(self, *_mocks):
        """With the MRP app (project mandatory on drafts), no project stops processing with a clear fix."""
        from uae_erpgulf.core.inbound_processor import create_purchase_invoice_from_payload

        payload = {"document_identifier": "SUP-INV-9", "seller": {}, "lines": [], "is_return": False}
        with patch.object(frappe_mock, "get_installed_apps", return_value=["frappe", "erpnext", "mrp"]), \
                patch.object(frappe_mock, "get_cached_value", return_value=None), \
                patch.object(frappe_mock.db, "has_column", return_value=True):
            with self.assertRaises(InboundProcessingError) as ctx:
                create_purchase_invoice_from_payload(payload, company="Gulf Oasis LLC")

        self.assertIn("Default Inbound E-Invoice Project", str(ctx.exception))
        self.assertIn("Gulf Oasis LLC", str(ctx.exception))
        frappe_mock.new_doc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
