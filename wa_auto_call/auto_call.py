from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

import frappe
from frappe.utils import add_to_date, cint, now_datetime, time_diff_in_seconds
from frappe.utils.background_jobs import (
    RQ_JOB_FAILURE_TTL,
    RQ_RESULTS_TTL,
    create_job_id,
    execute_job,
    get_queue,
    get_queues_timeout,
)
from frappe.utils.safe_exec import get_safe_globals


CUSTOMER_SENDER_TYPES = {"", "Customer"}
INBOUND_DIRECTION = "Inbound"
TARGET_WEBHOOK = "Frappe Webhook"
TARGET_SERVER_SCRIPT = "Server Script API"
LIMIT_ONCE_PER_CONVERSATION = "Once Per Conversation"
LIMIT_EVERY_QUALIFIED_CUSTOMER_MESSAGE = "Every Qualified Customer Message"
SCHEDULER_CONVERSATION_LIMIT = 200


def on_chat_message_after_insert(doc, method=None) -> None:
    if doc.direction != INBOUND_DIRECTION or (doc.sender_type or "Customer") != "Customer":
        return

    channel_account = None
    if doc.conversation:
        channel_account = frappe.db.get_value("Chat Conversation", doc.conversation, "channel_account")
    schedule_auto_call_triggers(doc.conversation, doc.name, channel_account)


def schedule_auto_call_triggers(
    conversation: str | None,
    message: str | None,
    channel_account: str | None = None,
) -> None:
    if not conversation or not message:
        return
    if not frappe.db.exists("DocType", "WA AI Call"):
        return

    filters: dict[str, Any] = {"is_active": 1}
    trigger_filters = [
        {**filters, "channel_account": ["in", ["", None]]},
    ]
    if channel_account:
        trigger_filters.append({**filters, "channel_account": channel_account})

    names = set()
    triggers = []
    for trigger_filter in trigger_filters:
        for trigger in frappe.db.get_all(
            "WA AI Call",
            filters=trigger_filter,
            fields=["name", "delay_seconds", "queue"],
            order_by="priority desc, modified desc",
        ):
            if trigger.name in names:
                continue
            names.add(trigger.name)
            triggers.append(trigger)

    for trigger in triggers:
        _schedule_auto_call_trigger(
            trigger_name=trigger.name,
            conversation=conversation,
            message=message,
            delay_seconds=cint(trigger.delay_seconds),
            queue=trigger.queue or "default",
        )


def process_due_auto_call_triggers() -> None:
    if not frappe.db.exists("DocType", "WA AI Call"):
        return

    for trigger in frappe.db.get_all(
        "WA AI Call",
        filters={"is_active": 1},
        fields=["name", "channel_account", "delay_seconds"],
        order_by="priority desc, modified desc",
    ):
        cutoff = add_to_date(now_datetime(), seconds=-max(1, cint(trigger.delay_seconds)))
        filters: dict[str, Any] = {"last_customer_message_at": ["<=", cutoff]}
        if trigger.channel_account:
            filters["channel_account"] = trigger.channel_account

        conversations = frappe.db.get_all(
            "Chat Conversation",
            filters=filters,
            fields=["name"],
            order_by="last_customer_message_at asc",
            limit=SCHEDULER_CONVERSATION_LIMIT,
        )
        for conversation in conversations:
            message = _get_latest_customer_message(conversation.name)
            if not message:
                continue
            if _has_completed_attempt(trigger.name, conversation.name, message):
                continue
            if _has_successful_run(trigger.name, conversation.name, message):
                continue
            process_auto_call_trigger(trigger.name, conversation.name, message)


def process_auto_call_trigger(trigger_name: str, conversation: str, message: str) -> None:
    trigger = frappe.get_doc("WA AI Call", trigger_name)
    if not cint(trigger.is_active):
        _record_skip(trigger, conversation, message, "Trigger is inactive.")
        return

    conversation_doc = frappe.get_doc("Chat Conversation", conversation)
    message_doc = frappe.get_doc("Chat Message", message)

    if message_doc.conversation != conversation:
        _record_skip(trigger, conversation, message, "Message does not belong to conversation.")
        return
    if message_doc.direction != INBOUND_DIRECTION or (message_doc.sender_type or "Customer") != "Customer":
        _record_skip(trigger, conversation, message, "Message is not an inbound customer message.")
        return

    latest_customer_message = _get_latest_customer_message(conversation)
    if latest_customer_message != message:
        _record_skip(trigger, conversation, message, "A newer customer message exists.")
        return

    if _has_successful_run(trigger.name, conversation, message):
        _record_skip(trigger, conversation, message, "Trigger limit already reached for this conversation.")
        return

    delay_seconds = cint(trigger.delay_seconds)
    age_seconds = time_diff_in_seconds(now_datetime(), message_doc.creation)
    if age_seconds < delay_seconds:
        _schedule_auto_call_trigger(
            trigger_name=trigger.name,
            conversation=conversation,
            message=message,
            delay_seconds=delay_seconds - int(age_seconds),
            queue=trigger.queue or "default",
        )
        _record_skip(trigger, conversation, message, "Delay window was not complete; trigger rescheduled.")
        return

    try:
        condition_matches = _condition_matches(trigger, conversation_doc, message_doc)
    except Exception:
        details = frappe.get_traceback()
        _record_failure(trigger, conversation, message, details)
        raise

    if not condition_matches:
        _record_skip(trigger, conversation, message, "Condition did not match.")
        return

    try:
        response = _run_target(trigger, conversation_doc, message_doc)
    except Exception:
        details = frappe.get_traceback()
        _record_failure(trigger, conversation, message, details)
        raise

    _record_success(trigger, conversation, message, response)


