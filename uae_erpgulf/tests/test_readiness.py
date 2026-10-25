"""
Unit tests for UAE E-Invoicing Company Readiness Diagnostic Tool.
"""

import sys
import unittest
from unittest.mock import MagicMock, patch

from uae_erpgulf.tests import REAL_FRAPPE

# Ensure frappe mock if running standalone
if "frappe" in sys.modules and isinstance(sys.modules["frappe"], MagicMock):
    frappe_mock = sys.modules["frappe"]
else:
    frappe_mock = MagicMock()
    frappe_mock.__path__ = []
    frappe_mock._ = lambda x, *args: x

    def _whitelist(*args, **kwargs):
        def decorator(fn):
            return fn
        if args and callable(args[0]):
            return args[0]
        return decorator

    frappe_mock.whitelist = _whitelist
    frappe_mock.db = MagicMock()
    if not REAL_FRAPPE:
        sys.modules["frappe"] = frappe_mock

if "frappe.utils" not in sys.modules:
    if not REAL_FRAPPE:
        sys.modules["frappe.utils"] = MagicMock()

for mod in ["pytz", "requests"]:
    if mod not in sys.modules:
        try:
            __import__(mod)
        except ImportError:
            if not REAL_FRAPPE:
                sys.modules[mod] = MagicMock()

from uae_erpgulf.core.readiness import check_company_readiness


@unittest.skipIf(REAL_FRAPPE, "standalone mock tests: run with python -m unittest, not bench")
class TestCompanyReadinessTool(unittest.TestCase):
    @patch("uae_erpgulf.adapters.factory.ASPFactory.get_adapter")
    def test_ready_company(self, mock_get_adapter):
        """Validates that a fully configured company returns READY status."""
        mock_get_adapter.return_value.test_connection.return_value = (True, "Authenticated")
        mock_comp = MagicMock()
        mock_comp.tax_id = "100234567890003"
        mock_comp.custom_trade_license_number = "TL-99120"
        mock_comp.custom_emirates = "Dubai"
        mock_comp.custom_accredited_service_providers = "Generic UBL"
        mock_comp.custom_participant_id = "0235:100234567890003"
        mock_comp.custom_base_url = "https://sb-ae-api.flick.network"
        mock_comp.custom_xflickauthkey = "test_key"
        mock_comp.default_payable_account = "2110 - Creditors - CO"
        mock_comp.default_expense_account = "5111 - Operating Expense - CO"
        mock_comp.cost_center = "Main - CO"
        mock_comp.custom_default_inbound_project = "PROJ-OPS"

        frappe_mock.get_doc.return_value = mock_comp
        frappe_mock.get_cached_doc.return_value = mock_comp
        frappe_mock.db.get_value.return_value = "VAT 5% - CO"
        frappe_mock.get_all.return_value = ["PROJ-OPS"]

        res = check_company_readiness("Company A")
        if res["overall_status"] != "READY":
            print("\nDEBUG NON-PASS CHECKS:", [c for c in res["checks"] if c["status"] != "PASS"])
        self.assertEqual(res["overall_status"], "READY")
        self.assertEqual(res["score_summary"]["fail"], 0)
        self.assertGreater(res["score_summary"]["pass"], 5)

    @patch.object(frappe_mock, "get_doc")
    @patch.object(frappe_mock.db, "get_value")
    @patch.object(frappe_mock, "get_all")
    def test_unready_company_flags_failures(self, mock_get_all, mock_get_value, mock_get_doc):
        """Validates that missing TRN and missing MRP projects flag FAIL."""
        mock_comp = MagicMock()
        mock_comp.tax_id = "INVALID_TRN"
        mock_comp.custom_trade_license_number = None
        mock_comp.custom_emirates = None
        mock_comp.custom_accredited_service_providers = "Generic UBL"
        mock_comp.custom_participant_id = None
        mock_comp.default_payable_account = None
        mock_comp.default_expense_account = None
        mock_comp.cost_center = None
        mock_comp.custom_default_inbound_project = None
        mock_get_doc.return_value = mock_comp

        mock_get_value.return_value = None
        mock_get_all.return_value = []

        res = check_company_readiness("Company B")
        self.assertEqual(res["overall_status"], "ACTION_REQUIRED")
        self.assertGreater(res["score_summary"]["fail"], 0)

        # Ensure TRN and MRP project failures are recorded
        failed_items = [c["item"] for c in res["checks"] if c["status"] == "FAIL"]
        self.assertIn("VAT Registration Number (TRN)", failed_items)
        self.assertIn("Mandatory Project Policy", failed_items)


if __name__ == "__main__":
    unittest.main()
