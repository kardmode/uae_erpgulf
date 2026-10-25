"""
Wafeq Accredited Service Provider (ASP) Adapter.
Implements the BaseASPAdapter contract for Wafeq's UAE e-invoicing API
(https://docs.wafeq.com/uae-einvoicing).

Wafeq builds the PINT-AE XML itself: the app sends the buyer, the lines and their VAT
category; Wafeq adds the seller (the taxpayer's registered profile), the totals and the
document type, delivers over Peppol and reports to the FTA.

Status model: a submission is accepted (or refused) synchronously. Delivery to the buyer
(`delivery_status`) and the FTA report (`tdd_status`) are tracked separately afterwards;
only `tdd_status` decides whether the invoice is reported.
"""

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict, List, Optional, Tuple

import frappe
import requests
from frappe import _

from uae_erpgulf.adapters.base import (
    BaseASPAdapter,
    network_failure_result,
    party_address,
    value_or,
)
from uae_erpgulf.core.constants import (
    CREDIT_NOTE_TYPES,
    DOC_TYPE_SELF_BILLED_CREDIT_NOTE,
    DOC_TYPE_SELF_BILLED_INVOICE,
)

# VAT categories whose lines must carry tax_percent 0
ZERO_RATE_CATEGORIES = ("Z", "E", "O", "AE")
# Delivery rejections that need a corrected document (BV business rule, SV syntax)
CORRECTABLE_REJECTIONS = ("BV", "SV")
# Standard Webhooks: accept timestamps within 5 minutes
WEBHOOK_TOLERANCE_SECONDS = 300


