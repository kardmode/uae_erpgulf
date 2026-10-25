"""
UAE Federal Tax Authority (FTA) & PEPPOL E-Invoicing Constants
Aligned with UAE E-Invoicing Data Dictionary (PINT-AE) & UBL 2.1
"""

# Supported Document Types (UN/ECE 1001 Subset)
DOC_TYPE_COMMERCIAL_INVOICE = "380"
DOC_TYPE_CREDIT_NOTE = "381"
DOC_TYPE_DEBIT_NOTE = "383"
DOC_TYPE_SELF_BILLED_INVOICE = "389"
DOC_TYPE_SELF_BILLED_CREDIT_NOTE = "261"
DOC_TYPE_OUT_OF_SCOPE_INVOICE = "480"
DOC_TYPE_OUT_OF_SCOPE_CREDIT_NOTE = "81"

# Document types rendered as UBL CreditNote
CREDIT_NOTE_TYPES = (
    DOC_TYPE_CREDIT_NOTE,
    DOC_TYPE_SELF_BILLED_CREDIT_NOTE,
    DOC_TYPE_OUT_OF_SCOPE_CREDIT_NOTE,
)

# VAT Categories (UN/ECE 5305 Subset)
VAT_CATEGORY_STANDARD = "S"          # Standard rated (5%)
VAT_CATEGORY_ZERO = "Z"              # Zero rated (0%)
VAT_CATEGORY_EXEMPT = "E"            # Exempt from tax
VAT_CATEGORY_REVERSE_CHARGE = "AE"    # VAT Reverse Charge (RCM)
VAT_CATEGORY_OUT_OF_SCOPE = "O"      # Not subject to VAT / Out of scope
VAT_CATEGORY_MARGIN = "N"            # Margin scheme

VAT_CATEGORY_MAP = {
    "s - standard rated": "S",
    "standard rated": "S",
    "standard": "S",
    "s": "S",
    "z - zero rated": "Z",
    "zero rated": "Z",
    "zero": "Z",
    "z": "Z",
    "e - exempt from tax": "E",
    "exempt from tax": "E",
    "exempt": "E",
    "e": "E",
    "ae - vat reverse charge": "AE",
    "vat reverse charge": "AE",
    "reverse charge": "AE",
    "ae": "AE",
    "o - not subject to vat": "O",
    "not subject to vat": "O",
    "out of scope": "O",
    "o": "O",
    "n - margin scheme": "N",
    "margin scheme": "N",
    "n": "N",
}

# Approved Payment Means (UN/ECE 4461 Subset)
PAYMENT_MEANS_MAP = {
    "1": "Instrument not defined",
    "10": "Cash",
    "20": "Cheque",
    "30": "Credit transfer",
    "31": "Debit transfer",
    "42": "Payment to bank account",
    "48": "Bank card",
    "49": "Direct debit",
    "55": "Debit card",
    "58": "SEPA credit transfer",
}

# UAE Emirate ISO Subdivision Codes
EMIRATE_CODE_MAP = {
    "abu dhabi": "AUH",
    "dubai": "DXB",
    "sharjah": "SHJ",
    "ajman": "AJM",
    "umm al quwain": "UAQ",
    "ras al khaimah": "RAK",
    "fujairah": "FUJ",
    "auh": "AUH",
    "dxb": "DXB",
    "shj": "SHJ",
    "ajm": "AJM",
    "uaq": "UAQ",
    "rak": "RAK",
    "fuj": "FUJ",
}
