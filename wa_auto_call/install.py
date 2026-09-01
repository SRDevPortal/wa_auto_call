from __future__ import annotations

import frappe

TRIGGER_LIMIT_FIELD = {
    "doctype": "Custom Field",
    "dt": "WA AI Call",
    "fieldname": "trigger_limit",
    "label": "Trigger Limit",
    "fieldtype": "Select",
    "insert_after": "queue",
    "options": "Once Per Conversation\nEvery Qualified Customer Message",
    "default": "Once Per Conversation",
    "description": "Once Per Conversation prevents this trigger from firing again for the same conversation after a successful run.",
    "in_list_view": 1,
}


def after_install() -> None:
    ensure_trigger_limit_field()
    ensure_whatsapp_workspace_link()


def after_migrate() -> None:
    ensure_trigger_limit_field()
    ensure_whatsapp_workspace_link()


def ensure_trigger_limit_field() -> None:
    if not frappe.db.exists("DocType", "WA AI Call"):
        return
    if not frappe.db.exists("DocType", "Custom Field"):
        return

    meta = frappe.get_meta("WA AI Call")
    if meta.get_field(TRIGGER_LIMIT_FIELD["fieldname"]):
        custom_field_name = f"WA AI Call-{TRIGGER_LIMIT_FIELD['fieldname']}"
        if frappe.db.exists("Custom Field", custom_field_name):
            _update_custom_field(custom_field_name)
        return

    frappe.get_doc(TRIGGER_LIMIT_FIELD).insert(ignore_permissions=True)
    frappe.clear_cache(doctype="WA AI Call")


def _update_custom_field(custom_field_name: str) -> None:
    values = {
        key: value
        for key, value in TRIGGER_LIMIT_FIELD.items()
        if key not in {"doctype", "dt", "fieldname"}
    }
    frappe.db.set_value("Custom Field", custom_field_name, values, update_modified=False)
    frappe.clear_cache(doctype="WA AI Call")


def ensure_whatsapp_workspace_link() -> None:
    if not frappe.db.exists("Workspace", "WhatsApp"):
        return
    if not frappe.db.exists("DocType", "WA AI Call"):
        return

    workspace = frappe.get_doc("Workspace", "WhatsApp")
    if any(row.label == "WA AI Call" for row in workspace.get("links") or []):
        return

    row = {
        "label": "WA AI Call",
        "type": "Link",
        "link_type": "DocType",
        "link_to": "WA AI Call",
    }
    _insert_after_card(workspace, "Automation and AI", row)
    workspace.flags.ignore_links = True
    workspace.save(ignore_permissions=True)


def _insert_after_card(workspace, card_label: str, row: dict) -> None:
    links = list(workspace.get("links") or [])
    if not links:
        workspace.append("links", row)
        return

    insert_at = len(links)
    in_card = False
    for index, item in enumerate(links):
        if item.type == "Card Break":
            if in_card:
                insert_at = index
                break
            in_card = item.label == card_label

    links.insert(insert_at, frappe._dict(row))
    workspace.set("links", links)
