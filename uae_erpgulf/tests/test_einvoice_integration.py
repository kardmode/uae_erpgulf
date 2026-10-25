"""
Integration tests for the UAE e-invoice submission engine against a real Frappe/ERPNext site.

Unlike the other modules in this folder (which replace `frappe` with a MagicMock), these run
against real DocType meta and a real database, so missing custom fields, wrong column names
and ERPNext calculation mismatches fail here.

Run on a test site with ERPNext and uae_erpgulf installed:
    bench --site test_site run-tests --module uae_erpgulf.tests.test_einvoice_integration

Run this module on its own: the mock-based modules here overwrite sys.modules["frappe"] on
import, so loading them in the same process breaks real-database tests.
"""

import hashlib
import hmac
import json
import re
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

try:
    import frappe
except ImportError:  # plain `python -m unittest` without a bench
    raise unittest.SkipTest("Requires a Frappe site (bench run-tests)")

if isinstance(frappe, MagicMock):
    raise unittest.SkipTest("frappe is mocked by another test module in this process")

import requests
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, add_to_date, getdate, now_datetime, nowdate

from uae_erpgulf.adapters import factory
from uae_erpgulf.adapters.flick import FlickASPAdapter
from uae_erpgulf.adapters.generic_ubl import GenericUBLAdapter
from uae_erpgulf.adapters.wafeq import WafeqASPAdapter
from uae_erpgulf.core.canonical import build_canonical_uae_invoice
from uae_erpgulf.core.credentials import SECRET_FIELDS, get_company_secret, get_flick_headers, set_company_secret
from uae_erpgulf.uae_erpgulf import queue, verify_token, webhook

APP_DIR = Path(__file__).resolve().parent.parent

COMPANY = "_Test UAE E-Invoice Co"
ABBR = "_TUAE"
CUSTOMER = "_Test UAE E-Invoice Customer"
ITEM = "_Test UAE E-Invoice Service"

# Every field the outbound engine reads or writes, by DocType
ENGINE_FIELDS = {
    "Company": [
        "custom_uae_einvoice_enabled",
        "custom_einvoice_go_live_date",
        "custom_accredited_service_providers",
        "custom_base_url",
        "custom_participant_id",
        *SECRET_FIELDS,
        "custom_webhook_signature_header",
        "custom_wafeq_taxpayer_id",
        "custom_wafeq_grant_id",
        "custom_legal_registration_identifier_type",
        "custom_trade_license_number",
    ],
    "Sales Invoice": [
        "custom_uae_einvoice_status",
        "custom_reporting_status",
        "custom_document_id",
        "custom_submit_response",
        "custom_einvoice_retry_count",
        "custom_einvoice_next_retry",
        "custom_vat_category",
        "custom_frequency_billing_code_list",
        "custom_invoice_note",
        "custom_return_against_for_uae_einvoice",
    ],
    "Purchase Invoice": [
        "custom_uae_einvoice_status",
        "custom_reporting_status",
        "custom_document_id",
        "custom_submit_response",
        "custom_einvoice_retry_count",
        "custom_einvoice_next_retry",
        "custom_vat_category",
        "custom_submit_to_fta",
    ],
    "Sales Invoice Item": ["custom_item_type_codes", "custom_hs_code_", "custom_sac_code"],
    "Purchase Invoice Item": ["custom_item_type_codes", "custom_hs_code_", "custom_sac_code"],
    "Customer": ["custom_trade_license_number", "custom_peppol_id", "custom_fz_beneficiary_id"],
    "Supplier": ["custom_trade_license_number", "custom_peppol_id", "custom_fz_beneficiary_id"],
    "Address": ["emirate"],
    "UOM": ["custom_un_ece_unit_code"],
    "Item Tax Template": ["custom_vat_category"],
    "Mode of Payment": ["custom_payment_means_codes"],
}

# Source files on the path every outbound invoice takes
ENGINE_SOURCES = [
    "core/canonical.py",
    "uae_erpgulf/queue.py",
    "adapters/base.py",
    "adapters/factory.py",
    "adapters/flick.py",
    "adapters/wafeq.py",
]