def _schedule_auto_call_trigger(
    *,
    trigger_name: str,
    conversation: str,
    message: str,
    delay_seconds: int,
    queue: str,
) -> None:
    delay_seconds = max(1, cint(delay_seconds))
    queue = queue or "default"
    job_id = create_job_id(_build_job_id(trigger_name, conversation, message))
    timeout = get_queues_timeout().get(queue) or 300

    def enqueue_job() -> None:
        q = get_queue(queue)
        q.enqueue_in(
            timedelta(seconds=delay_seconds),
            execute_job,
            job_timeout=timeout,
            failure_ttl=frappe.conf.get("rq_job_failure_ttl") or RQ_JOB_FAILURE_TTL,
            result_ttl=frappe.conf.get("rq_results_ttl") or RQ_RESULTS_TTL,
            job_id=job_id,
            kwargs={
                "site": frappe.local.site,
                "user": frappe.session.user,
                "method": "wa_auto_call.auto_call.process_auto_call_trigger",
                "event": None,
                "job_name": "wa_auto_call.auto_call.process_auto_call_trigger",
                "is_async": True,
                "kwargs": {
                    "trigger_name": trigger_name,
                    "conversation": conversation,
                    "message": message,
                },
            },
        )

    if getattr(frappe.flags, "in_migrate", False):
        return
    frappe.db.after_commit.add(enqueue_job)


def _build_job_id(trigger_name: str, conversation: str, message: str) -> str:
    token = f"{trigger_name}:{conversation}:{message}:{now_datetime()}"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]
    return f"wa_ai_call_{digest}"


def _get_latest_customer_message(conversation: str) -> str | None:
    rows = frappe.db.get_all(
        "Chat Message",
        filters={"conversation": conversation, "direction": INBOUND_DIRECTION},
        fields=["name", "sender_type"],
        order_by="creation desc",
        limit=20,
    )
    for row in rows:
        if (row.sender_type or "Customer") in CUSTOMER_SENDER_TYPES:
            return row.name
    return None


def _condition_matches(trigger, conversation_doc, message_doc) -> bool:
    condition = (trigger.condition or "").strip()
    if not condition:
        return True
    return bool(
        frappe.safe_eval(
            condition,
            eval_locals={
                "doc": conversation_doc,
                "conversation": conversation_doc,
                "message": message_doc,
                "utils": get_safe_globals().get("frappe").get("utils"),
            },
        )
    )


def _is_once_per_conversation(trigger) -> bool:
    return (
        getattr(trigger, "trigger_limit", None) or LIMIT_ONCE_PER_CONVERSATION
    ) == LIMIT_ONCE_PER_CONVERSATION


def _has_successful_run(trigger_name: str, conversation: str, message: str) -> bool:
    if not frappe.db.exists("DocType", "Chat Action Log"):
        return False

    filters = {
        "conversation": conversation,
        "action_type": "Webhook",
        "action_name": trigger_name,
        "status": "Success",
    }
    trigger_limit = frappe.db.get_value("WA AI Call", trigger_name, "trigger_limit") or LIMIT_ONCE_PER_CONVERSATION
    if trigger_limit == LIMIT_EVERY_QUALIFIED_CUSTOMER_MESSAGE:
        filters["reference_name"] = message

    return bool(frappe.db.exists("Chat Action Log", filters))


def _has_completed_attempt(trigger_name: str, conversation: str, message: str) -> bool:
    if not frappe.db.exists("DocType", "Chat Action Log"):
        return False

    return bool(
        frappe.db.exists(
            "Chat Action Log",
            {
                "conversation": conversation,
                "action_type": "Webhook",
                "action_name": trigger_name,
                "reference_name": message,
                "status": ["in", ["Success", "Failed"]],
            },
        )
    )


def _run_target(trigger, conversation_doc, message_doc):
    if trigger.target_type == TARGET_WEBHOOK:
        return _run_webhook(trigger, conversation_doc, message_doc)
    if trigger.target_type == TARGET_SERVER_SCRIPT:
        return _run_server_script_api(trigger, conversation_doc, message_doc)
    frappe.throw(f"Unsupported WA AI Call target type: {trigger.target_type}")


