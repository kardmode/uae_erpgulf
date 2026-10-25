frappe.ui.form.on("Company", {

    custom_verify_token_: function (frm) {
        frappe.call({
            method: "uae_erpgulf.uae_erpgulf.verify_token.verify_flick_token",
            args: { company: frm.doc.name },
            callback: function (r) {
                if (r.message) {
                    const res = r.message;
                    let responseData = {};
                    try {
                        responseData = typeof res.response === "string"
                            ? JSON.parse(res.response)
                            : res.response || {};
                    } catch (e) {
                        responseData = {};
                    }

                    const data = responseData.data || {};

                    const rows = [
                        ["Status", responseData.status || "-"],
                        ["Message", responseData.message || "-"],
                        ["Tenant ID", data.tenant_id || "-"],
                        ["Tenant Name", data.tenant_name || "-"],
                        ["Authenticated", data.authenticated !== undefined ? (data.authenticated ? "✔ Yes" : "✘ No") : "-"],
                    ];

                    const tableRows = rows.map(([field, value]) => `
                        <tr>
                            <td style="padding:8px 12px;border:1px solid #d1d8dd !important;font-weight:600;width:40%;">${field}</td>
                            <td style="padding:8px 12px;border:1px solid #d1d8dd !important;">${value}</td>
                        </tr>
                    `).join("");

                    const html = `
                        <style>
                            .flick-table { border-collapse: collapse; width: 100%; font-size: 13px; }
                            .flick-table th { background-color: #f0f4f7; padding: 8px 12px; border: 1px solid #d1d8dd !important; text-align: left; }
                            .flick-table td { border: 1px solid #d1d8dd !important; }
                            .flick-table tr:nth-child(even) { background-color: #f9f9f9; }
                        </style>
                        <table class="flick-table">
                            <thead>
                                <tr>
                                    <th>Field</th>
                                    <th>Value</th>
                                </tr>
                            </thead>
                            <tbody>${tableRows}</tbody>
                        </table>
                    `;

                    frappe.msgprint({
                        title: __("Token Verification"),
                        message: html,
                        indicator: data.authenticated ? "green" : "red",
                        wide: true
                    });

                    frm.reload_doc();
                }
            }
        });
    },

    custom_get_participant_details: function (frm) {
        if (!frm.doc.custom_participant_id) {
            frappe.msgprint(_("Please enter Participant ID"));
            return;
        }

        frappe.call({
            method: "uae_erpgulf.uae_erpgulf.verify_token.get_participant_details",
            args: { company: frm.doc.name },
            callback: function (r) {
                if (r.message) {
                    const res = r.message;
                    const apiResponse = res.response || {};   // Python returns {status, response: <full API json>}
                    const data = apiResponse.data || {};       // actual participant fields are here

                    const rows = [
                        ["Status", apiResponse.status || "-"],
                        ["Message", apiResponse.message || "-"],
                        ["Legal Name", data.legal_name || "-"],
                        ["Trade Name", data.trade_name || "-"],
                        ["Peppol ID", data.peppol_id || "-"],
                        ["Emirates Code", data.emirates_code || "-"],
                        ["Country Code", data.country_code || "-"],
                        ["Contact Email", data.contact_email || "-"],
                        ["Participant Status", data.status || "-"],
                    ];

                    const tableRows = rows.map(([field, value]) => `
                        <tr>
                            <td style="padding:8px 12px;border:1px solid #d1d8dd !important;font-weight:600;width:40%;">${field}</td>
                            <td style="padding:8px 12px;border:1px solid #d1d8dd !important;">${value}</td>
                        </tr>
                    `).join("");

                    const html = `
                        <style>
                            .flick-table { border-collapse: collapse; width: 100%; font-size: 13px; }
                            .flick-table th { background-color: #f0f4f7; padding: 8px 12px; border: 1px solid #d1d8dd !important; text-align: left; }
                            .flick-table td { border: 1px solid #d1d8dd !important; }
                            .flick-table tr:nth-child(even) { background-color: #f9f9f9; }
                        </style>
                        <table class="flick-table">
                            <thead>
                                <tr>
                                    <th>Field</th>
                                    <th>Value</th>
                                </tr>
                            </thead>
                            <tbody>${tableRows}</tbody>
                        </table>
                    `;

                    frappe.msgprint({
                        title: __("Participant Details"),
                        message: html,
                        indicator: data.status === "active" ? "green" : "orange",
                        wide: true
                    });

                    frm.reload_doc();
                } else {
                    frappe.msgprint(_("❌ Failed to fetch participant details"));
                }
            }
        });
    },

    // ✅ FIXED: Now inside same object
    custom_get_access_token: function (frm) {

        if (
            !frm.doc.custom_client_id?.trim() ||
            !frm.doc.custom_client_secret?.trim()
        ) {
            frappe.msgprint(__("Please set Client ID and Client Secret"));
            return;
        }

        frappe.call({
            method: "uae_erpgulf.uae_erpgulf.verify_token.get_flick_access_token",
            args: {
                company: frm.doc.name
            },
            freeze: true,
            freeze_message: "Fetching Access Token...",

            callback: function (r) {
                if (r.message && r.message.status === "success") {
                    frappe.msgprint({
                        title: __("Success"),
                        message: __("✔ Access Token Generated & Saved Successfully"),
                        indicator: "green"
                    });
                } else {
                    frappe.msgprint(_("❌ Failed to fetch access token"));
                }

                frm.reload_doc();
            },

            error: function (err) {
                console.error(err);
                frappe.msgprint(_("❌ Error while fetching access token"));
            }
        });
    }

});
frappe.ui.form.on('Company', {
    custom_subscribe_webhook: function (frm) {
        frappe.call({
            method: "uae_erpgulf.uae_erpgulf.webhook.register_flick_webhook",
            args: { company: frm.doc.name },
            freeze: true,
            freeze_message: "Subscribing Webhook...",
            callback: function (r) {
                if (r.message) {
                    const res = r.message;
                    const data = res.data || {};

                    const rows = [
                        ["Status", res.status || "-"],
                        ["Message", res.message || "-"],
                        ["UUID", data.uuid || "-"],
                        ["Endpoint", data.endpoint || "-"],
                        ["Active", data.active !== undefined ? (data.active ? "✔ Yes" : "✘ No") : "-"],
                    ];

                    const tableRows = rows.map(([field, value]) => `
                        <tr>
                            <td style="padding:8px 12px;border:1px solid #d1d8dd !important;font-weight:600;width:35%;">${field}</td>
                            <td style="padding:8px 12px;border:1px solid #d1d8dd !important;word-break:break-all;">${value}</td>
                        </tr>
                    `).join("");

                    const html = `
                        <style>
                            .flick-table { border-collapse: collapse; width: 100%; font-size: 13px; }
                            .flick-table th { background-color: #f0f4f7; padding: 8px 12px; border: 1px solid #d1d8dd !important; text-align: left; }
                            .flick-table td { border: 1px solid #d1d8dd !important; }
                            .flick-table tr:nth-child(even) { background-color: #f9f9f9; }
                        </style>
                        <table class="flick-table">
                            <thead>
                                <tr><th>Field</th><th>Value</th></tr>
                            </thead>
                            <tbody>${tableRows}</tbody>
                        </table>
                    `;

                    frappe.msgprint({
                        title: __("Webhook Subscribed"),
                        message: html,
                        indicator: data.active ? "green" : "orange",
                        wide: true
                    });

                    frm.reload_doc();
                } else {
                    frappe.msgprint(_("❌ Failed to subscribe webhook"));
                }
            },
            error: function (err) {
                console.error(err);
                frappe.msgprint(_("❌ Error while subscribing webhook"));
            }
        });
    },

    // ✅ Get Subscription
    custom_get_subscription: function (frm) {
        frappe.call({
            method: "uae_erpgulf.uae_erpgulf.webhook.custom_get_subscription",
            args: { company: frm.doc.name },
            freeze: true,
            freeze_message: "Fetching Webhook Details...",
            callback: function (r) {
                if (r.message) {
                    const res = r.message;
                    const data = res.data || {};

                    const rows = [
                        ["Status", res.status || "-"],
                        ["Message", res.message || "-"],
                        ["UUID", data.uuid || "-"],
                        ["Name", data.name || "-"],
                        ["Endpoint", data.endpoint || "-"],
                        ["Active", data.active !== undefined ? (data.active ? "✔ Yes" : "✘ No") : "-"],
                        ["Created At", data.created_at || "-"],
                        ["Updated At", data.updated_at || "-"],
                    ].map(([field, value]) => `
                        <tr>
                            <td style="padding:8px 12px;border:1px solid #d1d8dd !important;font-weight:600;width:35%;">${field}</td>
                            <td style="padding:8px 12px;border:1px solid #d1d8dd !important;word-break:break-all;">${value}</td>
                        </tr>
                    `).join("");

                    const html = `
                        <style>
                            .flick-table { border-collapse: collapse; width: 100%; font-size: 13px; }
                            .flick-table th { background-color: #f0f4f7; padding: 8px 12px; border: 1px solid #d1d8dd !important; text-align: left; }
                            .flick-table td { border: 1px solid #d1d8dd !important; }
                            .flick-table tr:nth-child(even) { background-color: #f9f9f9; }
                        </style>
                        <table class="flick-table">
                            <thead><tr><th>Field</th><th>Value</th></tr></thead>
                            <tbody>${rows}</tbody>
                        </table>
                    `;

                    frappe.msgprint({
                        title: __("Webhook Subscription Details"),
                        message: html,
                        indicator: data.active ? "green" : "orange",
                        wide: true
                    });

                    frm.reload_doc();
                } else {
                    frappe.msgprint(_("❌ Failed to fetch webhook details"));
                }
            },
            error: function (err) {
                console.error(err);
                frappe.msgprint(_("❌ Error while fetching webhook details"));
            }
        });
    },

    // ✅ NEW: Webhook Logs (Deliveries)
    custom_webhook_logs: function (frm) {
        if (!frm.doc.custom_uuid_of_webhook) {
            frappe.msgprint(_("⚠ Please create webhook first"));
            return;
        }

        frappe.call({
            method: "uae_erpgulf.uae_erpgulf.webhook.get_webhook_deliveries",
            args: { company: frm.doc.name },
            freeze: true,
            freeze_message: "Fetching Webhook Logs...",
            callback: function (r) {
                if (r.message) {
                    const res = r.message;
                    const pagination = res.pagination || {};
                    const deliveries = res.data || [];

                    const summaryRows = [
                        ["Status", res.status || "-"],
                        ["Message", res.message || "-"],
                        ["Total Logs", pagination.total !== undefined ? pagination.total : "-"],
                        ["Pages", pagination.pages !== undefined ? pagination.pages : "-"],
                    ].map(([field, value]) => `
                        <tr>
                            <td style="padding:8px 12px;border:1px solid #d1d8dd !important;font-weight:600;width:40%;">${field}</td>
                            <td style="padding:8px 12px;border:1px solid #d1d8dd !important;">${value}</td>
                        </tr>
                    `).join("");

                    // delivery rows if data is not empty
                    let deliverySection = "";
                    if (deliveries.length > 0) {
                        const deliveryRows = deliveries.map(d => `
                            <tr>
                                <td style="padding:8px 12px;border:1px solid #d1d8dd !important;">${d.id || "-"}</td>
                                <td style="padding:8px 12px;border:1px solid #d1d8dd !important;">${d.status || "-"}</td>
                                <td style="padding:8px 12px;border:1px solid #d1d8dd !important;">${d.event_type || "-"}</td>
                                <td style="padding:8px 12px;border:1px solid #d1d8dd !important;">${d.created_at || "-"}</td>
                            </tr>
                        `).join("");

                        deliverySection = `
                            <br>
                            <b>Delivery Records</b>
                            <table class="flick-table" style="margin-top:8px;">
                                <thead>
                                    <tr>
                                        <th>ID</th>
                                        <th>Status</th>
                                        <th>Event Type</th>
                                        <th>Created At</th>
                                    </tr>
                                </thead>
                                <tbody>${deliveryRows}</tbody>
                            </table>
                        `;
                    } else {
                        deliverySection = `<br><i style="color:#888;">No delivery records found.</i>`;
                    }

                    const html = `
                        <style>
                            .flick-table { border-collapse: collapse; width: 100%; font-size: 13px; }
                            .flick-table th { background-color: #f0f4f7; padding: 8px 12px; border: 1px solid #d1d8dd !important; text-align: left; }
                            .flick-table td { border: 1px solid #d1d8dd !important; }
                            .flick-table tr:nth-child(even) { background-color: #f9f9f9; }
                        </style>
                        <table class="flick-table">
                            <thead><tr><th>Field</th><th>Value</th></tr></thead>
                            <tbody>${summaryRows}</tbody>
                        </table>
                        ${deliverySection}
                    `;

                    frappe.msgprint({
                        title: __("Webhook Delivery Logs"),
                        message: html,
                        indicator: pagination.total > 0 ? "green" : "orange",
                        wide: true
                    });

                } else {
                    frappe.msgprint(_("❌ Failed to fetch webhook logs"));
                }
            },
            error: function (err) {
                console.error(err);
                frappe.msgprint(_("❌ Error while fetching webhook logs"));
            }
        });
    },

    refresh: function (frm) {
        if (!frm.doc.__islocal) {
            frm.add_custom_button(
                __("Run E-Invoice Readiness Audit"),
                function () {
                    frappe.call({
                        method: "uae_erpgulf.core.readiness.check_company_readiness",
                        args: { company: frm.doc.name },
                        freeze: true,
                        freeze_message: __("Auditing E-Invoicing & Accounting Readiness..."),
                        callback: function (r) {
                            if (r.message) {
                                show_readiness_modal(r.message);
                            }
                        }
                    });
                },
                __("UAE E-Invoice")
            );

            frm.add_custom_button(
                __("Seed Demo Master Data"),
                function () {
                    frappe.confirm(
                        __("This will create a sample UAE Supplier (Al Najm Supplies), Items, and an approved Purchase Order (PO-2026-DEMO-01) for testing. Proceed?"),
                        function () {
                            frappe.call({
                                method: "uae_erpgulf.core.simulator.seed_demo_data",
                                args: { company: frm.doc.name },
                                freeze: true,
                                freeze_message: __("Seeding Demo UAE E-Invoicing Data..."),
                                callback: function (r) {
                                    if (r.message && r.message.status === "success") {
                                        const details = r.message.details || {};
                                        const created = (details.created || []).map(x => `<li>✔ ${x}</li>`).join("");
                                        const existing = (details.existing || []).map(x => `<li>ℹ ${x} (already present)</li>`).join("");
                                        frappe.msgprint({
                                            title: __("Demo Data Seeded Successfully"),
                                            message: `<div style="font-size:13px;"><ul style="padding-left:16px;">${created}${existing}</ul></div>`,
                                            indicator: "green"
                                        });
                                        frm.reload_doc();
                                    }
                                }
                            });
                        }
                    );
                },
                __("UAE E-Invoice")
            );

            frm.add_custom_button(
                __("Simulate Inbound Invoice"),
                function () {
                    const d = new frappe.ui.Dialog({
                        title: __("Simulate Inbound UAE E-Invoice"),
                        fields: [
                            {
                                label: __("Simulation Scenario"),
                                fieldname: "scenario",
                                fieldtype: "Select",
                                reqd: 1,
                                default: "standard_po",
                                options: [
                                    { value: "standard_po", label: "1. Standard PO 3-Way Match (PEPPOL UBL XML)" },
                                    { value: "service_expense", label: "2. Service / Courier Fee (No PO, Expense Route)" },
                                    { value: "flick_json", label: "3. Flick JSON Format (Partial PO Billing)" },
                                    { value: "credit_note", label: "4. Inbound Credit Note (Return / Adjustment)" },
                                    { value: "duplicate_guard", label: "5. Duplicate Guard Verification (Blocks Re-submission)" }
                                ]
                            }
                        ],
                        primary_action_label: __("Execute Inbound Simulation"),
                        primary_action: function (values) {
                            d.hide();
                            frappe.call({
                                method: "uae_erpgulf.core.simulator.simulate_inbound_invoice",
                                args: { company: frm.doc.name, scenario: values.scenario },
                                freeze: true,
                                freeze_message: __("Simulating Inbound E-Invoice & Generating Draft..."),
                                callback: function (r) {
                                    if (r.message) {
                                        const res = r.message;
                                        if (res.status === "success") {
                                            const invoiceLink = res.invoice_name
                                                ? `<br><br><button class="btn btn-primary btn-sm" onclick="frappe.set_route('Form', 'Purchase Invoice', '${res.invoice_name}')">📄 View Draft Purchase Invoice: <b>${res.invoice_name}</b></button>`
                                                : "";
                                            frappe.msgprint({
                                                title: __("Simulation Completed"),
                                                message: `<b>${res.message}</b>${invoiceLink}`,
                                                indicator: "green",
                                                wide: true
                                            });
                                        } else {
                                            frappe.msgprint({
                                                title: __("Simulation Alert"),
                                                message: res.message || __("Simulation finished."),
                                                indicator: "orange"
                                            });
                                        }
                                    }
                                }
                            });
                        }
                    });
                    d.show();
                },
                __("UAE E-Invoice")
            );
        }
    }
});