# Referenced deliberately as optional fallbacks, so absent by design
OPTIONAL_FIELD_REFERENCES = {
    "custom_hs_code",  # legacy alias read after custom_hs_code_
}


class TestEInvoiceFieldContract(FrappeTestCase):
    def test_engine_fields_exist(self):
        """Every field the engine depends on exists on its DocType."""
        missing = [
            f"{doctype}.{fieldname}"
            for doctype, fieldnames in ENGINE_FIELDS.items()
            for fieldname in fieldnames
            if not frappe.get_meta(doctype).has_field(fieldname)
        ]
        self.assertEqual(missing, [], f"Custom fields missing from the site: {missing}")

    def test_engine_sources_reference_only_existing_fields(self):
        """Any custom_* field named in the engine source exists on some engine DocType."""
        known = {
            df.fieldname
            for doctype in ENGINE_FIELDS
            for df in frappe.get_meta(doctype).fields
        }
        pattern = re.compile(r"""["'.](custom_[a-z0-9_]+)\b""")
        unknown = {}
        for rel_path in ENGINE_SOURCES:
            source = (APP_DIR / rel_path).read_text(encoding="utf-8")
            refs = set(pattern.findall(source)) - known - OPTIONAL_FIELD_REFERENCES
            if refs:
                unknown[rel_path] = sorted(refs)
        self.assertEqual(unknown, {}, f"Engine references fields that do not exist: {unknown}")