def _run_webhook(trigger, conversation_doc, message_doc) -> dict[str, Any]:
    webhook = frappe.get_doc("Webhook", trigger.webhook)
    if not cint(webhook.enabled):
        frappe.throw(f"Webhook {webhook.name} is disabled.")

    target_doc = _target_doc_for_webhook(webhook.webhook_doctype, conversation_doc, message_doc)
    from frappe.integrations.doctype.webhook.webhook import enqueue_webhook

    enqueue_webhook(target_doc, webhook)
    return {
        "target_type": TARGET_WEBHOOK,
        "webhook": webhook.name,
        "webhook_doctype": webhook.webhook_doctype,
        "target_doc": target_doc.name,
    }


def _target_doc_for_webhook(webhook_doctype: str, conversation_doc, message_doc):
    if webhook_doctype == "Chat Conversation":
        return conversation_doc
    if webhook_doctype == "Chat Message":
        return message_doc

    reference_name = _linked_reference_name(conversation_doc, webhook_doctype)
    if reference_name:
        return frappe.get_doc(webhook_doctype, reference_name)

    frappe.throw(
        "Conversation {0} is not linked to a {1} document for this webhook.".format(
            conversation_doc.name,
            webhook_doctype,
        )
    )


def _linked_reference_name(conversation_doc, doctype: str) -> str | None:
    if doctype == "CRM Lead" and getattr(conversation_doc, "linked_crm_lead", None):
        return conversation_doc.linked_crm_lead
    if doctype == "Patient" and getattr(conversation_doc, "linked_patient", None):
        return conversation_doc.linked_patient
    if getattr(conversation_doc, "linked_reference_doctype", None) == doctype:
        return getattr(conversation_doc, "linked_reference_name", None)
    return None


def _run_server_script_api(trigger, conversation_doc, message_doc):
    api_method = (trigger.server_script_api_method or "").strip()
    server_script_name = frappe.db.get_value(
        "Server Script",
        {"script_type": "API", "api_method": api_method, "disabled": 0},
        "name",
    )
    if not server_script_name:
        frappe.throw(f"Active Server Script API Method {api_method} was not found.")

    server_script = frappe.get_doc("Server Script", server_script_name)
    previous_form_dict = getattr(frappe.local, "form_dict", None)
    frappe.local.form_dict = frappe._dict(_build_context(trigger, conversation_doc, message_doc))
    try:
        response = server_script.execute_method()
    finally:
        frappe.local.form_dict = previous_form_dict
    return {
        "target_type": TARGET_SERVER_SCRIPT,
        "server_script": server_script.name,
        "api_method": api_method,
        "response": response,
    }


def _build_context(trigger, conversation_doc, message_doc) -> dict[str, Any]:
    return {
        "cmd": (trigger.server_script_api_method or "").strip(),
        "trigger": trigger.name,
        "conversation": conversation_doc.name,
        "message": message_doc.name,
        "channel_account": conversation_doc.channel_account,
        "contact": conversation_doc.contact,
        "last_customer_message_at": conversation_doc.last_customer_message_at,
    }


def _record_success(trigger, conversation: str, message: str, response) -> None:
    _update_trigger_status(trigger.name, "Success", conversation, message, None)
    _log_chat_action(
        "Success",
        trigger,
        conversation,
        message,
        "WA AI Call target executed after the configured customer-message delay.",
        response=response,
    )


def _record_failure(trigger, conversation: str, message: str, details: str) -> None:
    error = (details or "")[-1000:]
    _update_trigger_status(trigger.name, "Failed", conversation, message, error)
    _log_chat_action("Failed", trigger, conversation, message, error)


def _record_skip(trigger, conversation: str, message: str, details: str) -> None:
    _update_trigger_status(trigger.name, "Skipped", conversation, message, details)
    if cint(getattr(trigger, "log_skipped_runs", 0)):
        _log_chat_action("Skipped", trigger, conversation, message, details)


def _log_chat_action(status: str, trigger, conversation: str, message: str, details: str, response=None) -> None:
    if not frappe.db.exists("DocType", "Chat Action Log"):
        return

    doc = frappe.get_doc(
        {
            "doctype": "Chat Action Log",
            "conversation": conversation,
            "channel_account": getattr(trigger, "channel_account", None),
            "action_source": "System",
            "action_type": "Webhook",
            "action_name": trigger.name,
            "status": status,
            "actor": frappe.session.user if getattr(frappe.session, "user", None) else None,
            "reference_doctype": "Chat Message",
            "reference_name": message,
            "details": details,
            "response_json": json.dumps(response or {}, default=str, ensure_ascii=True) if response else None,
        }
    )
    doc.insert(ignore_permissions=True)


def _update_trigger_status(
    trigger_name: str,
    status: str,
    conversation: str,
    message: str,
    error: str | None,
) -> None:
    frappe.db.set_value(
        "WA AI Call",
        trigger_name,
        {
            "last_status": status,
            "last_triggered_at": now_datetime(),
            "last_triggered_conversation": conversation,
            "last_triggered_message": message,
            "last_error": error,
        },
        update_modified=False,
    )
