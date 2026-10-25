"""
Unit tests for the Wafeq ASP adapter, against the documented API
(https://docs.wafeq.com/uae-einvoicing): payload mapping, headers and idempotency,
submission outcomes, FTA reporting status, and Standard Webhooks verification.
"""

import base64
import hashlib
import hmac
import json
import sys
import time
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
from uae_erpgulf.adapters.wafeq import WafeqASPAdapter, normalize_wafeq_event, verify_standard_webhook


# Credentials are encrypted Password fields; these mock tests read them off the mocked Company
import uae_erpgulf.core.credentials as _credentials


def _mock_company_secret(company, fieldname):
    value = getattr(frappe_mock.get_cached_doc("Company", company), fieldname, None)
    return value if isinstance(value, str) else None


if not REAL_FRAPPE:
    _credentials.get_company_secret = _mock_company_secret
from uae_erpgulf.adapters.base import classify_reporting_status


def _response(status_code, body):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = body
    response.text = json.dumps(body)
    response.content = json.dumps(body).encode()
    return response


def _canonical(**overrides):
    doc = {
        "document_type": "380",
        "document_identifier": "SINV-0001",
        "source_id": "SINV-0001",
        "issue_date": "2026-10-25",
        "due_date": "2026-11-24",
        "document_currency": "AED",
        "transaction_type_code": "00000000",
        "buyer_reference": "PO-778",
        "seller": {"legal_name": "Seller LLC", "tax_id": "100234567890003", "peppol_id": "0235:1002345678"},
        "buyer": {
            "legal_name": "Gulf Fresh Foods LLC",
            "tax_id": "199920000000003",
            "peppol_id": "0235:1999200000",
            "street_address": "Warehouse 7, Al Quoz",
            "city": "Dubai",
            "country_code": "AE",
            "emirate_code": "DXB",
            "legal_registration": {"type": "TL", "id": "CN-7654321"},
        },
        "lines": [
            {"name": "Chair", "description": "Office chair", "quantity": 3.0, "unit_price": 33.33,
             "line_extension_amount": 100.0, "unit_code": "H87", "vat_category": "S", "vat_percentage": 5.0},
            {"name": "Rent", "description": "Flat rent", "quantity": 1.0, "unit_price": 8000.0,
             "line_extension_amount": 8000.0, "unit_code": "MON", "vat_category": "E", "vat_percentage": 0.0,
             "exemption_reason_code": "DL8.46.2", "exemption_reason": "Supply of residential units"},
            {"name": "Export", "description": "Zero rated", "quantity": 2.0, "unit_price": 10.0,
             "line_extension_amount": 20.0, "unit_code": "H87", "vat_category": "Z", "vat_percentage": 0.0},
        ],
        "payment_means": [{"payment_means_code": "30", "payee_financial_account": {"iban": "AE070331234567890123456"}}],
        "monetary_totals": {"payable_amount": 8125.0},
    }
    doc.update(overrides)
    return doc


