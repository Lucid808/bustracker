"""SMS provider wrapper - Africa's Talking with print() fallback."""
import os

AT_USERNAME = os.environ.get("AT_USERNAME", "")
AT_API_KEY  = os.environ.get("AT_API_KEY", "")
AT_SENDER   = os.environ.get("AT_SENDER_ID", "")

_ready = False
_sms = None

if AT_USERNAME and AT_API_KEY:
    try:
        import africastalking
        africastalking.initialize(AT_USERNAME, AT_API_KEY)
        _sms = africastalking.SMS
        _ready = True
    except Exception as e:
        print(f"[SMS] Africa's Talking init failed: {e}")


def send(phone: str, text: str) -> dict:
    if not _ready:
        print(f"[SMS-PRINT] {phone}: {text}")
        return {"ok": True, "provider": "print"}

    try:
        kwargs = {}
        if AT_SENDER:
            kwargs["sender_id"] = AT_SENDER
        response = _sms.send(text, [phone], **kwargs)
        print(f"[SMS-AT] {phone}: {response}")
        return {"ok": True, "provider": "africastalking", "raw": response}
    except Exception as e:
        print(f"[SMS-AT-ERROR] {phone}: {e}")
        print(f"[SMS-PRINT] {phone}: {text}")
        return {"ok": False, "provider": "africastalking", "error": str(e)}