class TestEInvoiceEngine(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _ensure_fiscal_year()
        _ensure_company()
        cls.vat_account = _ensure_vat_account()
        _ensure_address("Company", COMPANY, "_Test UAE Co HQ", "Dubai", is_company=True)
        _ensure_customer()
        _ensure_address("Customer", CUSTOMER, "_Test UAE Customer Office", "Abu Dhabi")
        _ensure_item()

    def setUp(self):
        frappe.clear_document_cache("Company", COMPANY)

    # --- Canonical model -------------------------------------------------------------------

    def test_sales_invoice_canonical_uses_real_addresses_and_erpnext_totals(self):
        si = make_sales_invoice(qty=3, rate=100, discount_amount=30)
        canonical = build_canonical_uae_invoice(si)

        seller, buyer = canonical["seller"], canonical["buyer"]
        self.assertEqual(seller["street_address"], "1 _Test UAE Co HQ Street")
        self.assertEqual(seller["city"], "Dubai")
        self.assertEqual(seller["emirate_code"], "DXB")
        self.assertEqual(seller["country_code"], "AE")
        self.assertEqual(buyer["emirate_code"], "AUH")

        totals = canonical["monetary_totals"]
        # 300 less the 30 header discount is distributed into the line net amount
        self.assertEqual(totals["line_extension_amount"], 270.0)
        self.assertEqual(totals["tax_exclusive_amount"], 270.0)
        self.assertEqual(totals["tax_amount"], 13.5)
        self.assertAlmostEqual(totals["tax_amount"], si.total_taxes_and_charges)
        self.assertAlmostEqual(totals["tax_inclusive_amount"], si.grand_total)
        self.assertAlmostEqual(totals["payable_amount"], si.rounded_total or si.grand_total)
        self.assertEqual(canonical["tax_subtotals"], [
            {"category": "S", "rate": 5.0, "taxable_amount": 270.0, "tax_amount": 13.5}
        ])

    def test_peppol_ids_use_the_10_digit_tin(self):
        canonical = build_canonical_uae_invoice(make_sales_invoice())

        # 0235 + the first 10 digits of the 15-digit TRN (MoF mandatory fields: electronic address = TIN)
        self.assertEqual(canonical["seller"]["tin"], "1002345678")
        self.assertEqual(canonical["seller"]["peppol_id"], "0235:1002345678")
        self.assertEqual(canonical["buyer"]["peppol_id"], "0235:1009876543")
        self.assertNotIn("icv", canonical)

    def test_lines_carry_unit_codes(self):
        line = build_canonical_uae_invoice(make_sales_invoice())["lines"][0]
        self.assertEqual(line["unit_code"], "H87")  # "Nos"

    def test_credit_note_has_positive_amounts_and_original_date(self):
        from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return

        original = make_sales_invoice(qty=2, rate=100, posting_date=add_days(nowdate(), -1), submit=True)
        credit_note = make_sales_return(original.name)
        credit_note.posting_date = nowdate()
        credit_note.set_posting_time = 1
        credit_note.insert()

        canonical = build_canonical_uae_invoice(credit_note)

        self.assertEqual(canonical["document_type"], "381")
        self.assertEqual(canonical["monetary_totals"]["tax_exclusive_amount"], 200.0)
        self.assertEqual(canonical["monetary_totals"]["tax_amount"], 10.0)
        self.assertEqual(canonical["monetary_totals"]["payable_amount"], 210.0)
        self.assertEqual(canonical["lines"][0]["quantity"], 2.0)
        self.assertEqual(canonical["lines"][0]["line_extension_amount"], 200.0)
        self.assertEqual(canonical["billing_reference"], {
            "id": original.name,
            "issue_date": getdate(original.posting_date).isoformat(),
        })

    def test_incomplete_seller_address_is_rejected(self):
        incomplete = _ensure_address("Company", COMPANY, "_Test UAE Co No Emirate", None, is_company=True)
        si = make_sales_invoice(company_address=incomplete)

        with self.assertRaises(frappe.ValidationError) as ctx:
            build_canonical_uae_invoice(si)
        self.assertIn("Emirate", str(ctx.exception))

    def test_vat_category_that_disagrees_with_erpnext_tax_is_rejected(self):
        si = make_sales_invoice(vat_category="Z - Zero rated")  # but a 5% VAT row is charged

        with self.assertRaises(frappe.ValidationError):
            build_canonical_uae_invoice(si)

    def test_zero_rated_line_keeps_zero_percent_in_flick_payload(self):
        si = make_sales_invoice(vat_category="Z - Zero rated", with_vat=False)
        canonical = build_canonical_uae_invoice(si)

        flick_line = FlickASPAdapter(COMPANY).transform_payload(canonical)["document"]["invoice_lines"][0]
        self.assertEqual(flick_line["vat_category"], "Z")
        self.assertEqual(flick_line["vat_percentage"], 0.0)

    # --- Submission queue ------------------------------------------------------------------

    def test_ordinary_purchase_invoice_is_not_transmitted(self):
        pi = frappe.new_doc("Purchase Invoice")
        self.assertFalse(queue.should_transmit(pi))

        pi.custom_submit_to_fta = 1
        self.assertTrue(queue.should_transmit(pi))

        pi.custom_uae_einvoice_status = "Inbound Cleared"
        self.assertFalse(queue.should_transmit(pi))

    def test_network_failure_schedules_retry_then_scheduler_requeues(self):
        si = make_sales_invoice(submit=True)
        adapter = MagicMock()
        adapter.submit_document.side_effect = requests.exceptions.ConnectionError("ASP unreachable")

        with _no_transaction_control(), patch(
            "uae_erpgulf.adapters.factory.ASPFactory.get_adapter", return_value=adapter
        ):
            queue.process_einvoice_background("Sales Invoice", si.name, retry_count=0)

        status, retries, next_retry = frappe.db.get_value(
            "Sales Invoice", si.name,
            ["custom_uae_einvoice_status", "custom_einvoice_retry_count", "custom_einvoice_next_retry"],
        )
        self.assertEqual(status, "Retry Pending")
        self.assertEqual(retries, 1)
        self.assertGreater(next_retry, now_datetime())

        frappe.db.set_value("Sales Invoice", si.name, "custom_einvoice_next_retry", add_to_date(now_datetime(), minutes=-1))
        with _no_transaction_control(), patch("frappe.enqueue") as enqueue:
            queue.retry_pending_submissions()

        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.kwargs["docname"], si.name)
        self.assertEqual(enqueue.call_args.kwargs["retry_count"], 1)
        self.assertNotIn("delay", enqueue.call_args.kwargs)
        self.assertEqual(
            frappe.db.get_value("Sales Invoice", si.name, "custom_uae_einvoice_status"), "Pending Submission"
        )

    def test_queued_submission_is_polled_until_reported(self):
        si = make_sales_invoice(submit=True)
        adapter = MagicMock()
        adapter.submit_document.return_value = {
            "success": True, "status_code": 202, "document_id": "ASP-DOC-1",
            "reporting_status": "queued", "exchange_status": "queued", "response_data": {"status": "queued"},
        }
        adapter.get_document_artifacts.return_value = {"xml": None, "pdf": None}

        with _no_transaction_control(), patch(
            "uae_erpgulf.adapters.factory.ASPFactory.get_adapter", return_value=adapter
        ):
            queue.process_einvoice_background("Sales Invoice", si.name)

            status, reporting, next_poll = frappe.db.get_value(
                "Sales Invoice", si.name,
                ["custom_uae_einvoice_status", "custom_reporting_status", "custom_einvoice_next_retry"],
            )
            self.assertEqual(status, "Success")  # accepted by the ASP...
            self.assertEqual(reporting, "pending")  # ...but not reported to the FTA
            self.assertGreater(next_poll, now_datetime())
            adapter.get_document_artifacts.assert_not_called()

            # Still pending on the first poll: backs off
            frappe.db.set_value("Sales Invoice", si.name, "custom_einvoice_next_retry", add_to_date(now_datetime(), minutes=-1))
            adapter.get_document_status.return_value = {"success": True, "reporting_status": "processing", "response_data": {}}
            queue.poll_pending_reporting()
            self.assertEqual(frappe.db.get_value("Sales Invoice", si.name, "custom_reporting_status"), "pending")
            self.assertEqual(frappe.db.get_value("Sales Invoice", si.name, "custom_einvoice_retry_count"), 1)

            # Reported on the next poll
            frappe.db.set_value("Sales Invoice", si.name, "custom_einvoice_next_retry", add_to_date(now_datetime(), minutes=-1))
            adapter.get_document_status.return_value = {"success": True, "reporting_status": "cleared", "response_data": {}}
            queue.poll_pending_reporting()

        adapter.get_document_status.assert_called_with("ASP-DOC-1")
        reporting, next_poll = frappe.db.get_value(
            "Sales Invoice", si.name, ["custom_reporting_status", "custom_einvoice_next_retry"]
        )
        self.assertEqual(reporting, "reported")
        self.assertIsNone(next_poll)
        adapter.get_document_artifacts.assert_called_once_with("ASP-DOC-1")

    def test_status_button_uses_configured_adapter(self):
        si = make_sales_invoice(submit=True)
        si.db_set({"custom_uae_einvoice_status": "Success", "custom_document_id": "ASP-DOC-2",
                   "custom_reporting_status": "pending"})
        adapter = MagicMock()
        adapter.get_document_status.return_value = {
            "success": True, "reporting_status": "rejected", "reporting_reference": "FTA-REF-9", "response_data": {},
        }

        with _no_transaction_control(), patch(
            "uae_erpgulf.adapters.factory.ASPFactory.get_adapter", return_value=adapter
        ):
            response = queue.refresh_einvoice_status("Sales Invoice", si.name)

        self.assertEqual(response["data"]["reporting_status"], "failed")
        self.assertEqual(response["data"]["provider_status"], "rejected")
        self.assertEqual(response["data"]["reporting_reference"], "FTA-REF-9")
        self.assertEqual(frappe.db.get_value("Sales Invoice", si.name, "custom_uae_einvoice_status"), "Failed")

    def test_generic_ubl_never_marks_invoice_reported(self):
        si = make_sales_invoice(submit=True)

        with _no_transaction_control(), patch(
            "uae_erpgulf.adapters.factory.ASPFactory.get_adapter", return_value=GenericUBLAdapter(COMPANY)
        ):
            queue.process_einvoice_background("Sales Invoice", si.name)

        self.assertEqual(frappe.db.get_value("Sales Invoice", si.name, "custom_uae_einvoice_status"), "Failed")

    def test_reported_invoice_cannot_be_resubmitted_or_cancelled(self):
        si = make_sales_invoice(submit=True)
        si.db_set("custom_uae_einvoice_status", "Success")

        with self.assertRaises(frappe.ValidationError):
            queue.manual_retry_submission("Sales Invoice", si.name)
        with self.assertRaises(frappe.ValidationError):
            si.reload()
            si.cancel()

    def test_bulk_send_queues_through_background_path(self):
        from uae_erpgulf.uae_erpgulf import test as sales_bulk

        draft = make_sales_invoice()
        submitted = make_sales_invoice(submit=True)
        reported = make_sales_invoice(submit=True)
        reported.db_set("custom_uae_einvoice_status", "Success")
        frappe.db.set_value("Company", COMPANY, "custom_uae_einvoice_enabled", 1)
        frappe.clear_document_cache("Company", COMPANY)

        try:
            with patch("frappe.enqueue") as enqueue:
                result = sales_bulk.bulk_send_invoices([draft.name, submitted.name, reported.name])
        finally:
            frappe.db.set_value("Company", COMPANY, "custom_uae_einvoice_enabled", 0)
            frappe.clear_document_cache("Company", COMPANY)

        self.assertEqual(sorted(result["success"]), sorted([draft.name, submitted.name]))
        self.assertEqual(result["failed"], [])
        self.assertTrue(any(reported.name in s for s in result["skipped"]))
        queued = sorted(c.kwargs["docname"] for c in enqueue.call_args_list)
        self.assertEqual(queued, sorted([draft.name, submitted.name]))
        self.assertEqual(frappe.db.get_value("Sales Invoice", draft.name, "docstatus"), 1)

    def test_historical_invoice_is_never_transmitted(self):
        si = make_sales_invoice(submit=True)
        si.db_set("custom_uae_einvoice_status", "Pre-Mandate / Historical")
        self.assertFalse(queue.should_transmit(si))

    def test_interrupted_send_recovery_depends_on_provider_idempotency(self):
        flick_si = make_sales_invoice(submit=True)
        flick_si.db_set({"custom_uae_einvoice_status": "Submitted to ASP",
                         "custom_einvoice_next_retry": add_to_date(now_datetime(), minutes=-1)})

        with _no_transaction_control(), patch("frappe.enqueue") as enqueue:
            queue._recover_stale_submissions("Sales Invoice")

        # Flick has no idempotency key: it may already have the invoice, so never resend blindly
        self.assertEqual(frappe.db.get_value("Sales Invoice", flick_si.name, "custom_uae_einvoice_status"), "Failed")
        enqueue.assert_not_called()

        wafeq_si = make_sales_invoice(submit=True)
        wafeq_si.db_set({"custom_uae_einvoice_status": "Submitted to ASP",
                         "custom_einvoice_next_retry": add_to_date(now_datetime(), minutes=-1)})
        frappe.db.set_value("Company", COMPANY, "custom_accredited_service_providers", "Wafeq")
        frappe.clear_document_cache("Company", COMPANY)
        try:
            with _no_transaction_control(), patch("frappe.enqueue") as enqueue:
                queue._recover_stale_submissions("Sales Invoice")
        finally:
            _ensure_company()
            frappe.clear_document_cache("Company", COMPANY)

        # Wafeq dedupes by Idempotency-Key: safe to send again
        self.assertEqual(frappe.db.get_value("Sales Invoice", wafeq_si.name, "custom_uae_einvoice_status"), "Pending Submission")
        self.assertEqual(enqueue.call_args.kwargs["docname"], wafeq_si.name)

    def test_wafeq_webhook_marks_invoice_reported(self):
        import base64

        secret = "whsec_" + base64.b64encode(b"integration-test-key").decode()
        set_company_secret(COMPANY, "custom_secret_of_webhook", secret)
        frappe.db.set_value("Company", COMPANY, {
            "custom_accredited_service_providers": "Wafeq",
            "custom_wafeq_taxpayer_id": "txpay_TEST",
        })
        frappe.clear_document_cache("Company", COMPANY)

        si = make_sales_invoice(submit=True)
        si.db_set({"custom_uae_einvoice_status": "Success", "custom_document_id": "inv_WEBHOOK1",
                   "custom_reporting_status": "pending"})
        body = json.dumps({
            "id": "evt_1", "type": "document.reported", "test": False,
            "data": {"document_id": "inv_WEBHOOK1", "taxpayer_id": "txpay_TEST", "tax_authority_status": "REPORTED"},
        }).encode()
        timestamp = str(int(now_datetime().timestamp()))
        signature = base64.b64encode(hmac.new(
            base64.b64decode(secret[len("whsec_"):]), f"evt-msg-1.{timestamp}.".encode() + body, hashlib.sha256
        ).digest()).decode()
        headers = {"webhook-id": "evt-msg-1", "webhook-timestamp": timestamp, "webhook-signature": f"v1,{signature}"}

        try:
            self.assertEqual(webhook._authenticate_wafeq_webhook(body, headers), COMPANY)
            with self.assertRaises(frappe.AuthenticationError):
                webhook._authenticate_wafeq_webhook(body + b" ", headers)

            from uae_erpgulf.adapters.wafeq import normalize_wafeq_event
            with _no_transaction_control(), patch("uae_erpgulf.adapters.wafeq.WafeqASPAdapter.get_document_artifacts",
                                                 return_value={"xml": None, "pdf": None}):
                webhook._apply_wafeq_reporting_event(COMPANY, normalize_wafeq_event(json.loads(body)))
        finally:
            _ensure_company()
            frappe.clear_document_cache("Company", COMPANY)

        self.assertEqual(frappe.db.get_value("Sales Invoice", si.name, "custom_reporting_status"), "reported")

    def test_in_flight_invoice_cannot_be_cancelled(self):
        si = make_sales_invoice(submit=True)
        si.db_set("custom_uae_einvoice_status", "Retry Pending")

        with self.assertRaises(frappe.ValidationError):
            si.reload()
            si.cancel()


