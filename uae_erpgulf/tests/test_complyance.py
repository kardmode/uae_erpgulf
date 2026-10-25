"""
Unit tests for Complyance.io (Complyance Electronics L.L.C) ASP Adapter.
Validates dynamic factory resolution, payload transformation for GETS UAE JSON,
API authentication headers, document submission, connection testing, and webhook parsing.
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
from uae_erpgulf.adapters.complyance import ComplyanceASPAdapter


# Credentials are encrypted Password fields; these mock tests read them off the mocked Company
import uae_erpgulf.core.credentials as _credentials


def _mock_company_secret(company, fieldname):
    value = getattr(frappe_mock.get_cached_doc("Company", company), fieldname, None)
    return value if isinstance(value, str) else None


if not REAL_FRAPPE:
    _credentials.get_company_secret = _mock_company_secret
from uae_erpgulf.adapters.base import classify_reporting_status


@unittest.skipIf(REAL_FRAPPE, "standalone mock tests: run with python -m unittest, not bench")
class TestComplyanceAdapter(unittest.TestCase):
    def setUp(self):
        self.mock_company = MagicMock()
        self.mock_company.name = "Al Andalus LLC"
        self.mock_company.tax_id = "100234567890003"
        self.mock_company.custom_accredited_service_providers = "Complyance Electronics L.L.C"
        self.mock_company.custom_base_url = "https://sandbox-api.complyance.io/v1"
        self.mock_company.custom_asp_api_key = "cmp_test_token_998877"
        self.mock_company.custom_participant_id = "0235:100234567890003"

        frappe_mock.get_cached_doc.return_value = self.mock_company

    def test_factory_resolves_complyance(self):
        """Validates that 'Complyance Electronics L.L.C' resolves to ComplyanceASPAdapter."""
        adapter = ASPFactory.get_adapter("Al Andalus LLC")
        self.assertIsInstance(adapter, ComplyanceASPAdapter)
        self.assertEqual(adapter.base_url, "https://sandbox-api.complyance.io/v1")

    def test_headers_include_bearer_and_idempotency(self):
        """Validates that Complyance authentication and header contracts are met."""
        adapter = ComplyanceASPAdapter("Al Andalus LLC")
        headers = adapter._get_headers(idempotency_key="INV-2026-0001")

        self.assertEqual(headers["Authorization"], "Bearer cmp_test_token_998877")
        self.assertEqual(headers["Idempotency-Key"], "INV-2026-0001")
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_transform_payload_structure(self):
        """Validates canonical to Complyance GETS AE JSON transformation."""
        adapter = ComplyanceASPAdapter("Al Andalus LLC")
        canonical_doc = {
            "document_type": "380",
            "document_identifier": "SLI-SINV-202600001",
            "icv": 105,
            "issue_date": "2026-10-25",
            "due_date": "2026-11-25",
            "document_currency": "AED",
            "seller": {
                "tax_id": "100234567890003",
                "legal_name": "Al Andalus LLC",
                "address": {"city": "Dubai", "country": "AE"}
            },
            "buyer": {
                "tax_id": "100999888777003",
                "legal_name": "Dubai Marina Retail LLC",
                "address": {
                    "street": "Al Sufouh Rd",
                    "city": "Dubai",
                    "state": "Dubai",
                    "country": "AE"
                }
            },
            "lines": [
                {
                    "item_code": "DESK-OAK",
                    "item_name": "Executive Oak Desk",
                    "quantity": 2.0,
                    "unit_price": 1500.0,
                    "unit_code": "H87",
                    "tax_category": "S",
                    "tax_percent": 5.0,
                    "tax_amount": 150.0,
                    "line_extension_amount": 3000.0
                }
            ],
            "monetary_totals": {
                "tax_exclusive_amount": 3000.0,
                "tax_inclusive_amount": 3150.0,
                "charge_total_amount": 150.0,
                "payable_amount": 3150.0
            }
        }

        payload = adapter.transform_payload(canonical_doc)

        self.assertEqual(payload["country"], "AE")
        self.assertEqual(payload["format"], "GETS")
        self.assertEqual(payload["document_type"], "TAX_INVOICE")
        self.assertEqual(payload["invoice_number"], "SLI-SINV-202600001")
        self.assertEqual(payload["supplier"]["trn"], "100234567890003")
        self.assertEqual(payload["buyer"]["trn"], "100999888777003")
        self.assertEqual(len(payload["line_items"]), 1)
        self.assertEqual(payload["line_items"][0]["tax_rate"], 5.0)
        self.assertEqual(payload["totals"]["payable_amount"], 3150.0)

    def test_transform_credit_note_type(self):
        """Validates that document_type 381 maps to CREDIT_NOTE in GETS schema."""
        adapter = ComplyanceASPAdapter("Al Andalus LLC")
        canonical_doc = {
            "document_type": "381",
            "document_identifier": "SLI-CRN-202600001",
            "lines": []
        }
        payload = adapter.transform_payload(canonical_doc)
        self.assertEqual(payload["document_type"], "CREDIT_NOTE")

    @patch("requests.post")
    def test_submit_invoice_cleared(self, mock_post):
        """Validates successful submission handling from Complyance.io."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "success": True,
            "id": "cmp_inv_0123456789",
            "status": "cleared",
            "document_id": "cmp_inv_0123456789"
        }
        mock_post.return_value = mock_resp

        adapter = ComplyanceASPAdapter("Al Andalus LLC")
        canonical_doc = {
            "document_type": "380",
            "document_identifier": "SLI-SINV-202600001",
            "lines": []
        }

        result = adapter.submit_document(canonical_doc)

        self.assertTrue(result["success"])
        self.assertEqual(result["document_id"], "cmp_inv_0123456789")
        self.assertEqual(classify_reporting_status(result["reporting_status"]), "reported")

    @patch("requests.get")
    def test_test_connection_success(self, mock_get):
        """Validates test_connection() against Complyance auth endpoint."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp

        adapter = ComplyanceASPAdapter("Al Andalus LLC")
        success, msg = adapter.test_connection()

        self.assertTrue(success)
        self.assertIn("authenticated successfully", msg)

    def test_parse_webhook_payload(self):
        """Validates parsing of Complyance.io inbound webhook event."""
        adapter = ComplyanceASPAdapter("Al Andalus LLC")
        raw_json = b"""{
            "event": "invoice.cleared",
            "data": {
                "id": "cmp_inv_0123456789",
                "invoice_number": "SLI-SINV-202600001",
                "status": "cleared"
            }
        }"""
        parsed = adapter.parse_webhook_payload(raw_json, {})

        self.assertEqual(parsed["event_type"], "invoice.cleared")
        self.assertEqual(parsed["document_id"], "cmp_inv_0123456789")
        self.assertEqual(parsed["invoice_number"], "SLI-SINV-202600001")
        self.assertEqual(classify_reporting_status(parsed["reporting_status"]), "reported")


if __name__ == "__main__":
    unittest.main()
