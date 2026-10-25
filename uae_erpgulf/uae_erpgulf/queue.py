import frappe
import json
import requests
from frappe import _
from frappe.utils import add_to_date, cint, now_datetime
from typing import Optional, Union
from frappe.model.document import Document
from uae_erpgulf.adapters.base import (
    FAILED,
    PENDING,
    REPORTED,
    RETRYABLE_STATUS_CODES,
    classify_reporting_status,
)

# Statuses after which the invoice must never be transmitted again
FINAL_STATUSES = ("Success", "Reported", "Cleared")
# Statuses while a transmission is queued or running
IN_FLIGHT_STATUSES = ("Pending Submission", "Submitted to ASP", "Retry Pending")
# Inbound purchase invoices were issued by the supplier and already cleared on their side
INBOUND_STATUSES = ("Inbound Cleared",)
# Issued before the company's go-live (marked by the v2.0 migration): never transmitted
HISTORICAL_STATUS = "Pre-Mandate / Historical"

MAX_RETRIES = 3
RETRY_BASE_SECONDS = 60  # 60s, 120s, 240s

# Queued and in-flight invoices hold a lease (custom_einvoice_next_retry). If it expires, the job
# was lost or the worker died mid-send, and retry_pending_submissions recovers the invoice.
IN_FLIGHT_LEASE_MINUTES = 30

# Accepted-but-not-yet-reported invoices are polled at 5, 10, 20, 40 then every 60 minutes,
# for up to POLL_MAX_ATTEMPTS checks (~3 days). Wafeq Sandbox never reports to the FTA, so
# without a cap its invoices would be polled forever.
POLL_BASE_MINUTES = 5
POLL_MAX_MINUTES = 60
POLL_MAX_ATTEMPTS = 75
POLL_BATCH_SIZE = 50
NO_DOCUMENT_ID = ("", "Not Submitted")


def should_transmit(doc: Document) -> bool:
    """
    Sales Invoices are always issued by the company. Purchase Invoices are only
    transmitted when they are self-billed (Submit To FTA ticked); ordinary supplier
    bills are issued by the supplier and arrive inbound.
    """
    if getattr(doc, "custom_uae_einvoice_status", None) == HISTORICAL_STATUS:
        return False
    if doc.doctype == "Purchase Invoice":
        if getattr(doc, "custom_uae_einvoice_status", None) in INBOUND_STATUSES:
            return False
        return bool(cint(getattr(doc, "custom_submit_to_fta", 0)))
    return True


def enqueue_invoice_submission(doc: Document, method: Optional[str] = None):
    """
    On Document Submit Hook:
    Enqueues the invoice for background submission to the Accredited Service Provider (ASP).
    Guarantees that ERPNext document submission completes immediately without blocking UI.
    """
    company = getattr(doc, "company", None)
    if not company:
        return

    company_doc = frappe.get_cached_doc("Company", company)
    if not getattr(company_doc, "custom_uae_einvoice_enabled", 0):
        return

    # Go-Live Date Guard: past/backfilled QuickBooks invoices bypass e-invoicing
    go_live_date = getattr(company_doc, "custom_einvoice_go_live_date", None)
    if go_live_date and doc.posting_date and str(doc.posting_date) < str(go_live_date):
        return

    if getattr(doc.flags, "ignore_einvoice", False):
        return

    if not should_transmit(doc):
        return

    # Idempotency check: don't re-queue already reported/cleared invoices
    current_status = getattr(doc, "custom_uae_einvoice_status", None)
    if current_status in FINAL_STATUSES:
        return

    # Set status to Pending Submission
    doc.db_set({"custom_uae_einvoice_status": "Pending Submission", "custom_einvoice_next_retry": _lease_expiry()})

    # Enqueue background worker with enqueue_after_commit=True for database atomicity
    frappe.enqueue(
        "uae_erpgulf.uae_erpgulf.queue.process_einvoice_background",
        queue="short",
        doctype=doc.doctype,
        docname=doc.name,
        retry_count=0,
        enqueue_after_commit=True
    )

    frappe.msgprint(
        _("Invoice submitted locally. UAE E-Invoice transmission has been queued in the background."),
        alert=True
    )


