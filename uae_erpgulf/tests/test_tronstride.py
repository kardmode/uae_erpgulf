"""
Unit tests for TronStride FZC (Aigentrix) ASP Adapter.
Validates dynamic factory resolution, payload transformation with PINT-AE UBL 2.1 XML,
API authentication, submission handling, and webhook event parsing.
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

from uae_erpgulf.adapters.factory import ASPFactory
from uae_erpgulf.adapters.tronstride import TronstrideASPAdapter


# Credentials are encrypted Password fields; these mock tests read them off the mocked Company
import uae_erpgulf.core.credentials as _credentials


def _mock_company_secret(company, fieldname):
    value = getattr(frappe_mock.get_cached_doc("Company", company), fieldname, None)
    return value if isinstance(value, str) else None


if not REAL_FRAPPE:
    _credentials.get_company_secret = _mock_company_secret
from uae_erpgulf.adapters.base import classify_reporting_status


@unittest.skipIf(REAL_FRAPPE, "standalone mock tests: run with python -m unittest, not bench")
class TestTronstrideAdapter(unittest.TestCase):
    def setUp(self):
        self.mock_company = MagicMock()
        self.mock_company.name = "Al Andalus LLC"
        self.mock_company.tax_id = "100234567890003"
        self.mock_company.custom_accredited_service_providers = "TronStride FZC"
        self.mock_company.custom_base_url = "https://sandbox-api.aigentrix.ai"
        self.mock_company.custom_asp_api_key = "tron_sandbox_key_998877"
        self.mock_company.custom_participant_id = "0235:100234567890003"

        frappe_mock.get_cached_doc.return_value = self.mock_company

    def test_factory_resolves_tronstride(self):
        """Validates that TronStride FZC resolves TronstrideASPAdapter."""
        adapter = ASPFactory.get_adapter("Al Andalus LLC")
        self.assertIsInstance(adapter, TronstrideASPAdapter)
        self.assertEqual(adapter.base_url, "https://sandbox-api.aigentrix.ai")

    def test_factory_resolves_aigentrix_alias(self):
        """Validates that alias 'aigentrix' also resolves TronstrideASPAdapter."""
        self.mock_company.custom_accredited_service_providers = "aigentrix"
        adapter = ASPFactory.get_adapter("Al Andalus LLC")
        self.assertIsInstance(adapter, TronstrideASPAdapter)

    def test_transform_payload(self):
        """Validates payload transformation with PINT-AE UBL 2.1 XML inclusion."""
        adapter = TronstrideASPAdapter("Al Andalus LLC")
        canonical_doc = {
            "document_type": "388",
            "document_identifier": "INV-2026-901",
            "issue_date": "2026-10-25",
            "issue_time": "14:00:00",
            "document_currency": "AED",
            "seller": {
                "tax_id": "100234567890003",
                "legal_name": "Al Andalus LLC",
                "street_address": "Business Bay",
                "city": "Dubai",
                "country_code": "AE",
            },
            "buyer": {
                "tax_id": "100987654321003",
                "legal_name": "Client Corp",
                "street_address": "Sheikh Zayed Rd",
                "city": "Dubai",
                "country_code": "AE",
            },
            "lines": [
                {
                    "idx": 1,
                    "item_code": "SRV-CONSULT",
                    "item_name": "Advisory Fee",
                    "quantity": 1,
                    "uom": "NOS",
                    "rate": 1000.0,
                    "line_amount": 1000.0,
                    "tax_category": "S",
                    "tax_rate": 5.0,
                }
            ],
            "monetary_totals": {
                "line_extension_amount": 1000.0,
                "tax_exclusive_amount": 1000.0,
                "tax_inclusive_amount": 1050.0,
                "payable_amount": 1050.0,
                "charge_total_amount": 50.0,
            },
            "taxes": [
                {
                    "tax_category": "S",
                    "tax_rate": 5.0,
                    "taxable_amount": 1000.0,
                    "tax_amount": 50.0,
                }
            ]
        }

        payload = adapter.transform_payload(canonical_doc)
        self.assertEqual(payload["document_id"], "INV-2026-901")
        self.assertEqual(payload["supplier_trn"], "100234567890003")
        self.assertEqual(payload["total_amount"], 1050.0)
        self.assertIn("ubl_xml", payload)
        self.assertIn("<Invoice", payload["ubl_xml"])
        self.assertIn("urn:oasis:names:specification:ubl:schema:xsd:Invoice-2", payload["ubl_xml"])

    @patch("requests.post")
    def test_submit_document_success(self, mock_post):
        """Validates successful clearance submission to TronStride API."""
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {
            "status": "success",
            "data": {
                "document_id": "TRON-DOC-998877",
                "status": "cleared",
                "exchange_status": "delivered",
                "peppol_id": "0235:100234567890003",
            }
        }
        mock_post.return_value = mock_resp

        adapter = TronstrideASPAdapter("Al Andalus LLC")
        canonical_doc = {
            "document_type": "388",
            "document_identifier": "INV-2026-902",
            "seller": {"tax_id": "100234567890003"},
            "buyer": {"tax_id": "100987654321003"},
            "monetary_totals": {"payable_amount": 105.0},
            "lines": []
        }

        result = adapter.submit_document(canonical_doc)
        self.assertTrue(result["success"])
        self.assertEqual(result["document_id"], "TRON-DOC-998877")
        self.assertEqual(classify_reporting_status(result["reporting_status"]), "reported")

    @patch("requests.get")
    def test_connection_success(self, mock_get):
        """Validates test_connection() against TronStride ping endpoint."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp

        adapter = TronstrideASPAdapter("Al Andalus LLC")
        ok, msg = adapter.test_connection()
        self.assertTrue(ok)
        self.assertIn("authenticated successfully", msg)

    def test_parse_webhook_payload(self):
        """Validates parsing of TronStride inbound webhook event."""
        adapter = TronstrideASPAdapter("Al Andalus LLC")
        webhook_json = b'''{
            "event": "invoice.cleared",
            "participant_id": "0235:100234567890003",
            "data": {
                "document_id": "TRON-DOC-554433",
                "invoice_number": "INV-2026-999",
                "status": "cleared",
                "exchange_status": "delivered"
            }
        }'''

        event = adapter.parse_webhook_payload(webhook_json, {})
        self.assertEqual(event["event_type"], "invoice.cleared")
        self.assertEqual(event["document_id"], "TRON-DOC-554433")
        self.assertEqual(event["invoice_number"], "INV-2026-999")
        self.assertEqual(event["status"], "cleared")


if __name__ == "__main__":
    unittest.main()
