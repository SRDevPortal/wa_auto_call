frappe.ui.form.on("WA AI Call", {
    refresh(frm) {
        frm.toggle_reqd("webhook", frm.doc.target_type === "Frappe Webhook");
        frm.toggle_reqd("server_script_api_method", frm.doc.target_type === "Server Script API");
    },

    target_type(frm) {
        frm.trigger("refresh");
    },
});
