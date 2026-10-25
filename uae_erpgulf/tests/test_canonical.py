"""
Unit tests for UAE Canonical Model and ASP Adapters.
Can run standalone without live DB or within Frappe test runner.
"""

import sys
import unittest
from unittest.mock import MagicMock, patch

from uae_erpgulf.tests import REAL_FRAPPE
import xml.etree.ElementTree as ET

# Ensure frappe mock if running standalone
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

if not REAL_FRAPPE:
    sys.modules["frappe"] = frappe_mock
if not REAL_FRAPPE:
    sys.modules["frappe.utils"] = MagicMock()

for mod in ["pytz", "requests"]:
    if mod not in sys.modules:
        try:
            __import__(mod)
        except ImportError:
            if not REAL_FRAPPE:
                sys.modules[mod] = MagicMock()





from uae_erpgulf.adapters.flick import FlickASPAdapter
from uae_erpgulf.adapters.generic_ubl import GenericUBLAdapter
from uae_erpgulf.adapters.factory import ASPFactory


# Credentials are encrypted Password fields; these mock tests read them off the mocked Company
import uae_erpgulf.core.credentials as _credentials


def _mock_company_secret(company, fieldname):
    value = getattr(frappe_mock.get_cached_doc("Company", company), fieldname, None)
    return value if isinstance(value, str) else None


if not REAL_FRAPPE:
    _credentials.get_company_secret = _mock_company_secret


@unittest.skipIf(REAL_FRAPPE, "standalone mock tests: run with python -m unittest, not bench")
class TestCanonicalModelAndAdapters(unittest.TestCase):
    def setUp(self):
        self.sample_canonical = {
            "document_identifier": "ACC-SINV-2026-00001",
            "document_type": "388",
            "issue_date": "2026-10-25",
            "issue_time": "14:30:00",
            "document_currency": "AED",
            "seller": {
                "legal_name": "Gulf Oasis Technologies LLC",
                "tax_id": "100234567890003",
                "peppol_id": "0235:100234567890003",
                "street_address": "Sheikh Zayed Road",
                "city": "Dubai",
                "state": "Dubai",
                "country": "AE"
            },
            "buyer": {
                "legal_name": "Emirates Retail Co",
                "tax_id": "100987654321003",
                "peppol_id": "0235:100987654321003",
                "street_address": "Al Maryah Island",
                "city": "Abu Dhabi",
                "state": "Abu Dhabi",
                "country": "AE"
            },
            "monetary_totals": {
                "line_extension_amount": 1000.0,
                "tax_exclusive_amount": 1000.0,
                "tax_inclusive_amount": 1050.0,
                "payable_amount": 1050.0,
                "allowance_total_amount": 0.0,
                "charge_total_amount": 0.0,
                "prepaid_amount": 0.0
            },
            "tax_totals": [
                {
                    "tax_amount": 50.0,
                    "taxable_amount": 1000.0,
                    "tax_category": "S",
                    "tax_rate": 5.0
                }
            ],
            "lines": [
                {
                    "idx": 1,
                    "item_code": "CONS-01",
                    "item_name": "IT Consulting Services",
                    "quantity": 10.0,
                    "uom": "HUR",
                    "rate": 100.0,
                    "line_amount": 1000.0,
                    "tax_category": "S",
                    "tax_rate": 5.0,
                    "tax_amount": 50.0
                }
            ],
            "billing_reference": None,
            "po_reference": "PO-99120"
        }

    def test_generic_ubl_generation(self):
        """Validates that GenericUBLAdapter produces compliant PEPPOL UBL 2.1 XML."""
        adapter = GenericUBLAdapter(company="Gulf Oasis Technologies LLC")
        xml_output = adapter.transform_payload(self.sample_canonical)

        self.assertTrue(xml_output.startswith("<?xml"))
        self.assertIn("<Invoice", xml_output)
        self.assertIn("<cbc:ID>ACC-SINV-2026-00001</cbc:ID>", xml_output)
        self.assertIn("<cbc:DocumentCurrencyCode>AED</cbc:DocumentCurrencyCode>", xml_output)
        self.assertIn("100234567890003", xml_output)
        self.assertIn("Gulf Oasis Technologies LLC", xml_output)
        self.assertIn("Emirates Retail Co", xml_output)

        # Parse XML to verify well-formedness
        root = ET.fromstring(xml_output)
        self.assertIn("Invoice", root.tag)

    def test_generic_ubl_credit_note(self):
        """Validates that Credit Note canonical doc produces CreditNote root tag."""
        cn_doc = dict(self.sample_canonical)
        cn_doc["document_type"] = "381"
        cn_doc["billing_reference"] = "ACC-SINV-2026-00001"

        adapter = GenericUBLAdapter(company="Gulf Oasis Technologies LLC")
        xml_output = adapter.transform_payload(cn_doc)

        self.assertIn("<CreditNote", xml_output)
        self.assertIn("<cac:BillingReference>", xml_output)
        self.assertIn("<cac:InvoiceDocumentReference>", xml_output)
        self.assertIn("<cbc:ID>ACC-SINV-2026-00001</cbc:ID>", xml_output)

    @patch.object(frappe_mock, "get_cached_doc")
    def test_flick_payload_transformation(self, mock_get_doc):
        """Validates Flick JSON schema transformation."""
        mock_company = MagicMock()
        mock_company.custom_base_url = "https://sb-ae-api.flick.network"
        mock_company.custom_participant_id = "0235:100234567890003"
        mock_company.custom_xflickauthkey = "test_key_123"
        mock_get_doc.return_value = mock_company

        adapter = FlickASPAdapter(company="Gulf Oasis Technologies LLC")
        flick_json = adapter.transform_payload(self.sample_canonical)

        self.assertIn("document", flick_json)
        doc = flick_json["document"]
        self.assertEqual(doc["document_identifier"], "ACC-SINV-2026-00001")
        self.assertEqual(doc["issuing_party"]["vat_number"], "100234567890003")
        self.assertEqual(doc["receiving_party"]["trade_name"], "Emirates Retail Co")
        self.assertEqual(float(doc["legal_monetary_total"]["payable_amount"]), 1050.0)
        self.assertEqual(len(doc["invoice_lines"]), 1)
        self.assertEqual(doc["invoice_lines"][0]["name"], "IT Consulting Services")

    @patch.object(frappe_mock, "get_cached_doc")
    def test_asp_factory_resolution(self, mock_get_doc):
        """Validates dynamic ASP factory resolution by company setting."""
        mock_company_flick = MagicMock()
        mock_company_flick.custom_accredited_service_providers = "Flick Network L.L.C"
        mock_company_flick.custom_base_url = "https://sb-ae-api.flick.network"
        mock_company_flick.custom_participant_id = "0235:100234567890003"
        mock_company_flick.custom_xflickauthkey = "test_key_123"

        mock_company_ubl = MagicMock()
        mock_company_ubl.custom_accredited_service_providers = "Generic UBL"

        mock_get_doc.side_effect = lambda dt, name: mock_company_flick if name == "Company A" else mock_company_ubl

        adapter_a = ASPFactory.get_adapter("Company A")
        self.assertIsInstance(adapter_a, FlickASPAdapter)

        adapter_b = ASPFactory.get_adapter("Company B")
        self.assertIsInstance(adapter_b, GenericUBLAdapter)


if __name__ == "__main__":
    unittest.main()