@unittest.skipIf(REAL_FRAPPE, "standalone mock tests: run with python -m unittest, not bench")
class TestWafeqAdapter(unittest.TestCase):
    def setUp(self):
        self.mock_company = MagicMock()
        self.mock_company.name = "Al Andalus LLC"
        self.mock_company.custom_accredited_service_providers = "Wafeq"
        self.mock_company.custom_base_url = None
        self.mock_company.custom_asp_api_key = "pk_test_sample_wafeq_secret_key"
        self.mock_company.custom_wafeq_taxpayer_id = "txpay_0123456789abcdef"
        self.mock_company.custom_wafeq_grant_id = "tgr_0123456789"
        frappe_mock.get_cached_doc.return_value = self.mock_company
        self.adapter = WafeqASPAdapter("Al Andalus LLC")

    def test_factory_resolves_wafeq(self):
        self.assertIsInstance(ASPFactory.get_adapter("Al Andalus LLC"), WafeqASPAdapter)

    def test_headers_carry_key_taxpayer_grant_and_idempotency(self):
        headers = self.adapter._get_headers(idempotency_key="SINV-0001-abc")
        self.assertEqual(headers["Authorization"], "Api-Key pk_test_sample_wafeq_secret_key")
        self.assertEqual(headers["x-ae-taxpayer-id"], "txpay_0123456789abcdef")
        self.assertEqual(headers["x-ae-grant-id"], "tgr_0123456789")
        self.assertEqual(headers["Idempotency-Key"], "SINV-0001-abc")
        self.assertEqual(headers["Prefer"], "respond-async")
        self.assertNotIn("Prefer", self.adapter._get_headers())

    def test_invoice_payload_matches_wafeq_schema(self):
        payload = self.adapter.transform_payload(_canonical())

        # Wafeq derives these itself: never sent
        for key in ("seller", "supplier", "totals", "document_type_code", "legal_monetary_total"):
            self.assertNotIn(key, payload)
        self.assertEqual(payload["invoice_number"], "SINV-0001")
        self.assertEqual(payload["due_date"], "2026-11-24")
        self.assertEqual(payload["buyer_reference"], "PO-778")
        self.assertEqual(payload["tax_amount_type"], "TAX_EXCLUSIVE")
        self.assertNotIn("transaction_type_code", payload)  # all-zero flags are the default
        self.assertNotIn("currency", payload)
        self.assertEqual(payload["payment_means"], {"code": "30", "iban": "AE070331234567890123456"})

        customer = payload["customer"]
        self.assertEqual(customer["id"], "0235:1999200000")
        self.assertEqual(customer["vat_number"], "199920000000003")
        self.assertEqual(customer["address"]["country_subentity"], "DXB")
        self.assertEqual(customer["legal_registration"], {"type": "TL", "id": "CN-7654321"})

        standard, exempt, zero = payload["line_items"]
        self.assertEqual(standard["tax_percent"], 5.0)
        self.assertAlmostEqual(standard["quantity"] * standard["unit_price"], 100.0, places=4)
        self.assertEqual(exempt["tax_percent"], 0.0)
        self.assertEqual(exempt["tax_exemption_reason_code"], "DL8.46.2")
        self.assertEqual(exempt["unit_code"], "MON")
        self.assertEqual(zero["tax_category"], "Z")
        self.assertEqual(zero["tax_percent"], 0.0)

    def test_foreign_currency_sends_rate_as_string(self):
        payload = self.adapter.transform_payload(_canonical(document_currency="USD", currency_exchange_rate=3.6725))
        self.assertEqual(payload["currency"], "USD")
        self.assertEqual(payload["currency_exchange_rate"], "3.6725")

    def test_credit_note_references_original_through_wafeq(self):
        cn = _canonical(
            document_type="381",
            credit_note_reason_code="DL8.61.1.D",
            billing_reference={"id": "SINV-0001", "issue_date": "2026-10-20", "document_id": "inv_ABC"},
        )
        payload = self.adapter.transform_payload(cn)
        self.assertEqual(payload["credit_note_number"], "SINV-0001")
        self.assertNotIn("due_date", payload)
        self.assertEqual(payload["previous_invoice_source"], "ap")
        self.assertEqual(payload["previous_document_id"], "inv_ABC")
        self.assertEqual(payload["credit_note_reason_code"], "DL8.61.1.D")
        self.assertEqual(WafeqASPAdapter._endpoint("381"), "/credit-notes")

    def test_credit_note_for_external_original(self):
        payload = self.adapter.transform_payload(
            _canonical(document_type="381", billing_reference={"id": "OLD-9", "issue_date": "2025-12-01", "document_id": None})
        )
        self.assertEqual(payload["previous_invoice_source"], "external")
        self.assertEqual(payload["previous_invoice_number"], "OLD-9")
        self.assertEqual(payload["previous_invoice_issue_date"], "2025-12-01")

    def test_self_billed_invoice_sends_supplier(self):
        payload = self.adapter.transform_payload(_canonical(document_type="389"))
        self.assertIn("supplier", payload)
        self.assertNotIn("customer", payload)
        self.assertEqual(payload["supplier"]["name"], "Seller LLC")
        self.assertEqual(WafeqASPAdapter._endpoint("389"), "/self-billing/invoices")
        self.assertEqual(WafeqASPAdapter._endpoint("261"), "/self-billing/credit-notes")

    @patch("requests.post")
    def test_queued_submission_is_accepted_but_not_reported(self, mock_post):
        mock_post.return_value = _response(202, {"success": True, "status": "queued", "document_id": "inv_Q1"})
        result = self.adapter.submit_document(_canonical())

        self.assertTrue(result["success"])
        self.assertEqual(result["document_id"], "inv_Q1")
        self.assertEqual(classify_reporting_status(result["reporting_status"]), "pending")
        self.assertTrue(mock_post.call_args[0][0].endswith("/invoices"))

    @patch("requests.post")
    def test_same_document_reuses_idempotency_key(self, mock_post):
        mock_post.return_value = _response(202, {"success": True, "status": "queued", "document_id": "inv_Q1"})
        self.adapter.submit_document(_canonical())
        self.adapter.submit_document(_canonical())
        first, second = (c.kwargs["headers"]["Idempotency-Key"] for c in mock_post.call_args_list)
        self.assertEqual(first, second)

        self.adapter.submit_document(_canonical(buyer_reference="PO-999"))  # changed body -> new key
        self.assertNotEqual(mock_post.call_args.kwargs["headers"]["Idempotency-Key"], first)

    @patch("requests.post")
    def test_failed_delivery_with_document_id_is_still_accepted(self, mock_post):
        mock_post.return_value = _response(200, {"success": False, "status": "failed", "document_id": "inv_F1"})
        result = self.adapter.submit_document(_canonical())
        self.assertTrue(result["success"])  # reported to the FTA; must never be resubmitted
        self.assertEqual(result["document_id"], "inv_F1")

    @patch("requests.post")
    def test_validation_refusal_lists_rule_codes(self, mock_post):
        mock_post.return_value = _response(200, {
            "success": False, "status": "validation_failed", "document_id": None, "error": "Invalid document",
            "validation_errors": [{"code": "SCH:ibr-167-ae", "message": "Exempt line needs a reason code"}],
        })
        result = self.adapter.submit_document(_canonical())
        self.assertFalse(result["success"])
        self.assertFalse(result["retryable"])
        self.assertIn("SCH:ibr-167-ae", result["error_message"])

    @patch("requests.post")
    def test_temporary_errors_are_retryable(self, mock_post):
        for status_code, body in ((503, {"detail": {"code": "smp_unavailable"}}),
                                  (409, {"detail": {"code": "idempotency_in_progress"}}),
                                  (429, {"error": "Rate limit exceeded: 60 per 1 minute"})):
            mock_post.return_value = _response(status_code, body)
            self.assertTrue(self.adapter.submit_document(_canonical())["retryable"], status_code)

    @patch("requests.get")
    @patch("requests.post")
    def test_already_submitted_number_is_looked_up(self, mock_post, mock_get):
        mock_post.return_value = _response(409, {"detail": {"code": "document_already_submitted"}})
        mock_get.return_value = _response(200, {"items": [{"id": "inv_OLD", "invoice_number": "SINV-0001"}]})
        result = self.adapter.submit_document(_canonical())
        self.assertTrue(result["success"])
        self.assertEqual(result["document_id"], "inv_OLD")

    @patch("requests.get")
    def test_status_uses_fta_report_not_delivery(self, mock_get):
        mock_get.return_value = _response(200, {"id": "inv_1", "delivery_status": "delivered_confirmed", "tdd_status": "reporting"})
        self.assertEqual(classify_reporting_status(self.adapter.get_document_status("inv_1")["reporting_status"]), "pending")

        mock_get.return_value = _response(200, {"id": "inv_1", "delivery_status": "sent", "tdd_status": "reported"})
        self.assertEqual(classify_reporting_status(self.adapter.get_document_status("inv_1")["reporting_status"]), "reported")

        mock_get.return_value = _response(200, {"id": "inv_1", "delivery_status": "rejected",
                                                "delivery_reason_code": "BV", "tdd_status": "withdrawn"})
        self.assertEqual(classify_reporting_status(self.adapter.get_document_status("inv_1")["reporting_status"]), "failed")

    @patch("requests.get")
    def test_connection_reads_registered_profile(self, mock_get):
        mock_get.return_value = _response(200, {"name": "Al Andalus LLC"})
        ok, message = self.adapter.test_connection()
        self.assertTrue(ok)
        self.assertIn("Sandbox", message)
        self.assertTrue(mock_get.call_args[0][0].endswith("/settings/profile"))


