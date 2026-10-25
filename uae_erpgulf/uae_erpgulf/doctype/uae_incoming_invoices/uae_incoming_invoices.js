frappe.ui.form.on("UAE Incoming Invoices", {
    refresh(frm) {
        if (!frm.doc.__islocal) {
            // Dashboard Status & Link
            if (frm.doc.purchase_invoice) {
                frm.dashboard.set_headline(
                    `<span class="indicator green">Linked Purchase Invoice: <a href="/app/purchase-invoice/${frm.doc.purchase_invoice}" style="font-weight:600;">${frm.doc.purchase_invoice}</a></span>`
                );

                frm.add_custom_button(
                    __("Open Purchase Invoice"),
                    () => {
                        frappe.set_route("Form", "Purchase Invoice", frm.doc.purchase_invoice);
                    },
                    __("Actions")
                );
            }

            // Create Purchase Invoice button if not yet processed
            if (!frm.doc.purchase_invoice && frm.doc.status !== "Draft Created") {
                frm.add_custom_button(
                    __("Create Purchase Invoice"),
                    () => {
                        frm.call({
                            method: "uae_erpgulf.core.inbound_processor.process_inbound_record",
                            args: {
                                inbound_record_name: frm.doc.name
                            },
                            freeze: true,
                            freeze_message: __("Processing incoming e-invoice and running P2P matching..."),
                            callback(r) {
                                if (!r.exc && r.message) {
                                    const res = r.message;
                                    if (res.status === "success" && res.purchase_invoice) {
                                        let alertMsg = __("Draft Purchase Invoice {0} created.", [res.purchase_invoice]);
                                        frappe.show_alert({
                                            message: alertMsg,
                                            indicator: "green"
                                        }, 7);

                                        if (res.warnings && res.warnings.length > 0) {
                                            const warningList = res.warnings.map(w => `<li>${frappe.utils.escape_html(w)}</li>`).join("");
                                            frappe.msgprint({
                                                title: __("P2P Matching Warnings"),
                                                indicator: "orange",
                                                message: `<p>The invoice was drafted, but please review the following variances before submitting:</p><ul>${warningList}</ul>`
                                            });
                                        }

                                        // Navigate directly to the new Draft Purchase Invoice
                                        setTimeout(() => {
                                            frappe.set_route("Form", "Purchase Invoice", res.purchase_invoice);
                                        }, 1200);
                                    }
                                }
                            }
                        });
                    }
                ).addClass("btn-primary");
            }
        }
    }
});
