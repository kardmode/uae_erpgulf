frappe.query_reports["UAE E-Invoice VAT Reconciliation"] = {
    filters: [
        {
            fieldname: "company",
            label: __("Company"),
            fieldtype: "Link",
            options: "Company",
            default: frappe.defaults.get_user_default("Company"),
            reqd: 1
        },
        {
            fieldname: "from_date",
            label: __("From Date"),
            fieldtype: "Date",
            default: frappe.datetime.add_months(frappe.datetime.get_today(), -3),
            reqd: 1
        },
        {
            fieldname: "to_date",
            label: __("To Date"),
            fieldtype: "Date",
            default: frappe.datetime.get_today(),
            reqd: 1
        },
        {
            fieldname: "emirate",
            label: __("Emirate (Box 1)"),
            fieldtype: "Select",
            options: "\nAbu Dhabi\nDubai\nSharjah\nAjman\nUmm Al Quwain\nRas Al Khaimah\nFujairah"
        },
        {
            fieldname: "status",
            label: __("E-Invoice Status"),
            fieldtype: "Select",
            options: "\nCLEARED\nREPORTED\nPending Submission\nFailed\nPre-Mandate / Historical"
        }
    ]
};
