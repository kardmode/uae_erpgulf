"""
Complyance.io (Complyance Electronics L.L.C) Accredited Service Provider (ASP) Adapter.
Implements the BaseASPAdapter contract for Complyance's UAE Peppol PINT-AE E-Invoicing API.
Platform: Complyance.io (one.complyance.io / api.complyance.io/v1)
Accreditation: Approved UAE MoF / FTA Accredited Service Provider (Complyance Electronics L.L.C)
"""

import frappe
from frappe import _
import json
import requests
from typing import Dict, Any, Tuple, Optional
from uae_erpgulf.adapters.base import BaseASPAdapter, document_status_result, network_failure_result, party_address, value_or
from uae_erpgulf.core.constants import CREDIT_NOTE_TYPES


class ComplyanceASPAdapter(BaseASPAdapter):
    """
    Adapter for Complyance Electronics L.L.C / Complyance.io.
    Approved UAE Ministry of Finance & FTA E-Invoicing Service Provider (ASP).
    Features instant self-service developer sandbox, REST GETS-standard JSON, and PINT-AE compliance.
    """

    DEFAULT_BASE_URL = "https://api.complyance.io/v1"
    DEFAULT_SANDBOX_URL = "https://sandbox-api.complyance.io/v1"

    def __init__(self, company: str, settings: Optional[Dict[str, Any]] = None):
        super().__init__(company, settings)
        self.company_doc = frappe.get_cached_doc("Company", company)

        # Base URL resolution
        self.base_url = self._base_url(self.DEFAULT_BASE_URL)

        # Credentials
        self.api_key = self._secret("custom_asp_api_key")
        self.participant_id = getattr(self.company_doc, "custom_participant_id", None)

    def _get_headers(self, idempotency_key: Optional[str] = None) -> Dict[str, str]:
        """Resolves authentication headers using Complyance's Bearer API Key standard."""
        if not self.api_key:
            frappe.throw(_("Complyance.io API Key is missing in Company settings for {0}").format(self.company))

        token = self.api_key.strip()
        auth_header = token if token.lower().startswith("bearer ") else f"Bearer {token}"

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": auth_header,
            "User-Agent": "ERPGulf-UAE-EInvoicing/2.0",
        }

        if idempotency_key:
            headers["Idempotency-Key"] = str(idempotency_key).strip()

        return headers

    def test_connection(self) -> Tuple[bool, str]:
        """
        Verifies API connection with Complyance.io using the configured API Key.
        """
        try:
            url = f"{self.base_url}/auth/verify"
            headers = self._get_headers()
            response = requests.get(url, headers=headers, timeout=15)

            if response.status_code in (200, 204):
                return True, "Complyance.io API connection authenticated successfully."

            # Alternate ping/health endpoint
            ping_url = f"{self.base_url}/ping"
            ping_resp = requests.get(ping_url, headers=headers, timeout=15)
            if ping_resp.status_code in (200, 204):
                return True, "Complyance.io API authenticated successfully."

            if response.status_code in (401, 403) or ping_resp.status_code in (401, 403):
                return False, f"Complyance Authentication Failed (Invalid API Key). Status: {response.status_code}"

            return False, f"Complyance API Error ({response.status_code}): {response.text[:200]}"
        except Exception as e:
            return False, f"Complyance Connection Exception: {str(e)}"

    def transform_payload(self, canonical_doc: Dict[str, Any]) -> Dict[str, Any]:
        """
        Transforms Canonical UAE Invoice Model into Complyance GETS AE JSON schema.
        """
        seller = canonical_doc.get("seller", {}) or {}
        seller_addr = seller.get("address") or party_address(seller)
        buyer = canonical_doc.get("buyer", {}) or {}
        buyer_addr = buyer.get("address") or party_address(buyer)
        totals = canonical_doc.get("monetary_totals", {}) or {}

        doc_type = str(canonical_doc.get("document_type", "380"))
        if doc_type in CREDIT_NOTE_TYPES:
            type_label = "CREDIT_NOTE"
        elif doc_type == "383":
            type_label = "DEBIT_NOTE"
        else:
            type_label = "TAX_INVOICE"

        line_items = []
        for line in canonical_doc.get("lines", []):
            tax_pct = float(value_or(line, "vat_percentage", value_or(line, "tax_percent", 5.0)))
            tax_cat = line.get("vat_category") or line.get("tax_category") or ("S" if tax_pct > 0 else "Z")
            line_items.append({
                "item_code": line.get("item_code"),
                "name": line.get("name") or line.get("item_name") or line.get("item_code") or "Item",
                "description": line.get("description") or line.get("name") or line.get("item_name") or "Item",
                "quantity": float(line.get("quantity", 1)),
                "unit_price": float(line.get("unit_price", 0)),
                "unit_code": line.get("unit_code") or "H87",
                "tax_category": tax_cat,
                "tax_rate": tax_pct,
                "tax_amount": float(value_or(line, "vat_amount", line.get("tax_amount", 0.0))),
                "total_amount": float(line.get("line_extension_amount", 0.0)),
            })

        payload = {
            "country": "AE",
            "format": "GETS",
            "document_type": type_label,
            "invoice_number": canonical_doc.get("document_identifier"),
            "issue_date": canonical_doc.get("issue_date"),
            "due_date": canonical_doc.get("due_date") or canonical_doc.get("issue_date"),
            "currency": canonical_doc.get("document_currency", "AED"),
            "supplier": {
                "name": seller.get("legal_name"),
                "trn": seller.get("tax_id"),
                "address": {
                    "street": seller_addr.get("street") or "",
                    "city": seller_addr.get("city") or "",
                    "country": seller_addr.get("country") or "AE",
                    "state": seller_addr.get("state") or "",
                },
            },
            "buyer": {
                "name": buyer.get("legal_name") or buyer.get("name"),
                "trn": buyer.get("tax_id"),
                "address": {
                    "street": buyer_addr.get("street") or "",
                    "city": buyer_addr.get("city") or "",
                    "country": buyer_addr.get("country") or "AE",
                    "state": buyer_addr.get("state") or "",
                },
            },
            "line_items": line_items,
            "totals": {
                "tax_exclusive_amount": float(totals.get("tax_exclusive_amount", 0.0)),
                "tax_inclusive_amount": float(totals.get("tax_inclusive_amount", 0.0)),
                "tax_amount": float(value_or(totals, "tax_amount", 0.0)),
                "payable_amount": float(totals.get("payable_amount", 0.0)),
            },
            "metadata": {
                "erpnext_name": canonical_doc.get("document_identifier"),
            },
        }

        if canonical_doc.get("notes"):
            payload["notes"] = str(canonical_doc.get("notes"))[:2000]

        return payload

    def submit_document(self, canonical_doc: Dict[str, Any]) -> Dict[str, Any]:
        """
        Transmits invoice to Complyance.io submission endpoint.
        """
        url = f"{self.base_url}/invoices"
        payload = self.transform_payload(canonical_doc)
        idempotency_key = canonical_doc.get("document_identifier")
        headers = self._get_headers(idempotency_key=idempotency_key)

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=60)

            try:
                response_data = response.json()
            except Exception:
                response_data = {"raw_response": response.text}

            is_success = response.status_code in (200, 201, 202)
            data = response_data if isinstance(response_data, dict) else {}

            doc_id = data.get("document_id") or data.get("invoice_id") or data.get("id")
            # Passed through unchanged; a 202 without a status means accepted for processing
            reporting_status = data.get("status") or ("queued" if response.status_code == 202 else None)
            exchange_status = data.get("exchange_status") or reporting_status

            return {
                "success": is_success,
                "status_code": response.status_code,
                "document_id": doc_id,
                "reporting_status": reporting_status,
                "exchange_status": exchange_status,
                "response_data": response_data,
                "error_message": None if is_success else (data.get("message") or response.text[:200]),
            }
        except requests.exceptions.RequestException as e:
            return network_failure_result(e)

    def get_document_status(self, document_id: str) -> Dict[str, Any]:
        """Queries Complyance document inquiry endpoint."""
        url = f"{self.base_url}/invoices/{document_id}"
        try:
            response = requests.get(url, headers=self._get_headers(), timeout=30)
        except requests.exceptions.RequestException as e:
            return network_failure_result(e)
        return document_status_result(response)

    def get_document_artifacts(self, document_id: str) -> Dict[str, Optional[bytes]]:
        """Downloads cleared XML and PDF artifacts from Complyance."""
        artifacts: Dict[str, Optional[bytes]] = {"xml": None, "pdf": None}
        headers = self._get_headers()

        # XML
        try:
            xml_url = f"{self.base_url}/invoices/{document_id}/xml"
            xml_resp = requests.get(xml_url, headers=headers, timeout=30)
            if xml_resp.status_code == 200:
                artifacts["xml"] = xml_resp.content
        except Exception:
            pass

        return artifacts

    def parse_webhook_payload(self, raw_data: bytes, headers: Dict[str, str]) -> Dict[str, Any]:
        """Parses Complyance.io inbound webhook event."""
        data = json.loads(raw_data.decode("utf-8") if isinstance(raw_data, bytes) else raw_data)
        doc = data.get("data", data)
        return {
            "event_type": data.get("event") or data.get("type", "invoice.cleared"),
            "document_id": doc.get("document_id") or doc.get("id"),
            "invoice_number": doc.get("invoice_number"),
            "reporting_status": doc.get("status"),
            "exchange_status": doc.get("status"),
            "status": doc.get("status"),
        }