def process_einvoice_background(doctype: str, docname: str, retry_count: int = 0):
    """
    Background worker job that executes JSON generation, ASP API transmission,
    artifact retrieval (XML/PDF), and status logging.
    """
    try:
        if not frappe.db.exists(doctype, docname):
            return

        doc = frappe.get_doc(doctype, docname)

        # Cancelled (or never submitted) documents must not be reported
        if doc.docstatus != 1:
            return

        if not should_transmit(doc):
            return

        # Idempotency guard: only one worker may send an invoice, and never a reported one
        if not _claim_for_transmission(doctype, docname):
            return
        doc.reload()

        from uae_erpgulf.core.canonical import build_canonical_uae_invoice
        from uae_erpgulf.adapters.factory import ASPFactory

        # 1. Build Canonical UAE Model
        canonical_doc = build_canonical_uae_invoice(doc)

        # 2. Resolve configured ASP Adapter
        adapter = ASPFactory.get_adapter(doc.company)

        # 3. Transmit through ASP
        result = adapter.submit_document(canonical_doc)

        response_data = result.get("response_data", {})
        document_id = result.get("document_id")
        reporting_status = result.get("reporting_status")
        exchange_status = result.get("exchange_status")

        if isinstance(response_data, dict):
            response_text = json.dumps(response_data, indent=4)
        else:
            response_text = str(response_data)

        doc.db_set("custom_submit_response", response_text)

        if result.get("success"):
            # Accepted by the ASP. Whether it is reported to the FTA comes from the reporting
            # status: "queued" / "accepted" / unknown stay pending and are polled until final.
            doc.db_set("custom_uae_einvoice_status", "Success")
            if document_id:
                doc.db_set("custom_document_id", document_id)
            reporting_status = apply_reporting_status(doc, reporting_status, adapter, poll_count=0)
            frappe.db.commit()

            # Record success log
            try:
                from uae_erpgulf.uae_erpgulf.validation import success_log
                company_doc = frappe.get_cached_doc("Company", doc.company)
                success_log(
                    title="UAE E-Invoice Submitted Successfully",
                    document_id=document_id,
                    participant_id=getattr(company_doc, "custom_participant_id", None),
                    invoice_number=doc.name,
                    reporting_status=reporting_status,
                    exchange_status=exchange_status,
                    status="Success",
                    submit_response=response_text
                )
            except Exception:
                frappe.log_error(frappe.get_traceback(), f"UAE Success Log Creation Error: {doc.name}")

        elif is_retryable(result):
            _schedule_retry(doctype, docname, retry_count, result.get("error_message") or response_text)

        else:
            doc.db_set({"custom_uae_einvoice_status": "Failed", "custom_einvoice_next_retry": None})
            frappe.log_error(
                title=f"UAE E-Invoice ASP Rejected: {doc.name}",
                message=response_text
            )

        frappe.db.commit()

    except requests.exceptions.RequestException as net_err:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), f"UAE E-Invoice Network Error for {docname}")
        _schedule_retry(doctype, docname, retry_count, str(net_err))

    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), f"UAE E-Invoice Background Processing Error for {docname}")
        frappe.db.set_value(doctype, docname, "custom_uae_einvoice_status", "Failed")
        frappe.db.set_value(doctype, docname, "custom_submit_response", str(e))
        frappe.db.commit()


def apply_reporting_status(doc: Document, provider_status: Optional[str], adapter=None, poll_count: int = 0) -> str:
    """
    Records the normalised reporting state (reported / pending / failed) of an invoice the ASP
    has accepted, schedules the next status poll while pending, and downloads the cleared
    XML / PDF once reported. Returns the normalised state.
    """
    state = classify_reporting_status(provider_status)
    values = {"custom_reporting_status": state, "custom_einvoice_next_retry": None}

    if state == PENDING and poll_count >= POLL_MAX_ATTEMPTS:
        # Stop polling; the status button (and webhooks) can still update it
        values["custom_einvoice_retry_count"] = poll_count
        frappe.log_error(
            title=f"UAE E-Invoice Still Pending: {doc.name}",
            message=f"FTA reporting still pending after {poll_count} status checks (provider status: {provider_status}). "
                    "Automatic checks stopped; use Get Document Status to check again.",
        )
    elif state == PENDING:
        delay = min(POLL_BASE_MINUTES * (2 ** poll_count), POLL_MAX_MINUTES)
        values["custom_einvoice_retry_count"] = poll_count
        values["custom_einvoice_next_retry"] = add_to_date(now_datetime(), minutes=delay)
    elif state == FAILED:
        values["custom_uae_einvoice_status"] = "Failed"
        frappe.log_error(
            title=f"UAE E-Invoice Reporting Failed: {doc.name}",
            message=f"Provider reporting status: {provider_status}",
        )

    doc.db_set(values, update_modified=False)

    document_id = doc.get("custom_document_id")
    if state == REPORTED and adapter and document_id not in NO_DOCUMENT_ID:
        try:
            from uae_erpgulf.uae_erpgulf.attach import save_einvoice_artifacts
            save_einvoice_artifacts(doc, adapter.get_document_artifacts(document_id))
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"UAE Artifact Download Error: {doc.name}")

    return state


