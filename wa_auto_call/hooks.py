app_name = "wa_auto_call"
app_title = "WA Auto Call"
app_publisher = "SRIAAS"
app_description = "Delayed WhatsApp customer-message triggers for WA Chat Hub"
app_email = "webdevelopersriaas@gmail.com"
app_license = "MIT"

required_apps = ["wa_chat_hub"]

after_install = "wa_auto_call.install.after_install"
after_migrate = "wa_auto_call.install.after_migrate"

doc_events = {
    "Chat Message": {
        "after_insert": "wa_auto_call.auto_call.on_chat_message_after_insert",
    },
}
