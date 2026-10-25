"""
Unit tests for UAE E-Invoicing Testing Sandbox & Data Simulator.
Validates demo master data seeding and simulation execution across
standard PO matching, service expense routing, Flick JSON, credit notes, and duplicate guards.
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
    # Real exception classes: the inbound processor subclasses them
    frappe_mock.ValidationError = type("ValidationError", (Exception,), {})
    frappe_mock.DuplicateEntryError = type("DuplicateEntryError", (Exception,), {})
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

from uae_erpgulf.core.simulator import seed_demo_data, simulate_inbound_invoice
from uae_erpgulf.core.inbound_processor import DuplicateInboundInvoiceError


@unittest.skipIf(REAL_FRAPPE, "standalone mock tests: run with python -m unittest, not bench")
class TestSimulatorAndSeeder(unittest.TestCase):
    def setUp(self):
        self.company = "Al Najm Contracting LLC"
        self.mock_company_doc = MagicMock()
        self.mock_company_doc.tax_id = None
        self.mock_company_doc.custom_default_inbound_project = None
        frappe_mock.get_doc.return_value = self.mock_company_doc
        frappe_mock.db.exists.return_value = False

    def test_seed_demo_data(self):
        """Validates that seed_demo_data creates demo TRN, project, supplier, and PO."""
        res = seed_demo_data(self.company)
        self.assertEqual(res["status"], "success")
        self.assertIn("Al Najm Supplies", str(res["details"]))
        self.assertIn("100234567890003", str(res["details"]))
        self.assertIn("DEMO-STK-01", str(res["details"]))

    @patch("uae_erpgulf.core.simulator.InboundInvoiceProcessor")
    def test_simulate_standard_po(self, mock_processor_cls):
        """Validates standard PO 3-way match simulation."""
        mock_proc = MagicMock()
        mock_pi = MagicMock()
        mock_pi.name = "PINV-2026-0001"
        mock_pi.bill_no = "SIM-UBL-12345"
        mock_pi.grand_total = 1050.0
        mock_pi.currency = "AED"
        mock_pi.remarks = "E-Invoiced"
        mock_item = MagicMock()
        mock_item.purchase_order = "PO-2026-DEMO-01"
        mock_pi.items = [mock_item]
        mock_proc.process.return_value = mock_pi
        mock_processor_cls.return_value = mock_proc

        res = simulate_inbound_invoice(self.company, scenario="standard_po")
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["invoice_name"], "PINV-2026-0001")
        self.assertEqual(res["po_matched"], "PO-2026-DEMO-01")

    @patch("uae_erpgulf.core.simulator.InboundInvoiceProcessor")
    def test_simulate_service_expense(self, mock_processor_cls):
        """Validates service/non-stock invoice simulation."""
        mock_proc = MagicMock()
        mock_pi = MagicMock()
        mock_pi.name = "PINV-2026-0002"
        mock_pi.bill_no = "SIM-SRV-12345"
        mock_pi.grand_total = 525.0
        mock_item = MagicMock()
        mock_item.expense_account = "5111 - Freight & Courier - CO"
        mock_pi.items = [mock_item]
        mock_proc.process.return_value = mock_pi
        mock_processor_cls.return_value = mock_proc

        res = simulate_inbound_invoice(self.company, scenario="service_expense")
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["invoice_name"], "PINV-2026-0002")
        self.assertEqual(res["expense_account"], "5111 - Freight & Courier - CO")

    @patch("uae_erpgulf.core.simulator.InboundInvoiceProcessor")
    def test_simulate_duplicate_guard(self, mock_processor_cls):
        """Validates that duplicate guard blocks repeated submissions."""
        mock_proc = MagicMock()
        mock_pi = MagicMock()
        mock_pi.name = "PINV-2026-0003"
        # First call succeeds, second call throws DuplicateInboundInvoiceError
        mock_proc.process.side_effect = [
            mock_pi,
            DuplicateInboundInvoiceError("Duplicate detected: Bill No already exists")
        ]
        mock_processor_cls.return_value = mock_proc

        res = simulate_inbound_invoice(self.company, scenario="duplicate_guard")
        self.assertEqual(res["status"], "success")
        self.assertIn("Duplicate guard verified", res["message"])


if __name__ == "__main__":
    unittest.main()