def refresh_reporting_status(doctype: str, docname: str) -> dict:
    """Asks the invoice's ASP for its current status and applies it. Returns the adapter result."""
    from uae_erpgulf.adapters.factory import ASPFactory

    doc = frappe.get_doc(doctype, docname)
    document_id = doc.get("custom_document_id")
    if not document_id or document_id in NO_DOCUMENT_ID:
        frappe.throw(_("Invoice {0} has not been accepted by the service provider yet.").format(docname))

    adapter = ASPFactory.get_adapter(doc.company)
    result = adapter.get_document_status(document_id)
    doc.db_set(
        "custom_document_status_response",
        json.dumps(result.get("response_data"), indent=4, default=str),
        update_modified=False,
    )

    next_poll = cint(doc.get("custom_einvoice_retry_count")) + 1
    if result.get("success"):
        result["state"] = apply_reporting_status(doc, result.get("reporting_status"), adapter, poll_count=next_poll)
    else:
        result["state"] = doc.get("custom_reporting_status")
        if result["state"] == PENDING:
            # Status lookup failed: keep polling, but back off
            apply_reporting_status(doc, None, poll_count=next_poll)
    return result


def poll_pending_reporting():
    """Scheduler job: polls the ASP for accepted invoices whose FTA reporting is still pending."""
    for doctype in ("Sales Invoice", "Purchase Invoice"):
        due = frappe.get_all(
            doctype,
            filters={
                "docstatus": 1,
                "custom_uae_einvoice_status": "Success",
                "custom_reporting_status": PENDING,
                "custom_document_id": ["not in", list(NO_DOCUMENT_ID)],
                "custom_einvoice_next_retry": ["<=", now_datetime()],
            },
            pluck="name",
            order_by="custom_einvoice_next_retry asc",
            limit=POLL_BATCH_SIZE,
        )
        for name in due:
            try:
                refresh_reporting_status(doctype, name)
                frappe.db.commit()
            except Exception:
                frappe.db.rollback()
                frappe.log_error(frappe.get_traceback(), f"UAE E-Invoice Status Poll Error: {name}")


@frappe.whitelist()
def refresh_einvoice_status(doctype: str, docname: str):
    """Status button: fetches the latest status from the invoice's configured service provider."""
    from uae_erpgulf.core.credentials import check_doc_permission

    if doctype not in ("Sales Invoice", "Purchase Invoice"):
        frappe.throw(_("UAE E-Invoicing is only supported for Sales and Purchase Invoices."))
    check_doc_permission(doctype, docname, "read")

    result = refresh_reporting_status(doctype, docname)
    frappe.db.commit()

    messages = {
        REPORTED: _("Reported to the FTA."),
        PENDING: _("Accepted by the service provider; FTA reporting is still pending."),
        FAILED: _("Reporting failed. Check the status response, correct the invoice and send it again."),
    }
    return {
        "status": "success" if result.get("success") else "error",
        "message": result.get("error_message") if not result.get("success") else messages.get(result.get("state")),
        "data": {
            "id": frappe.db.get_value(doctype, docname, "custom_document_id"),
            "reporting_status": result.get("state"),
            "provider_status": result.get("reporting_status"),
            "exchange_status": result.get("exchange_status"),
            "reporting_reference": result.get("reporting_reference"),
        },
    }


def _lease_expiry():
    return add_to_date(now_datetime(), minutes=IN_FLIGHT_LEASE_MINUTES)


def _claim_for_transmission(doctype: str, docname: str) -> bool:
    """
    Atomically marks the invoice "Submitted to ASP" unless it is already reported or another
    worker is sending it (the row lock serialises concurrent jobs for the same invoice).
    """
    row = frappe.db.sql(
        f"SELECT custom_uae_einvoice_status FROM `tab{doctype}` WHERE name = %s FOR UPDATE", docname
    )
    if not row or row[0][0] in FINAL_STATUSES + ("Submitted to ASP",):
        frappe.db.rollback()
        return False
    frappe.db.set_value(
        doctype,
        docname,
        {"custom_uae_einvoice_status": "Submitted to ASP", "custom_einvoice_next_retry": _lease_expiry()},
        update_modified=False,
    )
    frappe.db.commit()
    return True


def is_retryable(result: dict) -> bool:
    """Transient ASP failures (network, throttling, 5xx) are retried; validation rejections are not."""
    return bool(result.get("retryable")) or result.get("status_code") in RETRYABLE_STATUS_CODES


