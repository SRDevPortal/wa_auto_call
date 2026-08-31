from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class WAAICall(Document):
    def validate(self):
        delay_seconds = cint(self.delay_seconds)
        if delay_seconds < 1 or delay_seconds > 604800:
            frappe.throw(_("Delay must be between 1 second and 7 days."))

        if self.queue not in {"short", "default", "long"}:
            frappe.throw(_("Queue must be short, default, or long."))

        if self.target_type == "Frappe Webhook":
            self.server_script_api_method = None
            self._validate_webhook()
        elif self.target_type == "Server Script API":
            self.webhook = None
            self._validate_server_script_api()
        else:
            frappe.throw(_("Target Type must be Frappe Webhook or Server Script API."))

    def _validate_webhook(self) -> None:
        if not self.webhook:
            frappe.throw(_("Webhook is required."))

        webhook_doctype = frappe.db.get_value("Webhook", self.webhook, "webhook_doctype")
        if webhook_doctype not in {"Chat Conversation", "Chat Message"}:
            frappe.throw(_("Webhook DocType must be Chat Conversation or Chat Message."))

    def _validate_server_script_api(self) -> None:
        api_method = (self.server_script_api_method or "").strip()
        if not api_method:
            frappe.throw(_("Server Script API Method is required."))

        server_script = frappe.db.get_value(
            "Server Script",
            {
                "script_type": "API",
                "api_method": api_method,
                "disabled": 0,
            },
            "name",
        )
        if not server_script:
            frappe.throw(_("Active Server Script API Method {0} was not found.").format(api_method))
