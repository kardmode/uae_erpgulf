"""
Unit tests for UAE E-Invoicing Phase 8:
- Migration Patch execution
- Go-Live Date Cutoff & QuickBooks historical invoice protection
- UAE E-Invoice to VAT 201 Reconciliation Report
- MRP VAT Return compliance audit integration
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

if "frappe.model.document" not in sys.modules:
    if not REAL_FRAPPE:
        sys.modules["frappe.model.document"] = MagicMock()

if "frappe.custom.doctype.custom_field.custom_field" not in sys.modules:
    if not REAL_FRAPPE:
        sys.modules["frappe.custom.doctype.custom_field.custom_field"] = MagicMock()

for mod in ["pytz", "requests"]:
    if mod not in sys.modules:
        try:
            __import__(mod)
        except ImportError:
            if not REAL_FRAPPE:
                sys.modules[mod] = MagicMock()

from uae_erpgulf.uae_erpgulf.queue import enqueue_invoice_submission
from uae_erpgulf.uae_erpgulf.report.uae_einvoice_vat_reconciliation.uae_einvoice_vat_reconciliation import (
    execute as run_vat_reconciliation_report,
    get_columns,
    get_report_summary
)


@unittest.skipIf(REAL_FRAPPE, "standalone mock tests: run with python -m unittest, not bench")
class TestMigrationAndVATIntegration(unittest.TestCase):
    def setUp(self):
        frappe_mock.reset_mock()

    def test_go_live_cutoff_skips_historical_invoice(self):
        """Validates that historical/QuickBooks invoices posted before go-live date bypass e-invoicing queue."""
        mock_comp = MagicMock()
        mock_comp.custom_uae_einvoice_enabled = 1
        mock_comp.custom_einvoice_go_live_date = "2027-01-01"
        frappe_mock.get_cached_doc.return_value = mock_comp

        mock_inv = MagicMock()
        mock_inv.company = "Legacy Trading LLC"
        mock_inv.posting_date = "2026-05-15"  # Prior to go-live date
        mock_inv.custom_uae_einvoice_status = None
        mock_inv.flags = MagicMock()
        mock_inv.flags.ignore_einvoice = False

        enqueue_invoice_submission(mock_inv)

        # Ensure frappe.enqueue was NOT called
        frappe_mock.enqueue.assert_not_called()

    def test_go_live_cutoff_allows_in_mandate_invoice(self):
        """Validates that current invoices posted on or after go-live date ARE queued."""
        frappe_mock.reset_mock()
        mock_comp = MagicMock()
        mock_comp.custom_uae_einvoice_enabled = 1
        mock_comp.custom_einvoice_go_live_date = "2026-01-01"
        frappe_mock.get_cached_doc.return_value = mock_comp

        mock_inv = MagicMock()
        mock_inv.company = "Legacy Trading LLC"
        mock_inv.posting_date = "2026-10-25"  # On or after go-live date
        mock_inv.custom_uae_einvoice_status = None
        mock_inv.flags = MagicMock()
        mock_inv.flags.ignore_einvoice = False

        enqueue_invoice_submission(mock_inv)

        # Ensure frappe.enqueue WAS called
        frappe_mock.enqueue.assert_called_once()

    @patch("uae_erpgulf.patches.v2_0.migrate_einvoice_fields.create_custom_fields")
    def test_migration_patch_execution(self, mock_create_custom_fields):
        """Validates that v2.0 migration patch executes custom fields sync and data backfills."""
        from uae_erpgulf.patches.v2_0 import migrate_einvoice_fields

        company_row = MagicMock()
        company_row.name = "Company A"
        company_row.custom_einvoice_go_live_date = "2026-01-01"
        frappe_mock.get_all.side_effect = [["Company A"], [company_row]]
        frappe_mock.utils.today.return_value = "2026-10-25"
        frappe_mock.db.has_column.return_value = True
        frappe_mock.db.sql.reset_mock()

        try:
            migrate_einvoice_fields.execute()
        finally:
            frappe_mock.get_all.side_effect = None

        mock_create_custom_fields.assert_called_once()
        frappe_mock.db.commit.assert_called()
        # Pre-go-live invoices of both doctypes are marked historical, cut off at the go-live date
        updates = [c for c in frappe_mock.db.sql.call_args_list if "Pre-Mandate / Historical" in c[0][0]]
        self.assertEqual(len(updates), 2)
        self.assertEqual(updates[0][0][1], ("Company A", "2026-01-01"))

    def test_vat_reconciliation_report(self):
        """Validates that UAE E-Invoice VAT Reconciliation Report computes metrics properly."""
        frappe_mock.db.has_column.return_value = True

        # Mock SQL returned rows
        mock_rows = [
            {
                "name": "SINV-2026-001",
                "posting_date": "2026-10-20",
                "customer": "Customer A",
                "customer_name": "Customer A",
                "tax_id": "100234567890003",
                "emirate": "Dubai",
                "taxable_amount": 1000.0,
                "vat_amount": 50.0,
                "grand_total": 1050.0,
                "einvoice_status": "CLEARED",
                "document_id": "UUID-12345"
            },
            {
                "name": "SINV-2026-002",
                "posting_date": "2026-10-22",
                "customer": "Customer B",
                "customer_name": "Customer B",
                "tax_id": "",
                "emirate": "Abu Dhabi",
                "taxable_amount": 2000.0,
                "vat_amount": 100.0,
                "grand_total": 2100.0,
                "einvoice_status": "Pre-Mandate / Historical",
                "document_id": ""
            }
        ]
        frappe_mock.db.sql.return_value = mock_rows

        cols, data, msg, chart, summary = run_vat_reconciliation_report({"company": "Test Company"})

        self.assertEqual(len(cols), 11)
        self.assertEqual(len(data), 2)
        self.assertIn("✔ FTA Cleared", data[0]["reconciliation_badge"])
        self.assertIn("ℹ Pre-Mandate", data[1]["reconciliation_badge"])

        # Summary check
        self.assertEqual(summary[0]["value"], "AED 3,000.00")  # Total Taxable
        self.assertEqual(summary[1]["value"], "AED 150.00")    # Total VAT
        self.assertEqual(summary[2]["value"], 1)               # Cleared count
        self.assertEqual(summary[3]["value"], 1)               # Pending/Pre-mandate count


if __name__ == "__main__":
    unittest.main()