def _schedule_retry(doctype: str, docname: str, retry_count: int, message: str):
    """
    Marks the invoice for a delayed retry with exponential backoff.
    frappe.enqueue cannot delay jobs, so retry_pending_submissions picks these up on schedule.
    """
    if retry_count < MAX_RETRIES:
        delay = RETRY_BASE_SECONDS * (2 ** retry_count)
        frappe.db.set_value(
            doctype,
            docname,
            {
                "custom_uae_einvoice_status": "Retry Pending",
                "custom_einvoice_retry_count": retry_count + 1,
                "custom_einvoice_next_retry": add_to_date(now_datetime(), seconds=delay),
                "custom_submit_response": f"Transient failure, retry {retry_count + 1} of {MAX_RETRIES} scheduled: {message}",
            },
            update_modified=False,
        )
    else:
        frappe.db.set_value(
            doctype,
            docname,
            {
                "custom_uae_einvoice_status": "Failed",
                "custom_einvoice_next_retry": None,
                "custom_submit_response": f"Failure after {MAX_RETRIES} retry attempts: {message}",
            },
            update_modified=False,
        )
    frappe.db.commit()


def retry_pending_submissions():
    """Scheduler job: re-enqueues invoices whose retry backoff has elapsed."""
    for doctype in ("Sales Invoice", "Purchase Invoice"):
        due = frappe.get_all(
            doctype,
            filters={
                "docstatus": 1,
                "custom_uae_einvoice_status": "Retry Pending",
                "custom_einvoice_next_retry": ["<=", now_datetime()],
            },
            fields=["name", "custom_einvoice_retry_count"],
        )
        for row in due:
            # Flip the status first so the next scheduler tick does not enqueue it twice
            frappe.db.set_value(
                doctype,
                row.name,
                {"custom_uae_einvoice_status": "Pending Submission", "custom_einvoice_next_retry": _lease_expiry()},
                update_modified=False,
            )
            frappe.enqueue(
                "uae_erpgulf.uae_erpgulf.queue.process_einvoice_background",
                queue="short",
                doctype=doctype,
                docname=row.name,
                retry_count=cint(row.custom_einvoice_retry_count),
                enqueue_after_commit=True,
            )
        _recover_stale_submissions(doctype)
    frappe.db.commit()


def _recover_stale_submissions(doctype: str):
    """
    Invoices whose lease expired: a queued job that never ran is simply queued again. One that
    was mid-send is re-sent only through a provider with idempotent submission (the same
    Idempotency-Key cannot create a second invoice); otherwise it is failed for a manual check,
    because it may already have reached the provider.
    """
    from uae_erpgulf.adapters.factory import ASPFactory

    stale = frappe.get_all(
        doctype,
        filters={
            "docstatus": 1,
            "custom_uae_einvoice_status": ["in", ["Pending Submission", "Submitted to ASP"]],
            "custom_einvoice_next_retry": ["<=", now_datetime()],
        },
        fields=["name", "company", "custom_uae_einvoice_status", "custom_einvoice_retry_count"],
    )
    for row in stale:
        provider = frappe.get_cached_value("Company", row.company, "custom_accredited_service_providers")
        adapter_cls = ASPFactory.adapter_class(provider)
        if row.custom_uae_einvoice_status == "Submitted to ASP" and not getattr(adapter_cls, "IDEMPOTENT_SUBMISSION", False):
            frappe.db.set_value(
                doctype,
                row.name,
                {
                    "custom_uae_einvoice_status": "Failed",
                    "custom_einvoice_next_retry": None,
                    "custom_submit_response": f"Interrupted while sending to {provider}. It may have reached the "
                                              "provider: check its portal before sending it again.",
                },
                update_modified=False,
            )
            continue

        frappe.db.set_value(
            doctype,
            row.name,
            {"custom_uae_einvoice_status": "Pending Submission", "custom_einvoice_next_retry": _lease_expiry()},
            update_modified=False,
        )
        frappe.enqueue(
            "uae_erpgulf.uae_erpgulf.queue.process_einvoice_background",
            queue="short",
            doctype=doctype,
            docname=row.name,
            retry_count=cint(row.custom_einvoice_retry_count),
            enqueue_after_commit=True,
        )


