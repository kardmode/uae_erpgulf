import base64
import hashlib
import hmac
import re
import frappe
import requests
import json
from frappe import _
from frappe.utils import get_datetime, now_datetime
from datetime import timedelta
from uae_erpgulf.adapters.base import FAILED, PENDING, classify_reporting_status
from uae_erpgulf.core.credentials import (
    check_company_permission,
    get_company_secret,
    get_flick_headers,
    set_company_secret,
)

WEBHOOK_ENDPOINT = "/api/method/uae_erpgulf.uae_erpgulf.webhook.flick_webhook_listener"
REDACTED = "*****"


@frappe.whitelist(allow_guest=True, methods=["POST"])# nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
def flick_webhook_listener(token: str = None):
    """
    Listener for Flick API webhooks. Logs incoming data and updates invoice status.

    The endpoint is public, so every call must prove it comes from the subscription we
    registered: the per-company token embedded in the endpoint URL, plus an HMAC signature
    of the body when the Company has a signature header configured. Unauthenticated calls
    are rejected before anything is logged or updated.
    """
    raw_body = frappe.request.get_data() or b""
    company = _authenticate_webhook(token or frappe.form_dict.get("token"), raw_body)

    try:
        raw_data = raw_body.decode("utf-8")
        data = json.loads(raw_data)

        # 🔹 Extract top-level fields
        event_type = data.get("event")
        participant_id = data.get("participant_id")

        company_participant = frappe.db.get_value("Company", company, "custom_participant_id")
        if participant_id and company_participant and participant_id != company_participant:
            frappe.throw(_("Webhook participant does not match the subscribed company"), frappe.PermissionError)

        # 🔹 Extract nested data
        doc_data = data.get("data", {})

        document_id = doc_data.get("document_id")
        status = doc_data.get("status")
        exchange_status = doc_data.get("exchange_status")
        reporting_status = doc_data.get("reporting_status")
        invoice_number = doc_data.get("document_identifier")

        # ✅ Create Webhook Log Doc
        doc = frappe.get_doc({
            "doctype": "UAE E-Invoice Webhook Logs",
            "webhook_response": raw_data,
            "document_id": document_id,
            "participant_id": participant_id,
            "event_type": event_type,
            "reporting_status": reporting_status,
            "exchange_status": exchange_status,
            "invoice_number":invoice_number,
            "status": status
        })

        doc.insert(ignore_permissions=True)
        if document_id and reporting_status:
            # Same normalisation as polling: only final provider states change the invoice
            state = classify_reporting_status(reporting_status)

            # Only invoices of the authenticated company can be updated by its webhook
            for doctype in ("Sales Invoice", "Purchase Invoice"):
                invoice = frappe.db.get_value(
                    doctype,
                    {"custom_document_id": document_id, "company": company},
                    "name"
                )
                if invoice:
                    update_vals = {"custom_reporting_status": state}
                    if state != PENDING:
                        update_vals["custom_einvoice_next_retry"] = None
                    if state == FAILED:
                        update_vals["custom_uae_einvoice_status"] = "Failed"
                    frappe.db.set_value(
                        doctype,
                        invoice,
                        update_vals,
                        update_modified=False
                    )

        frappe.db.commit()

        return {
            "acknowledged": True,
            "processed": True
        }

    except frappe.PermissionError:
        raise

    except Exception:
        frappe.log_error(
            title="Webhook Processing Error",
            message=frappe.get_traceback()
        )
        return {
            "acknowledged": False,
            "processed": False
        }


def _authenticate_webhook(token, raw_body: bytes) -> str:
    """Returns the Company the webhook belongs to, or rejects the request (401/403)."""
    if not token:
        frappe.throw(_("Missing webhook token"), frappe.AuthenticationError)

    company = None
    for name in frappe.get_all("Company", filters={"custom_webhook_token": ["is", "set"]}, pluck="name"):
        expected = get_company_secret(name, "custom_webhook_token")
        if expected and hmac.compare_digest(expected.encode(), str(token).encode()):
            company = name
            break

    if not company:
        frappe.throw(_("Invalid webhook token"), frappe.AuthenticationError)

    signature_header = frappe.db.get_value("Company", company, "custom_webhook_signature_header")
    if signature_header:
        secret = get_company_secret(company, "custom_secret_of_webhook")
        provided = frappe.get_request_header(signature_header.strip())
        if not secret or not provided or not _signature_matches(secret, raw_body, provided):
            frappe.throw(_("Invalid webhook signature"), frappe.AuthenticationError)

    return company


