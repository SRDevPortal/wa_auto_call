from __future__ import annotations

import frappe


def after_install() -> None:
    ensure_whatsapp_workspace_link()


def after_migrate() -> None:
    ensure_whatsapp_workspace_link()


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
