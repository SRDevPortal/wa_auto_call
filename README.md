# WA Auto Call

Delayed WhatsApp customer-message trigger app for WA Chat Hub.

This app is intended to be installed as a separate Frappe app so WA Chat Hub core stays unchanged.

`WA AI Call` includes a `Trigger Limit` setting:

- `Once Per Conversation` is the default and allows only one successful trigger per conversation.
- `Every Qualified Customer Message` allows the trigger to run again after each later customer message that passes the configured delay.