function show_readiness_modal(data) {
    const s = data.score_summary || {};
    const isReady = data.overall_status === "READY";
    const statusColor = isReady ? "#28a745" : "#e03e2d";

    const badge = (status) => {
        if (status === "PASS") return '<span style="color:#28a745;font-weight:700;">✔ PASS</span>';
        if (status === "WARN") return '<span style="color:#f39c12;font-weight:700;">⚠ WARN</span>';
        return '<span style="color:#e03e2d;font-weight:700;">✘ FAIL</span>';
    };

    let tableRows = "";
    (data.checks || []).forEach(c => {
        tableRows += `
            <tr style="border-bottom:1px solid #e2e8f0;">
                <td style="padding:8px;font-weight:600;font-size:12px;color:#4a5568;">${c.category}</td>
                <td style="padding:8px;font-size:13px;font-weight:600;">${c.item}</td>
                <td style="padding:8px;text-align:center;">${badge(c.status)}</td>
                <td style="padding:8px;font-size:12px;color:#2d3748;">
                    ${c.message}
                    ${c.action ? `<br><small style="color:#718096;"><i>Action: ${c.action}</i></small>` : ""}
                </td>
            </tr>
        `;
    });

    const html = `
        <div style="margin-bottom:14px;padding:12px 16px;border-radius:6px;background:${isReady ? '#e6f4ea' : '#fce8e6'};border-left:5px solid ${statusColor};">
            <h4 style="margin:0 0 4px 0;color:${statusColor};">
                ${isReady ? "✅ Company is E-Invoicing Ready" : "⚠️ Action Required Before Going Live"}
            </h4>
            <div style="font-size:13px;color:#333;">
                Passed: <b>${s.pass || 0}</b> &nbsp;|&nbsp; Warnings: <b>${s.warn || 0}</b> &nbsp;|&nbsp; Failures: <b style="color:#e03e2d;">${s.fail || 0}</b>
            </div>
        </div>
        <table style="width:100%;border-collapse:collapse;margin-top:10px;">
            <thead style="background:#f7fafc;border-bottom:2px solid #cbd5e0;">
                <tr>
                    <th style="padding:8px;text-align:left;font-size:12px;color:#718096;">Category</th>
                    <th style="padding:8px;text-align:left;font-size:12px;color:#718096;">Compliance Check</th>
                    <th style="padding:8px;text-align:center;font-size:12px;color:#718096;">Status</th>
                    <th style="padding:8px;text-align:left;font-size:12px;color:#718096;">Details & Remediation</th>
                </tr>
            </thead>
            <tbody>${tableRows}</tbody>
        </table>
    `;

    frappe.msgprint({
        title: __("UAE E-Invoicing Pre-Flight Readiness Audit"),
        message: html,
        indicator: isReady ? "green" : "red",
        wide: true
    });
}
