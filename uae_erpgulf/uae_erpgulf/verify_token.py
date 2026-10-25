

import frappe
import http.client
import json
import requests
# from pydoc import doc
from frappe import _
# from datetime import now_datetime
from frappe.utils import now_datetime
import pytz
from uae_erpgulf.core.credentials import (
    check_company_permission,
    check_doc_permission,
    get_company_secret,
    get_flick_headers,
    set_company_secret,
)



@frappe.whitelist(allow_guest=False)
def verify_flick_token(company:str):
    """Verify Flick token using fields inside Company DocType"""
    check_company_permission(company)

    doc = frappe.get_doc("Company", company)

    base_url = doc.custom_base_url

    if not base_url :
        frappe.throw(_("Please enter Base URL ."))

    auth_key = get_company_secret(company, "custom_xflickauthkey")
    access_token = get_company_secret(company, "custom_access_token")
    if auth_key:
        headers = {
            "X-Flick-Auth-Key": auth_key
        }

    # Case 2: Fallback to Access Token
    elif access_token:
        headers = {
            "Authorization": f"Bearer {access_token}"
        }

    # Case 3: Neither available
    else:
        frappe.throw(_("Both X-Flick Auth Key and Access Token are missing in Company"))
    try:
        url = f"{base_url}/v1/auth/verify"
        response = requests.get(url, headers=headers, timeout=30)

        try:
            response_text = json.dumps(response.json())  # compact clean JSON string
        except Exception:
            response_text = response.text
        doc.db_set("custom_token_response", response_text)
        return {
            "status": "success",
            "response": response_text
        }

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Flick Verify Error")
        return {
            "status": "error",
            "message": str(e)
        }

@frappe.whitelist(allow_guest=False)
def get_participant_details(company:str):
    """Fetch participant details from Flick API and save response in Company DocType"""
    check_company_permission(company)
    company_doc = frappe.get_doc("Company", company)
    base_url = company_doc.custom_base_url
    if not base_url:
        frappe.throw(_("Please enter Base URL and X-Flick-Auth-Key in Company."))
    participant_id = company_doc.custom_participant_id
    if not participant_id:
        frappe.throw(_("Participant ID is missing in Company"))

    url = f"{base_url}/v1/participants/{participant_id}"
    headers = get_flick_headers(company_doc.name)

    response = requests.get(url, headers=headers, timeout=30)
    data = response.json()
    company_doc.db_set("custom_participant_details_response", json.dumps(data))
    return {
        "status": "success",
        "response": data
    }



from frappe.utils import get_datetime, now_datetime
from datetime import timedelta


@frappe.whitelist(allow_guest=False)
def get_flick_access_token(company:str):
    """Fetch and store a Flick access token. The token itself is never returned to the browser."""
    check_company_permission(company)
    result = fetch_flick_access_token(company)
    if not result.get("access_token"):
        return {"status": "error", "message": result.get("message")}
    return {"status": "success"}


def fetch_flick_access_token(company:str):
    """Fetch access token from Flick API using Client ID and Client Secret stored in Company DocType"""
    doc = frappe.get_doc("Company", company)
    base_url = doc.custom_base_url
    client_id = doc.custom_client_id
    client_secret = get_company_secret(company, "custom_client_secret")
    if not base_url or not client_id or not client_secret:
        frappe.throw(_("Please enter Base URL, Client ID and Client Secret in Company."))
    url = f"{base_url}/v1/oauth/token"
    headers = {
        "Content-Type": "application/json"
    }

    payload = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()  # raises error for bad responses
        response_json = response.json()
        access_token = response_json.get("access_token")

        if not access_token:
            frappe.throw(_("Access token not found in response"))
          # default 1 hour
        dubai_tz = pytz.timezone("Asia/Dubai")
        current_time = now_datetime().astimezone(dubai_tz)

        # ✅ Store ACTUAL expiry
        expiry_time = current_time + timedelta(hours=1)

        set_company_secret(company, "custom_access_token", access_token)
        doc.db_set("custom_token_expiry_time", expiry_time)


        frappe.db.commit()
        return {"access_token": access_token}

    except requests.exceptions.RequestException as e:
        return {
            "status": "error",
            "message": str(e)
        }


def get_valid_flick_token(company):
    company_doc = frappe.get_doc("Company", company)

    current_time = now_datetime()
    expiry_time = get_datetime(company_doc.custom_token_expiry_time)
    access_token = get_company_secret(company, "custom_access_token")

    # ✅ If token is valid → return it
    if (
        access_token
        and expiry_time
        and current_time < expiry_time
    ):
        return access_token

    # ❌ Token expired → fetch new
    response = fetch_flick_access_token(company)

    access_token = response.get("access_token")

    if not access_token:
        frappe.throw(_("Failed to refresh access token"))

    return access_token

@frappe.whitelist()
def get_document_status(invoice_name: str):
    """Kept for existing callers: the status now comes from the Company's configured provider."""
    from uae_erpgulf.uae_erpgulf.queue import refresh_einvoice_status

    return refresh_einvoice_status("Sales Invoice", invoice_name)
