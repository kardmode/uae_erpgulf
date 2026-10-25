"""
TronStride FZC (Aigentrix) Accredited Service Provider (ASP) Adapter.
Implements the BaseASPAdapter contract for TronStride's UAE Peppol PINT-AE E-Invoicing API.
Platform: Aigentrix (app.aigentrix.ai)
"""

import frappe
from frappe import _
import json
import requests
from typing import Dict, Any, Tuple, Optional
from uae_erpgulf.adapters.base import BaseASPAdapter, document_status_result, network_failure_result
from uae_erpgulf.adapters.generic_ubl import GenericUBLAdapter


class TronstrideASPAdapter(BaseASPAdapter):
    """
    Adapter for TronStride FZC / Aigentrix (aigentrix.ai / tronstride.com).
    Approved UAE Ministry of Finance & FTA E-Invoicing Service Provider (ASP).
    Offers self-service developer access, direct API key auth, and PINT-AE UBL 2.1 Schematron validation.
    """

    DEFAULT_BASE_URL = "https://api.aigentrix.ai"
    DEFAULT_SANDBOX_URL = "https://sandbox-api.aigentrix.ai"

    def __init__(self, company: str, settings: Optional[Dict[str, Any]] = None):
        super().__init__(company, settings)
        self.company_doc = frappe.get_cached_doc("Company", company)
        
        # Base URL resolution
        self.base_url = self._base_url(self.DEFAULT_BASE_URL)
        
        # Credentials
        self.api_key = self._secret("custom_asp_api_key")
        self.participant_id = getattr(self.company_doc, "custom_participant_id", None)
        self._ubl_builder = GenericUBLAdapter(company, settings)

    def _get_headers(self, content_type: str = "application/json") -> Dict[str, str]:
        """Resolves authentication headers using TronStride's direct API Key standard."""
        if not self.api_key:
            frappe.throw(_("TronStride (Aigentrix) API Key is missing in Company settings for {0}").format(self.company))

        return {
            "Content-Type": content_type,
            "x-api-key": self.api_key.strip(),
            "Accept": "application/json",
            "User-Agent": "ERPGulf-UAE-EInvoicing/2.0"
        }

    def test_connection(self) -> Tuple[bool, str]:
        """
        Verifies API connection with TronStride / Aigentrix using the configured API Key.
        """
        try:
            # Check ping / verify endpoint
            url = f"{self.base_url}/api/v1/ping"
            headers = self._get_headers()
            response = requests.get(url, headers=headers, timeout=15)
            
            # If 200 or 404 (if ping path differs but key accepted)
            if response.status_code in (200, 204):
                return True, "TronStride (Aigentrix) API connection authenticated successfully."
            
            # Try alternate auth verify path
            verify_url = f"{self.base_url}/api/v1/auth/verify"
            verify_resp = requests.get(verify_url, headers=headers, timeout=15)
            if verify_resp.status_code in (200, 204):
                return True, "TronStride (Aigentrix) API authenticated successfully."

            if response.status_code in (401, 403) or verify_resp.status_code in (401, 403):
                return False, f"TronStride Authentication Failed (Invalid API Key). Status: {response.status_code}"

            return False, f"TronStride API Error ({response.status_code}): {response.text[:200]}"
        except Exception as e:
            return False, f"TronStride Connection Exception: {str(e)}"

    def transform_payload(self, canonical_doc: Dict[str, Any]) -> Dict[str, Any]:
        """
        Transforms Canonical Model into TronStride Aigentrix payload.
        TronStride natively accepts standard UAE PINT-AE UBL 2.1 XML directly or JSON wrapper.
        """
        # Generate standard PEPPOL UBL 2.1 XML
        ubl_xml = self._ubl_builder.transform_payload(canonical_doc)

        seller = canonical_doc.get("seller", {})
        buyer = canonical_doc.get("buyer", {})
        totals = canonical_doc.get("monetary_totals", {})

        return {
            "document_type": canonical_doc.get("document_type", "388"),
            "document_id": canonical_doc.get("document_identifier"),
            "issue_date": canonical_doc.get("issue_date"),
            "issue_time": canonical_doc.get("issue_time"),
            "currency": canonical_doc.get("document_currency", "AED"),
            "supplier_trn": seller.get("tax_id"),
            "customer_trn": buyer.get("tax_id"),
            "total_amount": totals.get("payable_amount"),
            "tax_amount": totals.get("tax_amount"),
            "ubl_xml": ubl_xml,
            "metadata": {
                "peppol_profile": "PINT-AE-v1.04"
            }
        }

    def submit_document(self, canonical_doc: Dict[str, Any]) -> Dict[str, Any]:
        """
        Transmits invoice to TronStride Aigentrix submission endpoint.
        """
        url = f"{self.base_url}/api/v1/invoices/submit"
        payload = self.transform_payload(canonical_doc)
        headers = self._get_headers(content_type="application/json")

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=60)
            
            try:
                response_data = response.json()
            except Exception:
                response_data = {"raw_response": response.text}

            is_success = response.status_code in (200, 201, 202)
            data = response_data.get("data", response_data) if isinstance(response_data, dict) else {}

            doc_id = data.get("document_id") or data.get("id") or data.get("uuid")
            # Passed through unchanged: acceptance by TronStride is not FTA reporting
            reporting_status = data.get("reporting_status") or data.get("status")
            exchange_status = data.get("exchange_status")

            return {
                "success": is_success,
                "status_code": response.status_code,
                "document_id": doc_id,
                "reporting_status": reporting_status,
                "exchange_status": exchange_status,
                "response_data": response_data,
                "error_message": None if is_success else (response_data.get("message") or response.text[:200]),
            }
        except requests.exceptions.RequestException as e:
            return network_failure_result(e)

    def get_document_status(self, document_id: str) -> Dict[str, Any]:
        """Queries TronStride document inquiry endpoint."""
        url = f"{self.base_url}/api/v1/invoices/{document_id}/status"
        try:
            response = requests.get(url, headers=self._get_headers(), timeout=30)
        except requests.exceptions.RequestException as e:
            return network_failure_result(e)
        return document_status_result(response)

    def get_document_artifacts(self, document_id: str) -> Dict[str, Optional[bytes]]:
        """Downloads cleared XML and PDF artifacts from TronStride."""
        artifacts: Dict[str, Optional[bytes]] = {"xml": None, "pdf": None}
        headers = self._get_headers()

        # XML
        try:
            xml_url = f"{self.base_url}/api/v1/invoices/{document_id}/xml"
            xml_resp = requests.get(xml_url, headers=headers, timeout=30)
            if xml_resp.status_code == 200:
                artifacts["xml"] = xml_resp.content
        except Exception:
            pass

        # PDF
        try:
            pdf_url = f"{self.base_url}/api/v1/invoices/{document_id}/pdf"
            pdf_resp = requests.get(pdf_url, headers=headers, timeout=30)
            if pdf_resp.status_code == 200:
                artifacts["pdf"] = pdf_resp.content
        except Exception:
            pass

        return artifacts

    def parse_webhook_payload(self, raw_data: bytes, headers: Dict[str, str]) -> Dict[str, Any]:
        """Parses TronStride / Aigentrix inbound webhook event."""
        data = json.loads(raw_data.decode("utf-8") if isinstance(raw_data, bytes) else raw_data)
        doc_data = data.get("data", data)
        return {
            "event_type": data.get("event") or data.get("event_type", "invoice.received"),
            "participant_id": data.get("participant_id") or doc_data.get("receiver_peppol_id"),
            "document_id": doc_data.get("document_id") or doc_data.get("id"),
            "invoice_number": doc_data.get("invoice_number") or doc_data.get("document_identifier"),
            "reporting_status": doc_data.get("status") or doc_data.get("reporting_status"),
            "exchange_status": doc_data.get("exchange_status"),
            "status": doc_data.get("status"),
        }