def _signature_matches(secret: str, raw_body: bytes, provided: str) -> bool:
    """HMAC-SHA256 of the raw body, sent as hex or base64, optionally prefixed 'sha256='."""
    provided = provided.strip()
    if provided.lower().startswith("sha256="):
        provided = provided[len("sha256="):]
    digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    candidates = (digest.hex(), base64.b64encode(digest).decode())
    return any(hmac.compare_digest(provided.encode(), c.encode()) for c in candidates)


def _redact(response_data):
    """
    Never store or return webhook credentials in plain text: the subscription secret,
    and the endpoint token that Flick echoes back inside endpoint URLs.
    """
    if isinstance(response_data, dict) and isinstance(response_data.get("data"), dict):
        if response_data["data"].get("secret"):
            response_data = dict(response_data, data=dict(response_data["data"], secret=REDACTED))
    text = re.sub(r"(token=)[A-Za-z0-9]+", r"\g<1>" + REDACTED, json.dumps(response_data))
    return json.loads(text)


def update_webhook_logs():
    companies = frappe.get_all(
        "Company",
        filters={
            "custom_uuid_of_webhook": ["is", "set"]
        },
        pluck="name"
    )

    for company in companies:
        try:
            fetch_webhook_deliveries(company)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"Webhook Log Update Failed - {company}"
            )

@frappe.whitelist(allow_guest=False)
def register_flick_webhook(company: str = None):
    check_company_permission(company)
    company_doc = frappe.get_doc("Company", company)
    base_url = company_doc.custom_base_url
    url = f"{base_url}/v1/webhooks/subscriptions"
    participant_id = company_doc.custom_participant_id

    headers = get_flick_headers(company_doc.name)

    # A fresh token per subscription; only stored once Flick accepts the subscription
    token = frappe.generate_hash(length=48)
    endpoint = frappe.utils.get_url(f"{WEBHOOK_ENDPOINT}?token={token}")
    payload = {
        "name": "ERPNext Webhook",
        "endpoint": endpoint,
        "event_types": [
        "document.received",
        "document.exchange.delivered",
        "document.exchange.failed",
        "document.reporting.reported",
        "document.reporting.failed",
        "document.completed",
        "document.failed"
            ],
        "participant_ids": [participant_id]
    }

    response = requests.post(url, headers=headers, json=payload, timeout=30)

    try:
        response_data = response.json()
    except Exception:
        response_data = {"raw_response": response.text}

    data = response_data.get("data") if isinstance(response_data, dict) else None
    if response.ok and isinstance(data, dict) and data.get("uuid"):
        company_doc.db_set("custom_uuid_of_webhook", data["uuid"])
        set_company_secret(company_doc.name, "custom_webhook_token", token)
        if data.get("secret"):
            set_company_secret(company_doc.name, "custom_secret_of_webhook", data["secret"])

    response_data = _redact(response_data)
    company_doc.db_set("custom_webhook_subscription_response", json.dumps(response_data))

    return response_data


@frappe.whitelist()
def custom_get_subscription(company: str = None):
    check_company_permission(company)
    company_doc = frappe.get_doc("Company", company)

    base_url = company_doc.custom_base_url
    uuid = company_doc.custom_uuid_of_webhook  # you must store this when creating webhook

    if not uuid:
        frappe.throw(_("Webhook UUID not found. Please create subscription first."))

    url = f"{base_url}/v1/webhooks/subscriptions/{uuid}"
    headers = get_flick_headers(company_doc.name)

    response = requests.get(url, headers=headers, timeout=30)

    try:
        response_data = response.json()
    except Exception:
        response_data = {"raw_response": response.text}

    response_data = _redact(response_data)
    company_doc.db_set("custom_get_subscription_response", json.dumps(response_data))

    return response_data

@frappe.whitelist()
def get_webhook_deliveries(company: str = None):
    check_company_permission(company)
    return fetch_webhook_deliveries(company)


def fetch_webhook_deliveries(company: str):
    company_doc = frappe.get_doc("Company", company)

    base_url = company_doc.custom_base_url
    uuid = company_doc.custom_uuid_of_webhook

    url = f"{base_url}/v1/webhooks/subscriptions/{uuid}/deliveries"
    headers = get_flick_headers(company_doc.name)

    response = requests.get(url, headers=headers, timeout=30)

    try:
        response_data = response.json()
    except Exception:
        response_data = {"raw_response": response.text}

    response_data = _redact(response_data)
    frappe.db.set_value(
        "Company",
        company_doc.name,
        "custom_webhook_delivery_logs",
        json.dumps(response_data),
        update_modified=False
    )
    # company_doc.save(ignore_permissions=True)

    return response_data


WAFEQ_PROVIDER = "Wafeq"


