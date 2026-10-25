# UAE ERPGulf Unit Tests
#
# Two kinds of test modules live here:
# - test_einvoice_integration: runs against a real site (bench run-tests).
# - all others: standalone unit tests that replace `frappe` with a MagicMock
#   (python -m unittest). Under bench, real frappe is already imported, so they skip
#   themselves instead of overwriting sys.modules["frappe"] for every other test.
import sys

_frappe = sys.modules.get("frappe")
REAL_FRAPPE = _frappe is not None and hasattr(_frappe, "local") and type(_frappe).__name__ == "module"