class TestEInvoiceSecurity(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _ensure_fiscal_year()
        _ensure_company()

    def tearDown(self):
        frappe.set_user("Administrator")

    def test_credentials_are_password_fields(self):
        meta = frappe.get_meta("Company")
        for fieldname in SECRET_FIELDS:
            self.assertEqual(meta.get_field(fieldname).fieldtype, "Password", fieldname)

    def test_secrets_are_encrypted_at_rest(self):
        set_company_secret(COMPANY, "custom_xflickauthkey", "flick-key-123")

        self.assertEqual(frappe.db.get_value("Company", COMPANY, "custom_xflickauthkey"), "*" * 13)
        self.assertEqual(get_company_secret(COMPANY, "custom_xflickauthkey"), "flick-key-123")
        self.assertEqual(get_flick_headers(COMPANY), {"X-Flick-Auth-Key": "flick-key-123"})

    def test_webhook_rejects_missing_or_wrong_token(self):
        set_company_secret(COMPANY, "custom_webhook_token", "tok-good")
        frappe.db.set_value("Company", COMPANY, "custom_webhook_signature_header", None)

        for bad_token in (None, "", "tok-bad"):
            with self.assertRaises(frappe.AuthenticationError):
                webhook._authenticate_webhook(bad_token, b"{}")
        self.assertEqual(webhook._authenticate_webhook("tok-good", b"{}"), COMPANY)

    def test_webhook_signature_is_required_when_configured(self):
        body = b'{"event": "document.reporting.reported"}'
        set_company_secret(COMPANY, "custom_webhook_token", "tok-signed")
        set_company_secret(COMPANY, "custom_secret_of_webhook", "whsec-test")
        frappe.db.set_value("Company", COMPANY, "custom_webhook_signature_header", "X-Test-Signature")
        good = hmac.new(b"whsec-test", body, hashlib.sha256).hexdigest()

        for provided in (None, "deadbeef", hmac.new(b"other", body, hashlib.sha256).hexdigest()):
            with patch("frappe.get_request_header", return_value=provided),                     self.assertRaises(frappe.AuthenticationError):
                webhook._authenticate_webhook("tok-signed", body)

        with patch("frappe.get_request_header", return_value=f"sha256={good}"):
            self.assertEqual(webhook._authenticate_webhook("tok-signed", body), COMPANY)

    def test_unsupported_provider_is_refused(self):
        frappe.db.set_value("Company", COMPANY, "custom_accredited_service_providers", "Pagero Gulf FZ-LLC")
        frappe.clear_document_cache("Company", COMPANY)
        try:
            with self.assertRaises(frappe.ValidationError):
                factory.ASPFactory.get_adapter(COMPANY)
        finally:
            _ensure_company()
            frappe.clear_document_cache("Company", COMPANY)

    def test_non_flick_adapter_uses_own_key_and_ignores_flick_url(self):
        set_company_secret(COMPANY, "custom_xflickauthkey", "flick-key")
        set_company_secret(COMPANY, "custom_asp_api_key", "pk_test_wafeq")
        frappe.db.set_value("Company", COMPANY, {
            "custom_accredited_service_providers": "Wafeq",
            "custom_base_url": "https://sb-ae-api.flick.network",
        })
        frappe.clear_document_cache("Company", COMPANY)
        try:
            adapter = factory.ASPFactory.get_adapter(COMPANY)
            self.assertIsInstance(adapter, WafeqASPAdapter)
            self.assertEqual(adapter.api_key, "pk_test_wafeq")
            self.assertEqual(adapter.base_url, WafeqASPAdapter.DEFAULT_BASE_URL)
        finally:
            frappe.db.set_value("Company", COMPANY, "custom_base_url", None)
            _ensure_company()
            frappe.clear_document_cache("Company", COMPANY)

    def test_stored_subscription_responses_never_contain_secrets(self):
        redacted = webhook._redact({"data": {"secret": "whsec-x", "endpoint": "https://site/api?token=Abc123"}})
        self.assertNotIn("whsec-x", json.dumps(redacted))
        self.assertNotIn("Abc123", json.dumps(redacted))

    def test_company_endpoints_require_company_permission(self):
        frappe.set_user("Guest")
        for endpoint in (
            webhook.register_flick_webhook,
            webhook.custom_get_subscription,
            webhook.get_webhook_deliveries,
            verify_token.get_flick_access_token,
            verify_token.verify_flick_token,
            factory.test_asp_connection,
        ):
            with self.assertRaises(frappe.PermissionError, msg=endpoint.__name__):
                endpoint(COMPANY)

    def test_bulk_send_rejects_client_built_documents(self):
        from uae_erpgulf.uae_erpgulf import test as sales_bulk

        forged = json.dumps({"doctype": "Sales Invoice", "company": COMPANY, "customer": "Anyone"})
        with patch("frappe.enqueue") as enqueue:
            result = sales_bulk.bulk_send_invoices([forged])
        self.assertEqual(result["success"], [])
        self.assertEqual(len(result["failed"]), 1)
        enqueue.assert_not_called()


# --- Fixtures --------------------------------------------------------------------------------


def make_sales_invoice(
    qty=1,
    rate=100,
    discount_amount=0,
    vat_category="S - Standard rated",
    with_vat=True,
    company_address=None,
    posting_date=None,
    submit=False,
):
    si = frappe.get_doc({
        "doctype": "Sales Invoice",
        "company": COMPANY,
        "customer": CUSTOMER,
        "currency": "AED",
        "posting_date": posting_date or nowdate(),
        "set_posting_time": 1,
        "due_date": posting_date or nowdate(),
        "custom_vat_category": vat_category,
        "apply_discount_on": "Net Total",
        "discount_amount": discount_amount,
        "items": [{"item_code": ITEM, "qty": qty, "rate": rate}],
    })
    if company_address:
        si.company_address = company_address
    if with_vat:
        si.append("taxes", {
            "charge_type": "On Net Total",
            "account_head": frappe.db.get_value("Account", {"company": COMPANY, "account_name": "_Test UAE VAT 5%"}),
            "description": "VAT 5%",
            "rate": 5,
        })
    si.insert()
    if submit:
        si.submit()
    return si


class _no_transaction_control:
    """The queue commits and rolls back explicitly; keep the test transaction intact."""

    def __enter__(self):
        self._patches = [patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback")]
        for p in self._patches:
            p.start()

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


def _ensure_fiscal_year():
    from erpnext.accounts.utils import FiscalYearError, get_fiscal_year

    try:
        get_fiscal_year(nowdate(), company=COMPANY)
    except (FiscalYearError, frappe.ValidationError):
        year = getdate(nowdate()).year
        frappe.get_doc({
            "doctype": "Fiscal Year",
            "year": f"_Test UAE FY {year}",
            "year_start_date": f"{year}-01-01",
            "year_end_date": f"{year}-12-31",
        }).insert(ignore_if_duplicate=True)


def _ensure_company():
    if not frappe.db.exists("Company", COMPANY):
        frappe.get_doc({
            "doctype": "Company",
            "company_name": COMPANY,
            "abbr": ABBR,
            "default_currency": "AED",
            "country": "United Arab Emirates",
            "create_chart_of_accounts_based_on": "Standard Template",
            "chart_of_accounts": "Standard",
            "tax_id": "100234567890003",
        }).insert()
    frappe.db.set_value("Company", COMPANY, {
        "custom_uae_einvoice_enabled": 0,  # tests drive the queue directly
        "custom_accredited_service_providers": "Flick Network L.L.C",
        "custom_participant_id": "0235:1002345678",
    })


def _ensure_vat_account():
    name = frappe.db.get_value("Account", {"company": COMPANY, "account_name": "_Test UAE VAT 5%"})
    if name:
        return name
    parent = frappe.db.get_value("Account", {"company": COMPANY, "account_type": "Tax", "is_group": 1}) or \
        frappe.db.get_value("Account", {"company": COMPANY, "root_type": "Liability", "is_group": 1})
    return frappe.get_doc({
        "doctype": "Account",
        "account_name": "_Test UAE VAT 5%",
        "company": COMPANY,
        "parent_account": parent,
        "account_type": "Tax",
        "tax_rate": 5,
    }).insert().name


def _ensure_address(link_doctype, link_name, title, emirate, is_company=False):
    name = frappe.db.get_value("Address", {"address_title": title})
    if name:
        return name
    address = frappe.get_doc({
        "doctype": "Address",
        "address_title": title,
        "address_type": "Billing",
        "address_line1": f"1 {title} Street",
        "city": emirate or "Unknown",
        "country": "United Arab Emirates",
        "emirate": emirate,
        "is_primary_address": 1 if emirate else 0,
        "is_your_company_address": 1 if is_company else 0,
        "links": [{"link_doctype": link_doctype, "link_name": link_name}],
    })
    return address.insert().name


def _ensure_customer():
    if frappe.db.exists("Customer", CUSTOMER):
        return
    frappe.get_doc({
        "doctype": "Customer",
        "customer_name": CUSTOMER,
        "customer_type": "Company",
        "customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}) or "All Customer Groups",
        "territory": frappe.db.get_value("Territory", {"is_group": 0}) or "All Territories",
        "tax_id": "100987654321003",
    }).insert()


def _ensure_item():
    if frappe.db.exists("Item", ITEM):
        return
    frappe.get_doc({
        "doctype": "Item",
        "item_code": ITEM,
        "item_name": ITEM,
        "item_group": frappe.db.get_value("Item Group", {"is_group": 0}) or "All Item Groups",
        "stock_uom": "Nos",
        "is_stock_item": 0,
        "custom_item_type_codes": "S - Services",
    }).insert()