@frappe.whitelist(allow_guest=True, methods=["POST"])# nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
def wafeq_webhook_listener():
    """
    Wafeq ASP webhook endpoint (register its URL under Developer -> Webhooks in the Wafeq portal).

    Events are signed with Standard Webhooks; unsigned or mis-signed calls are rejected before
    anything is stored. Redelivered events (same webhook-id) are acknowledged and ignored.
    Final FTA states update the invoice; pending events never overwrite a final state.
    """
    from uae_erpgulf.adapters.wafeq import normalize_wafeq_event

    raw_body = frappe.request.get_data() or b""
    headers = {key.lower(): value for key, value in frappe.request.headers.items()}
    company = _authenticate_wafeq_webhook(raw_body, headers)

    event = normalize_wafeq_event(json.loads(raw_body.decode("utf-8")))
    event_id = headers.get("webhook-id") or event.get("event_id")
    if event_id and frappe.db.exists("UAE E-Invoice Webhook Logs", {"event_id": event_id}):
        return {"acknowledged": True, "duplicate": True}

    frappe.get_doc({
        "doctype": "UAE E-Invoice Webhook Logs",
        "event_id": event_id,
        "webhook_response": raw_body.decode("utf-8"),
        "document_id": event.get("document_id"),
        "participant_id": event.get("participant_id"),
        "event_type": event.get("event_type"),
        "reporting_status": event.get("reporting_status"),
        "exchange_status": event.get("exchange_status"),
        "invoice_number": event.get("invoice_number"),
        "status": event.get("status"),
    }).insert(ignore_permissions=True)

    try:
        if not event.get("test"):
            if event.get("event_type") == "document.received":
                _stage_wafeq_inbound_document(company, event)
            elif event.get("document_id") and event.get("reporting_status"):
                _apply_wafeq_reporting_event(company, event)
    except Exception:
        # Stored above: never make Wafeq redeliver an event we failed to apply
        frappe.log_error(frappe.get_traceback(), f"Wafeq Webhook Processing Error: {event_id}")

    frappe.db.commit()
    return {"acknowledged": True}


def _authenticate_wafeq_webhook(raw_body: bytes, headers: dict) -> str:
    """The Company whose Wafeq signing secret verifies the event (and whose taxpayer it concerns)."""
    from uae_erpgulf.adapters.wafeq import verify_standard_webhook

    try:
        taxpayer_id = (json.loads(raw_body.decode("utf-8")).get("data") or {}).get("taxpayer_id")
    except (ValueError, AttributeError):
        frappe.throw(_("Invalid webhook body"), frappe.AuthenticationError)

    candidates = frappe.get_all(
        "Company",
        filters={"custom_accredited_service_providers": WAFEQ_PROVIDER, "custom_secret_of_webhook": ["is", "set"]},
        fields=["name", "custom_wafeq_taxpayer_id"],
    )
    for row in candidates:
        if taxpayer_id and row.custom_wafeq_taxpayer_id and row.custom_wafeq_taxpayer_id != taxpayer_id:
            continue
        secret = get_company_secret(row.name, "custom_secret_of_webhook")
        if verify_standard_webhook(secret, raw_body, headers):
            return row.name

    frappe.throw(_("Invalid webhook signature"), frappe.AuthenticationError)


def _apply_wafeq_reporting_event(company: str, event: dict) -> None:
    from uae_erpgulf.adapters.factory import ASPFactory
    from uae_erpgulf.uae_erpgulf.queue import apply_reporting_status

    state = classify_reporting_status(event["reporting_status"])
    if state == PENDING:
        return  # informational; polling and later events settle it

    for doctype in ("Sales Invoice", "Purchase Invoice"):
        name = frappe.db.get_value(doctype, {"custom_document_id": event["document_id"], "company": company}, "name")
        if name:
            doc = frappe.get_doc(doctype, name)
            apply_reporting_status(doc, event["reporting_status"], ASPFactory.get_adapter(company))
            return


def _stage_wafeq_inbound_document(company: str, event: dict) -> None:
    """A supplier's e-invoice arrived: stage it, with its XML, for the inbound Purchase Invoice flow."""
    from frappe.utils.file_manager import save_file
    from uae_erpgulf.adapters.factory import ASPFactory

    document_id = event.get("document_id")
    if not document_id or frappe.db.exists("UAE Incoming Invoices", {"document_id": document_id}):
        return

    staged = frappe.get_doc({
        "doctype": "UAE Incoming Invoices",
        "document_id": document_id,
        "status": "Not Submitted",
    }).insert(ignore_permissions=True)

    xml = ASPFactory.get_adapter(company).get_document_artifacts(document_id).get("xml")
    if xml:
        file_doc = save_file(
            fname=f"Incoming-{document_id}.xml",
            content=xml,
            dt="UAE Incoming Invoices",
            dn=staged.name,
            is_private=1,
        )
        staged.db_set("incoming_invoice_file", file_doc.file_url)