@unittest.skipIf(REAL_FRAPPE, "standalone mock tests: run with python -m unittest, not bench")
class TestWafeqWebhooks(unittest.TestCase):
    SECRET = "whsec_" + base64.b64encode(b"wafeq-test-signing-key").decode()

    def _signed(self, body: bytes, msg_id="msg_1", timestamp=None):
        timestamp = str(int(timestamp or time.time()))
        key = base64.b64decode(self.SECRET[len("whsec_"):])
        signed = f"{msg_id}.{timestamp}.".encode() + body
        signature = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
        return {"webhook-id": msg_id, "webhook-timestamp": timestamp, "webhook-signature": f"v1,{signature}"}

    def test_valid_signature_is_accepted(self):
        body = b'{"type": "document.reported"}'
        self.assertTrue(verify_standard_webhook(self.SECRET, body, self._signed(body)))

    def test_tampered_body_wrong_secret_and_old_timestamp_are_rejected(self):
        body = b'{"type": "document.reported"}'
        headers = self._signed(body)
        self.assertFalse(verify_standard_webhook(self.SECRET, body + b" ", headers))
        self.assertFalse(verify_standard_webhook("whsec_" + base64.b64encode(b"other").decode(), body, headers))
        self.assertFalse(verify_standard_webhook(self.SECRET, body, self._signed(body, timestamp=time.time() - 3600)))
        self.assertFalse(verify_standard_webhook(self.SECRET, body, {}))

    def test_reported_events_classify_by_fta_status(self):
        def event(status):
            return {"id": "evt_1", "type": "document.reported",
                    "data": {"document_id": "inv_1", "taxpayer_id": "txpay_1", "tax_authority_status": status}}

        self.assertEqual(classify_reporting_status(normalize_wafeq_event(event("REPORTED"))["reporting_status"]), "reported")
        self.assertEqual(classify_reporting_status(normalize_wafeq_event(event("NOT_COMPLIANT"))["reporting_status"]), "failed")
        self.assertEqual(classify_reporting_status(normalize_wafeq_event(event("WITHDRAWN"))["reporting_status"]), "failed")

    def test_delivery_events_do_not_report(self):
        event = {"type": "document.status", "data": {"document_id": "inv_1", "delivery_status": "delivered_confirmed"}}
        self.assertIsNone(normalize_wafeq_event(event)["reporting_status"])


if __name__ == "__main__":
    unittest.main()