class WafeqASPAdapter(BaseASPAdapter):
    """Adapter for Wafeq e-invoicing ASP (api.wafeq.com/ae/v1)."""

    # Same Idempotency-Key -> the provider never accepts the document twice, so retries are safe
    IDEMPOTENT_SUBMISSION = True
    DEFAULT_BASE_URL = "https://api.wafeq.com/ae/v1"

    def __init__(self, company: str, settings: Optional[Dict[str, Any]] = None):
        super().__init__(company, settings)
        self.company_doc = frappe.get_cached_doc("Company", company)
        self.base_url = self._base_url(self.DEFAULT_BASE_URL)
        self.api_key = self._secret("custom_asp_api_key")
        self.taxpayer_id = _clean(getattr(self.company_doc, "custom_wafeq_taxpayer_id", None))
        self.grant_id = _clean(getattr(self.company_doc, "custom_wafeq_grant_id", None))

    # -- HTTP ----------------------------------------------------------------------------

    def _get_headers(self, idempotency_key: Optional[str] = None) -> Dict[str, str]:
        """API key + taxpayer (+ grant) headers; submissions add an Idempotency-Key and ask for async delivery."""
        if not self.api_key:
            frappe.throw(_("Wafeq API Key is missing in Company settings for {0}").format(self.company))
        if not self.taxpayer_id:
            frappe.throw(_("Wafeq Taxpayer ID (txpay_...) is missing in Company settings for {0}").format(self.company))

        headers = {
            "Authorization": f"Api-Key {self.api_key.strip()}",
            "x-ae-taxpayer-id": self.taxpayer_id,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "ERPGulf-UAE-EInvoicing/2.0",
        }
        if self.grant_id:
            headers["x-ae-grant-id"] = self.grant_id
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
            headers["Prefer"] = "respond-async"
        return headers

    def test_connection(self) -> Tuple[bool, str]:
        """Reads the taxpayer's registered profile: proves the key, taxpayer and grant all work."""
        try:
            response = requests.get(f"{self.base_url}/settings/profile", headers=self._get_headers(), timeout=15)
        except requests.exceptions.RequestException as e:
            return False, f"Wafeq connection error: {e}"

        if response.status_code == 200:
            mode = "Sandbox" if (self.api_key or "").startswith("pk_test_") else "Live"
            return True, f"Wafeq {mode}: authenticated for taxpayer {self.taxpayer_id}."
        return False, f"Wafeq API error ({response.status_code}): {_error_text(response)}"

    # -- Payload -------------------------------------------------------------------------

    @staticmethod
    def _endpoint(document_type: str) -> str:
        if document_type == DOC_TYPE_SELF_BILLED_INVOICE:
            return "/self-billing/invoices"
        if document_type == DOC_TYPE_SELF_BILLED_CREDIT_NOTE:
            return "/self-billing/credit-notes"
        if document_type in CREDIT_NOTE_TYPES:
            return "/credit-notes"
        return "/invoices"

    def transform_payload(self, canonical_doc: Dict[str, Any]) -> Dict[str, Any]:
        """
        Maps the canonical model onto Wafeq's request schema. The seller, totals and
        document type are never sent: Wafeq derives them.
        """
        doc_type = str(canonical_doc.get("document_type") or "380")
        is_credit_note = doc_type in CREDIT_NOTE_TYPES
        is_self_billed = doc_type in (DOC_TYPE_SELF_BILLED_INVOICE, DOC_TYPE_SELF_BILLED_CREDIT_NOTE)

        payload: Dict[str, Any] = {
            "issue_date": canonical_doc.get("issue_date"),
            "source_id": canonical_doc.get("source_id") or canonical_doc.get("document_identifier"),
            "line_items": [self._line(line) for line in canonical_doc.get("lines") or []],
            "tax_amount_type": "TAX_EXCLUSIVE",
        }

        # On a self-billed document the taxpayer is the buyer and the counterparty is the supplier
        counterparty = canonical_doc.get("seller") if is_self_billed else canonical_doc.get("buyer")
        payload["supplier" if is_self_billed else "customer"] = self._party(counterparty or {})

        if is_credit_note:
            payload["credit_note_number"] = canonical_doc.get("document_identifier")
            payload.update(self._credit_note_reference(canonical_doc))
        else:
            payload["invoice_number"] = canonical_doc.get("document_identifier")
            payload["due_date"] = canonical_doc.get("due_date") or canonical_doc.get("issue_date")
            means = (canonical_doc.get("payment_means") or [None])[0]
            if means:
                payload["payment_means"] = {"code": means.get("payment_means_code")}
                iban = (means.get("payee_financial_account") or {}).get("iban")
                if iban:
                    payload["payment_means"]["iban"] = iban

        currency = canonical_doc.get("document_currency") or "AED"
        if currency != "AED":
            payload["currency"] = currency
            payload["currency_exchange_rate"] = str(canonical_doc.get("currency_exchange_rate"))

        flags = canonical_doc.get("transaction_type_code")
        if flags and flags.strip("0"):
            payload["transaction_type_code"] = flags
        if canonical_doc.get("invoice_period"):
            period = canonical_doc["invoice_period"]
            payload["invoice_period"] = {"start_date": period["start_date"], "end_date": period["end_date"]}
        if canonical_doc.get("buyer_reference"):
            payload["buyer_reference"] = str(canonical_doc["buyer_reference"])[:128]
        if canonical_doc.get("notes"):
            payload["notes"] = str(canonical_doc["notes"])[:2000]

        return payload

    @staticmethod
    def _line(line: Dict[str, Any]) -> Dict[str, Any]:
        category = line.get("vat_category") or line.get("tax_category") or "S"
        rate = float(value_or(line, "vat_percentage", value_or(line, "tax_percent", 5.0)))
        quantity = float(line.get("quantity") or 0)
        # Wafeq computes the line amount as quantity x unit_price. Derive the price from ERPNext's
        # net line amount (after discounts) so the line, and its VAT, match the posted invoice.
        unit_price = float(value_or(line, "unit_price", 0.0))
        if quantity and line.get("line_extension_amount") is not None:
            unit_price = round(float(line["line_extension_amount"]) / quantity, 6)
        item = {
            "description": str(line.get("description") or line.get("name") or "Item")[:1024],
            "name": str(line.get("name") or line.get("description") or "Item")[:512],
            "quantity": quantity,
            "unit_price": unit_price,
            "unit_code": line.get("unit_code") or "H87",
            "tax_category": category,
            # Wafeq defaults the rate to 5 for every category, so always send it explicitly
            "tax_percent": 0.0 if category in ZERO_RATE_CATEGORIES else rate,
        }
        if category == "E" and line.get("exemption_reason_code"):
            item["tax_exemption_reason_code"] = line["exemption_reason_code"]
            if line.get("exemption_reason"):
                item["tax_exemption_reason"] = str(line["exemption_reason"])[:512]
        if category == "AE" and line.get("rcm_goods_type"):
            item["reverse_charge_goods_type"] = line["rcm_goods_type"]
        if line.get("gtin"):
            item["gtin"] = str(line["gtin"])
        return item

    @staticmethod
    def _party(party: Dict[str, Any]) -> Dict[str, Any]:
        address = party.get("address") or party_address(party)
        result: Dict[str, Any] = {
            "name": party.get("legal_name") or party.get("name"),
            "address": {
                "street": address.get("street"),
                "city": address.get("city"),
                "country": address.get("country") or "AE",
            },
        }
        if address.get("state"):
            result["address"]["country_subentity"] = address["state"]
        if party.get("trade_name") and party.get("trade_name") != result["name"]:
            result["trade_name"] = party["trade_name"]
        if party.get("tax_id") and len("".join(c for c in str(party["tax_id"]) if c.isdigit())) == 15:
            result["vat_number"] = party["tax_id"]

        # Peppol ID: 0235:<TIN> for UAE parties, or the party's own scheme abroad
        if party.get("peppol_id"):
            result["id"] = party["peppol_id"]
        elif party.get("trade_license"):
            result["identifier"] = party["trade_license"]

        if party.get("legal_registration"):
            result["legal_registration"] = {
                "type": party["legal_registration"]["type"],
                "id": party["legal_registration"]["id"],
            }
        return result

    @staticmethod
    def _credit_note_reference(canonical_doc: Dict[str, Any]) -> Dict[str, Any]:
        ref = canonical_doc.get("billing_reference") or {}
        if not isinstance(ref, dict):
            ref = {"id": ref}
        fields: Dict[str, Any] = {}
        reason = canonical_doc.get("credit_note_reason_code")
        if reason:
            fields["credit_note_reason_code"] = reason
        if reason == "VD":
            return fields  # a volume discount credits no single invoice

        if ref.get("document_id"):
            fields["previous_invoice_source"] = "ap"
            fields["previous_document_id"] = ref["document_id"]
        elif ref.get("id"):
            fields["previous_invoice_source"] = "external"
            fields["previous_invoice_number"] = ref["id"]
            if ref.get("issue_date"):
                fields["previous_invoice_issue_date"] = ref["issue_date"]
        return fields

    @staticmethod
    def idempotency_key(canonical_doc: Dict[str, Any], payload: Dict[str, Any]) -> str:
        """
        One key per document version: retries of the same body reuse it (Wafeq never accepts
        the document twice); a changed document gets a new key, as Wafeq requires.
        """
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]
        source = str(canonical_doc.get("source_id") or canonical_doc.get("document_identifier"))
        return f"{source[:100]}-{digest}"

    # -- Submission ----------------------------------------------------------------------

    def submit_document(self, canonical_doc: Dict[str, Any]) -> Dict[str, Any]:
        payload = self.transform_payload(canonical_doc)
        url = f"{self.base_url}{self._endpoint(str(canonical_doc.get('document_type')))}"
        headers = self._get_headers(idempotency_key=self.idempotency_key(canonical_doc, payload))

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=60)
        except requests.exceptions.RequestException as e:
            return network_failure_result(e)

        data = _json(response)
        code = _detail_code(data)

        # Accepted: delivered, queued or reported-only. `failed` WITH a document_id means accepted
        # and reported, but delivery to the buyer failed: never resubmit it.
        if response.status_code in (200, 202) and data.get("document_id") and (
            data.get("success") or data.get("status") == "failed"
        ):
            return self._accepted(response, data)

        # Already there: the number was submitted before, or an earlier attempt ended unconfirmed
        if response.status_code == 409 and code in ("document_already_submitted", "idempotency_unconfirmed"):
            existing = self._find_document(canonical_doc)
            if existing:
                return self._accepted(response, existing, document_id=existing.get("id"))

        retryable = response.status_code in (429, 503) or code in (
            "idempotency_in_progress",
            "correction_not_ready",
            "transport_reconciliation_required",
        )
        return {
            "success": False,
            "status_code": response.status_code,
            "document_id": None,
            "reporting_status": None,
            "exchange_status": data.get("status"),
            "response_data": data,
            "error_message": _refusal_message(response, data),
            "retryable": retryable,
        }

    @staticmethod
    def _accepted(response, data: Dict[str, Any], document_id: Optional[str] = None) -> Dict[str, Any]:
        return {
            "success": True,
            "status_code": response.status_code,
            "document_id": document_id or data.get("document_id"),
            # Acceptance is not FTA reporting: tdd_status arrives later (webhook or polling)
            "reporting_status": data.get("tdd_status"),
            "exchange_status": data.get("delivery_status") or data.get("status"),
            "response_data": data,
            "error_message": data.get("error"),
            "retryable": False,
        }

    def _find_document(self, canonical_doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Looks up a document by its number on its issue date (the list API has no number filter)."""
        doc_type = str(canonical_doc.get("document_type"))
        path = self._endpoint(doc_type)
        params = {"direction": "outbound", "limit": 200}
        if canonical_doc.get("issue_date"):
            params["date_from"] = params["date_to"] = canonical_doc["issue_date"]
        try:
            response = requests.get(f"{self.base_url}{path}", headers=self._get_headers(), params=params, timeout=30)
        except requests.exceptions.RequestException:
            return None
        if response.status_code != 200:
            return None
        body = _json(response)
        items = body.get("data") or body.get("results") or body.get("items") or []
        number = canonical_doc.get("document_identifier")
        return next((item for item in items if isinstance(item, dict) and item.get("invoice_number") == number), None)

    # -- Status & artifacts --------------------------------------------------------------

    def get_document_status(self, document_id: str) -> Dict[str, Any]:
        """GET /invoices/{id}: the FTA report (tdd_status) decides reporting; delivery is informational."""
        try:
            response = requests.get(f"{self.base_url}/invoices/{document_id}", headers=self._get_headers(), timeout=30)
        except requests.exceptions.RequestException as e:
            return network_failure_result(e)

        data = _json(response)
        ok = response.status_code == 200
        reporting_status = data.get("tdd_status") if ok else None
        # A buyer-side rejection for a broken rule or format needs a corrected document
        if ok and data.get("delivery_status") == "rejected" and data.get("delivery_reason_code") in CORRECTABLE_REJECTIONS:
            reporting_status = "delivery_rejected"

        return {
            "success": ok,
            "status_code": response.status_code,
            "reporting_status": reporting_status,
            "exchange_status": data.get("delivery_status") if ok else None,
            "reporting_reference": data.get("message_id") if ok else None,
            "response_data": data,
            "error_message": None if ok else _error_text(response),
            "retryable": response.status_code in (429, 503),
        }

    def get_document_artifacts(self, document_id: str) -> Dict[str, Optional[bytes]]:
        """The e-invoice XML (Wafeq answers 302 to a short-lived download link). Wafeq has no PDF."""
        artifacts: Dict[str, Optional[bytes]] = {"xml": None, "pdf": None}
        try:
            response = requests.get(
                f"{self.base_url}/invoices/{document_id}/xml",
                headers=self._get_headers(),
                timeout=30,
                allow_redirects=True,
            )
            if response.status_code == 200:
                artifacts["xml"] = response.content
        except requests.exceptions.RequestException:
            pass
        return artifacts

    # -- Webhooks ------------------------------------------------------------------------

    def parse_webhook_payload(self, raw_data: bytes, headers: Dict[str, str]) -> Dict[str, Any]:
        """Normalises a verified Wafeq event (verify with verify_standard_webhook first)."""
        event = json.loads(raw_data.decode("utf-8") if isinstance(raw_data, bytes) else raw_data)
        return normalize_wafeq_event(event)


def normalize_wafeq_event(event: Dict[str, Any]) -> Dict[str, Any]:
    data = event.get("data") or {}
    event_type = event.get("type")
    reporting_status = None
    if event_type == "document.reported":
        reporting_status = data.get("tax_authority_status")
    elif data.get("delivery_status") == "rejected" and data.get("reason_code") in CORRECTABLE_REJECTIONS:
        reporting_status = "delivery_rejected"
    return {
        "event_id": event.get("id"),
        "event_type": event_type,
        "created_at": event.get("created_at"),
        "participant_id": event.get("participant_id"),
        "taxpayer_id": data.get("taxpayer_id"),
        "document_id": data.get("document_id"),
        "invoice_number": data.get("source_id"),
        "reporting_status": reporting_status,
        "exchange_status": data.get("delivery_status"),
        "status": data.get("fta_status") or data.get("delivery_status"),
        "test": bool(event.get("test")),
    }


def verify_standard_webhook(secret: str, raw_body: bytes, headers, now: Optional[float] = None) -> bool:
    """
    Standard Webhooks verification (https://www.standardwebhooks.com), as Wafeq uses:
    HMAC-SHA256 over "{webhook-id}.{webhook-timestamp}.{body}", keyed with the base64 secret
    (prefixed "whsec_"), sent as space-separated "v1,<base64>" entries in webhook-signature.
    """
    msg_id = headers.get("webhook-id")
    timestamp = headers.get("webhook-timestamp")
    signatures = headers.get("webhook-signature")
    if not (secret and msg_id and timestamp and signatures):
        return False

    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    if abs((now or time.time()) - sent_at) > WEBHOOK_TOLERANCE_SECONDS:
        return False

    key_material = secret[len("whsec_"):] if secret.startswith("whsec_") else secret
    try:
        key = base64.b64decode(key_material)
    except (ValueError, TypeError):
        key = key_material.encode()

    body = raw_body if isinstance(raw_body, bytes) else str(raw_body).encode()
    signed = f"{msg_id}.{timestamp}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()

    for entry in signatures.split():
        version, _, signature = entry.partition(",")
        if version == "v1" and hmac.compare_digest(signature.encode(), expected.encode()):
            return True
    return False


# -- helpers ---------------------------------------------------------------------------------


def _clean(value) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _json(response) -> Dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {"raw_response": response.text}
    return body if isinstance(body, dict) else {"data": body}


def _detail_code(data: Dict[str, Any]) -> Optional[str]:
    detail = data.get("detail")
    return detail.get("code") if isinstance(detail, dict) else None


def _error_text(response) -> str:
    data = _json(response)
    detail = data.get("detail")
    if isinstance(detail, dict):
        return detail.get("message") or detail.get("code") or response.text[:300]
    if isinstance(detail, list):
        return "; ".join(f"{'.'.join(map(str, d.get('loc', [])))}: {d.get('msg')}" for d in detail[:10])
    return data.get("error") or (detail if isinstance(detail, str) else None) or response.text[:300]


def _refusal_message(response, data: Dict[str, Any]) -> str:
    """Refusal reason, including Wafeq's PINT-AE rule codes so users can fix the invoice."""
    parts: List[str] = []
    if data.get("error"):
        parts.append(str(data["error"]))
    for item in data.get("validation_errors") or []:
        parts.append(f"{item.get('code')}: {item.get('message')}")
    return "\n".join(parts) or f"HTTP {response.status_code}: {_error_text(response)}"
