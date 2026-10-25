"""
Flick Network LLC Accredited Service Provider (ASP) Adapter.
Implements the BaseASPAdapter contract for Flick UAE E-Invoicing API.
"""

import frappe
from frappe import _
import json
import requests
from typing import Dict, Any, Tuple, Optional
from uae_erpgulf.adapters.base import BaseASPAdapter, document_status_result, network_failure_result, value_or
from uae_erpgulf.uae_erpgulf.verify_token import get_valid_flick_token


class FlickASPAdapter(BaseASPAdapter):
    """Adapter for Flick Network LLC (flick.network)."""

    def __init__(self, company: str, settings: Optional[Dict[str, Any]] = None):
        super().__init__(company, settings)
        self.company_doc = frappe.get_cached_doc("Company", company)
        self.base_url = (self.company_doc.custom_base_url or "https://sb-ae-api.flick.network").rstrip("/")
        self.participant_id = getattr(self.company_doc, "custom_participant_id", None)
        self.auth_key = self._secret("custom_xflickauthkey")

    def _get_headers(self) -> Dict[str, str]:
        """Resolves authentication headers (API Key or Bearer Token)."""
        if self.auth_key:
            return {
                "Content-Type": "application/json",
                "X-Flick-Auth-Key": self.auth_key,
            }

        token = get_valid_flick_token(self.company)
        if token:
            return {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            }

        frappe.throw(_("Flick authentication credentials (API Key or OAuth2) missing for {0}").format(self.company))

    def test_connection(self) -> Tuple[bool, str]:
        """Verifies API connection with Flick."""
        try:
            url = f"{self.base_url}/v1/auth/verify"
            response = requests.get(url, headers=self._get_headers(), timeout=15)
            if response.status_code == 200:
                return True, "Flick API connection authenticated successfully."
            return False, f"Flick API Error ({response.status_code}): {response.text}"
        except Exception as e:
            return False, str(e)

    def transform_payload(self, canonical_doc: Dict[str, Any]) -> Dict[str, Any]:
        """
        Maps the Canonical UAE Invoice Model into Flick's exact JSON schema.
        """
        seller = canonical_doc["seller"]
        buyer = canonical_doc["buyer"]
        totals = canonical_doc["monetary_totals"]

        # Build lines in Flick format
        flick_lines = []
        for line in canonical_doc["lines"]:
            flick_line = {
                "id": str(line.get("id") or line.get("idx", 1)),
                "note": "UAE e-invoice line item",
                "invoiced_quantity": str(line.get("quantity", 1)),
                "uom": line.get("uom", "NOS"),
                "line_extension_amount": str(value_or(line, "line_extension_amount", line.get("line_amount", 0.0))),
                "name": line.get("name") or line.get("item_name", ""),
                "description": line.get("description") or line.get("item_name", ""),
                "commodity_code": line.get("commodity_code") or line.get("item_code", ""),
                "hs_code": line.get("hs_code"),
                "sac_code": line.get("sac_code"),
                "vat_category": value_or(line, "vat_category", line.get("tax_category", "S")),
                "vat_percentage": value_or(line, "vat_percentage", line.get("tax_rate", 5.0)),
                "unit_price": value_or(line, "unit_price", line.get("rate", 0.0)),
                "base_quantity": "1"
            }
            flick_lines.append(flick_line)

        # Build Flick document dictionary
        flick_document = {
            "invoice_type_code": canonical_doc["document_type"],
            "document_identifier": canonical_doc["document_identifier"],
            # Flick's schema carries an icv_code; the UAE has no ICV, so send the invoice number's digits as before
            "icv_code": "".join(ch for ch in str(canonical_doc["document_identifier"]) if ch.isdigit()) or "1",
            "issue_date": canonical_doc["issue_date"],
            "issue_time": canonical_doc["issue_time"],
            "tax_point_date": canonical_doc.get("tax_point_date"),
            "due_date": canonical_doc.get("due_date"),
            "document_currency": canonical_doc["document_currency"],
            "currency_exchange_rate": canonical_doc.get("currency_exchange_rate"),
            "issuing_party": {
                "legal_name": seller.get("legal_name", ""),
                "trade_name": seller.get("trade_name") or seller.get("legal_name", ""),
                "vat_number": seller.get("tax_id", ""),
                "legal_identifier": seller.get("trade_license", ""),
                "street_address": seller.get("street_address", ""),
                "city_address": seller.get("city", ""),
                "emirates_code": seller.get("emirate_code") or seller.get("state", "DXB"),
                "country_code": seller.get("country_code", "AE"),
                "postal_zone": seller.get("postal_code", ""),
                "peppol_id": seller.get("peppol_id", ""),
                "contact_email": seller.get("email", ""),
            },
            "receiving_party": {
                "legal_name": buyer.get("legal_name", ""),
                "trade_name": buyer.get("trade_name") or buyer.get("legal_name", ""),
                "vat_number": buyer.get("tax_id", ""),
                "legal_identifier": buyer.get("trade_license", ""),
                "street_address": buyer.get("street_address", ""),
                "city_address": buyer.get("city", ""),
                "emirates_code": buyer.get("emirate_code") or buyer.get("state", "DXB"),
                "country_code": buyer.get("country_code", "AE"),
                "postal_zone": buyer.get("postal_code", ""),
                "peppol_id": buyer.get("peppol_id", ""),
                "contact_email": buyer.get("email", ""),
            },
            "invoice_lines": flick_lines,
            "legal_monetary_total": {
                "line_extension_amount": str(totals.get("line_extension_amount", 0.0)),
                "tax_exclusive_amount": str(totals.get("tax_exclusive_amount", 0.0)),
                "tax_inclusive_amount": str(totals.get("tax_inclusive_amount", 0.0)),
                "payable_amount": str(totals.get("payable_amount", 0.0)),
                "allowance_total_amount": str(totals.get("allowance_total_amount", 0.0)),
                "charge_total_amount": str(totals.get("charge_total_amount", 0.0)),
            },
            "payment_means": canonical_doc.get("payment_means") or [],
        }

        if canonical_doc.get("billing_reference"):
            flick_document["billing_reference"] = [canonical_doc["billing_reference"]]

        if canonical_doc.get("invoice_period"):
            flick_document["invoice_period"] = canonical_doc["invoice_period"]

        return {"document": flick_document}

    def submit_document(self, canonical_doc: Dict[str, Any]) -> Dict[str, Any]:
        """Submits document payload to Flick /v1/{participant_id}/documents endpoint."""
        if not self.participant_id:
            frappe.throw(_("Participant ID is missing in Company {0}").format(self.company))

        url = f"{self.base_url}/v1/{self.participant_id}/documents"
        payload = self.transform_payload(canonical_doc)
        headers = self._get_headers()

        response = requests.post(url, headers=headers, json=payload, timeout=60)

        try:
            response_data = response.json()
        except Exception:
            response_data = {"raw_response": response.text}

        data = response_data.get("data", {}) if isinstance(response_data, dict) else {}
        is_success = response.status_code in (200, 201)

        return {
            "success": is_success,
            "status_code": response.status_code,
            "document_id": data.get("id"),
            "reporting_status": data.get("reporting_status"),
            "exchange_status": data.get("exchange_status"),
            "response_data": response_data,
            "error_message": None if is_success else response_data.get("message") or response.text,
        }

    def get_document_status(self, document_id: str) -> Dict[str, Any]:
        """Queries Flick document status endpoint."""
        url = f"{self.base_url}/v1/{self.participant_id}/documents/{document_id}"
        try:
            response = requests.get(url, headers=self._get_headers(), timeout=30)
        except requests.exceptions.RequestException as e:
            return network_failure_result(e)
        return document_status_result(response)

    def get_document_artifacts(self, document_id: str) -> Dict[str, Optional[bytes]]:
        """Downloads cleared XML and PDF from Flick."""
        artifacts: Dict[str, Optional[bytes]] = {"xml": None, "pdf": None}
        headers = self._get_headers()

        # XML
        try:
            xml_url = f"{self.base_url}/v1/{self.participant_id}/documents/{document_id}/xml"
            xml_resp = requests.get(xml_url, headers=headers, timeout=30)
            if xml_resp.status_code == 200:
                artifacts["xml"] = xml_resp.content
        except Exception:
            pass

        # PDF
        try:
            pdf_url = f"{self.base_url}/v1/{self.participant_id}/documents/{document_id}/pdf"
            pdf_resp = requests.get(pdf_url, headers=headers, timeout=30)
            if pdf_resp.status_code == 200:
                artifacts["pdf"] = pdf_resp.content
        except Exception:
            pass

        return artifacts

    def parse_webhook_payload(self, raw_data: bytes, headers: Dict[str, str]) -> Dict[str, Any]:
        """Parses Flick webhook payload into standard structure."""
        data = json.loads(raw_data.decode("utf-8") if isinstance(raw_data, bytes) else raw_data)
        doc_data = data.get("data", {})
        return {
            "event_type": data.get("event"),
            "participant_id": data.get("participant_id"),
            "document_id": doc_data.get("document_id"),
            "invoice_number": doc_data.get("document_identifier"),
            "reporting_status": doc_data.get("reporting_status"),
            "exchange_status": doc_data.get("exchange_status"),
            "status": doc_data.get("status"),
        }
