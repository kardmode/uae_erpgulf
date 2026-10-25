"""
Abstract Base Class for UAE Accredited Service Provider (ASP) Integrations.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Tuple, Optional

RETRYABLE_STATUS_CODES = (408, 425, 429, 500, 502, 503, 504)

# FTA reporting states, normalised. Anything not known to be final (queued, accepted, reporting,
# not_reported, unresolved, unknown) is "pending": accepted by the ASP is not reported to the FTA,
# and delivery to the buyer ("delivered", "reporting_only" routes) says nothing about the FTA.
REPORTED = "reported"
PENDING = "pending"
FAILED = "failed"
REPORTED_STATES = {"reported", "cleared", "processed", "completed"}
FAILED_STATES = {
    "failed", "rejected", "error", "invalid", "declined",
    "not_compliant",       # Wafeq: the FTA rejected the report
    "withdrawn",           # Wafeq: report withdrawn after the buyer's provider rejected the document
    "delivery_rejected",   # buyer's provider rejected it as invalid (BV / SV): needs a corrected document
}


def classify_reporting_status(value: Optional[str]) -> str:
    """Normalise a provider's reporting status to reported / pending / failed."""
    state = str(value or "").strip().lower()
    if state in REPORTED_STATES:
        return REPORTED
    if state in FAILED_STATES:
        return FAILED
    return PENDING


class BaseASPAdapter(ABC):
    """
    Universal contract that every ASP connector must implement.
    Allows ERPNext to communicate with any approved UAE e-invoicing service provider.
    """

    # True only when the provider documents that resending the same submission (same
    # Idempotency-Key) can never create a second invoice; then interrupted sends are retried.
    IDEMPOTENT_SUBMISSION = False

    def __init__(self, company: str, settings: Optional[Dict[str, Any]] = None):
        self.company = company
        self.settings = settings or {}

    def _base_url(self, default: str) -> str:
        """
        The Company's Base URL override, or this provider's default. A Flick URL left over
        from an earlier Flick setup is ignored so invoices never go to the wrong provider.
        """
        custom_url = getattr(self.company_doc, "custom_base_url", None)
        custom_url = custom_url.strip() if isinstance(custom_url, str) else ""
        if not custom_url or ("flick.network" in custom_url.lower() and self.__class__.__name__ != "FlickASPAdapter"):
            custom_url = default
        return custom_url.rstrip("/")

    def _secret(self, *fieldnames: str) -> Optional[str]:
        """First configured credential among the Company Password fields, decrypted."""
        from uae_erpgulf.core.credentials import get_company_secret

        for fieldname in fieldnames:
            value = get_company_secret(self.company, fieldname)
            if value:
                return value
        return None

    @abstractmethod
    def test_connection(self) -> Tuple[bool, str]:
        """
        Verify connection and authentication with the ASP (API key, OAuth2, or certificates).
        Returns: (success: bool, message: str)
        """
        pass

    @abstractmethod
    def transform_payload(self, canonical_doc: Dict[str, Any]) -> Any:
        """
        Transforms the Canonical UAE Invoice Model (CUIM) into the ASP's required payload format
        (e.g. vendor-specific JSON schema or UBL 2.1 XML).
        """
        pass

    @abstractmethod
    def submit_document(self, canonical_doc: Dict[str, Any]) -> Dict[str, Any]:
        """
        Transmits the invoice to the ASP endpoint.
        Returns a normalized dict:
        {
            "success": bool,
            "status_code": int,
            "document_id": Optional[str],
            "reporting_status": Optional[str],  # e.g. "reported", "cleared", "failed"
            "exchange_status": Optional[str],
            "response_data": Any,
            "error_message": Optional[str],
            "retryable": bool  # True for transient failures (network, throttling, 5xx)
        }
        Network errors should be returned via network_failure_result(); other
        exceptions should propagate so the invoice is marked Failed with a traceback.
        """
        pass

    @abstractmethod
    def get_document_status(self, document_id: str) -> Dict[str, Any]:
        """
        Queries the ASP API for the latest clearance/reporting status of a previously submitted document.
        Returns document_status_result(...) / network_failure_result(...):
        {"success", "status_code", "reporting_status" (provider's raw value), "exchange_status",
         "reporting_reference", "response_data", "error_message", "retryable"}
        """
        pass

    @abstractmethod
    def get_document_artifacts(self, document_id: str) -> Dict[str, Optional[bytes]]:
        """
        Downloads clearance artifacts returned by the ASP/FTA.
        Returns: {"xml": Optional[bytes], "pdf": Optional[bytes]}
        """
        pass

    @abstractmethod
    def parse_webhook_payload(self, raw_data: bytes, headers: Dict[str, str]) -> Dict[str, Any]:
        """
        Validates and parses inbound webhook events from the ASP into normalized states.
        """
        pass


def network_failure_result(exc: Exception) -> Dict[str, Any]:
    """Normalized submit result for a transport failure; the queue schedules a retry."""
    return {
        "success": False,
        "status_code": None,
        "document_id": None,
        "reporting_status": None,
        "exchange_status": None,
        "response_data": {"error": str(exc)},
        "error_message": str(exc),
        "retryable": True,
    }


def party_address(party: Dict[str, Any]) -> Dict[str, Any]:
    """Address view of a canonical party (canonical parties store address fields flat)."""
    return {
        "street": party.get("street_address") or "",
        "city": party.get("city") or "",
        "postal_code": party.get("postal_code") or "",
        "country": party.get("country_code") or "",
        "state": party.get("emirate_code") or "",
    }


def value_or(mapping: Dict[str, Any], key: str, default: Any) -> Any:
    """mapping[key] unless it is missing or None. Unlike `or`, keeps legitimate zeros (0% VAT)."""
    value = mapping.get(key)
    return default if value is None else value


def document_status_result(response) -> Dict[str, Any]:
    """Normalized get_document_status result from an ASP HTTP response (body at root or under "data")."""
    try:
        body = response.json()
    except ValueError:
        body = {"raw_response": response.text}
    data = body.get("data", body) if isinstance(body, dict) else {}
    if not isinstance(data, dict):
        data = {}

    ok = response.status_code == 200
    return {
        "success": ok,
        "status_code": response.status_code,
        "reporting_status": (data.get("reporting_status") or data.get("status")) if ok else None,
        "exchange_status": data.get("exchange_status") if ok else None,
        "reporting_reference": data.get("reporting_reference") if ok else None,
        "response_data": body,
        "error_message": None if ok else response.text[:200],
        "retryable": response.status_code in RETRYABLE_STATUS_CODES,
    }