def validate_einvoice_cancellation(doc: Document, method: Optional[str] = None):
    """
    Before Cancel Hook:
    Enforces UAE FTA regulatory immutability.
    Reported / Cleared invoices cannot be cancelled directly in the ERP.
    Must be reversed via an official Credit Note (Type 381).
    """
    einvoice_status = getattr(doc, "custom_uae_einvoice_status", None)
    reporting_status = getattr(doc, "custom_reporting_status", None)

    if (
        einvoice_status in FINAL_STATUSES
        or reporting_status in ["reported", "cleared", "delivered", "processed"]
    ):
        doc_id = getattr(doc, "custom_document_id", None) or "Reported"
        frappe.throw(
            _(
                "Invoice <b>{0}</b> has already been reported to the UAE Federal Tax Authority / ASP (ID: {1}).<br><br>"
                "Under UAE FTA e-invoicing compliance regulations, reported/cleared tax invoices cannot be cancelled directly.<br>"
                "Please create a <b>Credit Note</b> (Document Type 381) against this invoice to reverse it legally."
            ).format(doc.name, doc_id)
        )

    if einvoice_status in IN_FLIGHT_STATUSES:
        frappe.throw(
            _(
                "Invoice <b>{0}</b> is being transmitted to the UAE ASP (status: {1}). "
                "Wait for the submission to finish before cancelling; if it is reported, issue a Credit Note instead."
            ).format(doc.name, einvoice_status)
        )


@frappe.whitelist()
def manual_retry_submission(doctype: str, docname: str):
    """API endpoint allowing users to trigger manual background re-submission from Desk."""
    if doctype not in ("Sales Invoice", "Purchase Invoice"):
        frappe.throw(_("UAE E-Invoicing is only supported for Sales and Purchase Invoices."))
    if not frappe.has_permission(doctype, "write", doc=docname):
        frappe.throw(_("Not permitted"), frappe.PermissionError)

    doc = frappe.get_doc(doctype, docname)
    if doc.docstatus != 1:
        frappe.throw(_("Only submitted documents can be sent for UAE E-Invoicing."))

    status = doc.get("custom_uae_einvoice_status")
    if status in FINAL_STATUSES:
        frappe.throw(
            _("Invoice {0} has already been reported (status: {1}). Re-sending would report it twice.").format(
                docname, status
            )
        )
    if status in ("Pending Submission", "Submitted to ASP"):
        frappe.throw(_("Invoice {0} is already being transmitted.").format(docname))
    if not should_transmit(doc):
        frappe.throw(_("Only self-billed purchase invoices (Submit To FTA ticked) are transmitted to the ASP."))

    _queue_for_transmission(doc)

    return {"status": "queued", "message": _("Invoice re-submission queued in background.")}


def _queue_for_transmission(doc: Document):
    doc.db_set({
        "custom_uae_einvoice_status": "Pending Submission",
        "custom_einvoice_retry_count": 0,
        "custom_einvoice_next_retry": _lease_expiry(),
    })
    frappe.enqueue(
        "uae_erpgulf.uae_erpgulf.queue.process_einvoice_background",
        queue="short",
        doctype=doc.doctype,
        docname=doc.name,
        retry_count=0,
        enqueue_after_commit=True
    )


def bulk_queue_invoices(doctype: str, invoices) -> dict:
    """
    Bulk action from the invoice list: submits drafts (the on_submit hook queues them) and
    queues already-submitted invoices, all through the same background path as a single
    submission. Each invoice is isolated in a savepoint so one failure does not undo the others.
    """
    from uae_erpgulf.core.credentials import check_doc_permission

    if isinstance(invoices, str):
        invoices = frappe.parse_json(invoices)

    success, skipped, failed = [], [], []

    for name in invoices or []:
        savepoint = f"uae_bulk_{frappe.generate_hash(length=8)}"
        frappe.db.savepoint(savepoint)
        try:
            check_doc_permission(doctype, name, "write")
            doc = frappe.get_doc(doctype, name)

            if not frappe.get_cached_value("Company", doc.company, "custom_uae_einvoice_enabled"):
                skipped.append(f"{name} : UAE e-invoicing is not enabled for {doc.company}")
                continue

            status = doc.get("custom_uae_einvoice_status")
            if status in FINAL_STATUSES:
                skipped.append(f"{name} : already reported")
                continue
            if status in ("Pending Submission", "Submitted to ASP"):
                skipped.append(f"{name} : already being transmitted")
                continue
            if not should_transmit(doc):
                skipped.append(f"{name} : not self-billed (Submit To FTA unticked)")
                continue

            if doc.docstatus == 0:
                doc.submit()
            elif doc.docstatus == 1:
                _queue_for_transmission(doc)
            else:
                skipped.append(f"{name} : cancelled")
                continue

            success.append(name)

        except Exception as e:
            frappe.db.rollback(save_point=savepoint)
            frappe.log_error(frappe.get_traceback(), f"UAE E-Invoice Bulk Queue Error: {name}")
            failed.append(f"{name} : {e}")

    return {"success": success, "skipped": skipped, "failed": failed}
