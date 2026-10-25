
frappe.ui.form.on("Sales Invoice", {
    refresh(frm) {
        // Do NOT call frm.clear_custom_buttons() as that destroys native ERPNext buttons

        // Button 1: Send / Re-send to UAE E-Invoice
        if (
            frm.doc.docstatus === 1 &&
            (
                frm.doc.custom_uae_einvoice_status === "Not Submitted" ||
                frm.doc.custom_uae_einvoice_status === "Failed" ||
                frm.doc.custom_reporting_status === "failed"
            )
        ) {
            frm.add_custom_button(
                __("Send Invoice"),
                () => {
                    frm.call({
                        method: "uae_erpgulf.uae_erpgulf.queue.manual_retry_submission",
                        args: {
                            doctype: frm.doc.doctype,
                            docname: frm.doc.name
                        },
                        freeze: true,
                        freeze_message: __("Queuing UAE E-Invoice submission..."),
                        callback(r) {
                            if (!r.exc) {
                                frappe.show_alert({
                                    message: __("UAE E-Invoice submission queued in background."),
                                    indicator: "blue"
                                });
                                frm.reload_doc();
                            }
                        }
                    });
                },
                __("UAE E-Invoice")
            );
        }

        // Button 2: Check Document Status
        if (!frm.doc.__islocal && frm.doc.custom_uae_einvoice_status && frm.doc.custom_uae_einvoice_status !== "Not Submitted") {
            frm.add_custom_button(
                __("Get Document Status"),
                function () {
                    frappe.call({
                        method: "uae_erpgulf.uae_erpgulf.queue.refresh_einvoice_status",
                        args: {
                            doctype: frm.doc.doctype,
                            docname: frm.doc.name
                        },
                        freeze: true,
                        freeze_message: __("Checking Document Status..."),
                        callback: function (r) {
                            if (r.message) {
                                const res = r.message;
                                const data = res.data || {};

                                const rows = [
                                    ["Status", res.status || "-"],
                                    ["Message", res.message || "-"],
                                    ["Document ID", data.id || "-"],
                                    ["Exchange Status", data.exchange_status || "-"],
                                    ["Reporting Status", data.reporting_status || "-"],
                                    ["Provider Status", data.provider_status || "-"],
                                    ["Reporting Reference", data.reporting_reference || "-"],
                                ];

                                const tableRows = rows.map(([field, value]) => `
                                    <tr>
                                        <td style="padding:8px 12px;border:1px solid #d1d8dd !important;font-weight:600;width:40%;">${field}</td>
                                        <td style="padding:8px 12px;border:1px solid #d1d8dd !important;">${value}</td>
                                    </tr>
                                `).join("");

                                const html = `
                                    <table style="border-collapse: collapse; width: 100%; font-size: 13px;">
                                        <thead>
                                            <tr style="background-color: #f0f4f7;">
                                                <th style="padding:8px 12px; border:1px solid #d1d8dd; text-align:left;">Field</th>
                                                <th style="padding:8px 12px; border:1px solid #d1d8dd; text-align:left;">Value</th>
                                            </tr>
                                        </thead>
                                        <tbody>${tableRows}</tbody>
                                    </table>
                                `;

                                frappe.msgprint({
                                    title: __("UAE E-Invoice Document Status"),
                                    message: html,
                                    indicator: data.reporting_status === "reported" ? "green" : "orange",
                                    wide: true
                                });

                                frm.reload_doc();
                            }
                        }
                    });
                },
                __("UAE E-Invoice")
            );
        }
    }
});