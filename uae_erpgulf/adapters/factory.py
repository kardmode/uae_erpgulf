"""
ASP Factory: Dynamically resolves and instantiates the configured
Accredited Service Provider (ASP) adapter for a Company.
"""

import frappe
from typing import Optional, Type
from frappe import _
from uae_erpgulf.adapters.base import BaseASPAdapter
from uae_erpgulf.adapters.flick import FlickASPAdapter
from uae_erpgulf.adapters.generic_ubl import GenericUBLAdapter
from uae_erpgulf.adapters.tronstride import TronstrideASPAdapter
from uae_erpgulf.adapters.wafeq import WafeqASPAdapter
from uae_erpgulf.adapters.complyance import ComplyanceASPAdapter


class ASPFactory:
    """Registry and factory for ASP adapters."""

    _REGISTRY = {
        "flick network l.l.c": FlickASPAdapter,
        "flick": FlickASPAdapter,
        "generic ubl": GenericUBLAdapter,
        "peppol": GenericUBLAdapter,
        "tronstride": TronstrideASPAdapter,
        "tronstride fzc": TronstrideASPAdapter,
        "tronstride fzc (aigentrix)": TronstrideASPAdapter,
        "aigentrix": TronstrideASPAdapter,
        "wafeq": WafeqASPAdapter,
        "wafeq e-invoicing": WafeqASPAdapter,
        "wafeq asp": WafeqASPAdapter,
        "wafeq fz-llc": WafeqASPAdapter,
        "complyance electronics l.l.c": ComplyanceASPAdapter,
        "complyance": ComplyanceASPAdapter,
        "complyance.io": ComplyanceASPAdapter,
    }

    @classmethod
    def register_adapter(cls, provider_key: str, adapter_cls: Type[BaseASPAdapter]):
        """Allows 3rd party plugins to register custom ASP adapters dynamically."""
        cls._REGISTRY[provider_key.lower().strip()] = adapter_cls

    @classmethod
    def adapter_class(cls, provider: str) -> Optional[Type[BaseASPAdapter]]:
        """The adapter for a provider name, or None when this app has no integration for it."""
        return cls._REGISTRY.get((provider or "").lower().strip())

    @classmethod
    def get_adapter(cls, company: str) -> BaseASPAdapter:
        """
        Retrieves the appropriate adapter instance for the specified Company.
        Refuses unknown providers instead of silently sending invoices to a different one.
        """
        company_doc = frappe.get_cached_doc("Company", company)
        provider_setting = getattr(company_doc, "custom_accredited_service_providers", "") or ""
        if not provider_setting:
            frappe.throw(_("Select an Accredited Service Provider on Company {0}.").format(company))

        adapter_cls = cls.adapter_class(provider_setting)
        if not adapter_cls:
            frappe.throw(
                _("No e-invoicing integration is available for {0}. Supported providers: Flick, Wafeq, "
                  "Complyance and TronStride.").format(provider_setting),
                title=_("Unsupported service provider"),
            )

        return adapter_cls(company=company)


@frappe.whitelist()
def test_asp_connection(company: str):
    """Whitelisted RPC endpoint to test ASP credentials for a company."""
    from uae_erpgulf.core.credentials import check_company_permission

    check_company_permission(company)
    adapter = ASPFactory.get_adapter(company)
    return adapter.test_connection()

