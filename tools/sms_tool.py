"""
SMS Tool — Send/receive text messages.

Two backends:
1. Twilio (full-featured) — if twilio package is installed
2. Email-to-SMS gateway (zero-dep fallback) — uses carrier gateways
   e.g., 5551234567@txt.att.net for AT&T

The email gateway is surprisingly useful — it works for outbound texts
with zero cost and zero signup. Receiving requires Twilio or similar.

Config in tools_config.json:
{
    "sms": {
        "enabled": true,
        "backend": "twilio" | "email_gateway",
        "twilio_sid": "...",
        "twilio_token": "...",
        "twilio_from": "+1...",
        "email_gateway": {
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "username": "agent@example.com",
            "password": "..."
        },
        "contacts": {
            "obi": {"phone": "+1...", "carrier": "att"},
            "mark": {"phone": "+1...", "carrier": "verizon"}
        }
    }
}
"""

import smtplib
from email.mime.text import MIMEText
from datetime import datetime

from . import BaseTool

# Carrier email-to-SMS gateways
CARRIER_GATEWAYS = {
    "att": "txt.att.net",
    "tmobile": "tmomail.net",
    "verizon": "vtext.com",
    "sprint": "messaging.sprintpcs.com",
    "uscellular": "email.uscc.net",
    "boost": "sms.myboostmobile.com",
    "cricket": "sms.cricketwireless.net",
    "metro": "mymetropcs.com",
    "virgin": "vmobl.com",
    "google_fi": "msg.fi.google.com",
    "mint": "tmomail.net",  # Mint uses T-Mobile network
}

# Lazy import twilio
_twilio_available = False
try:
    from twilio.rest import Client as TwilioClient
    _twilio_available = True
except ImportError:
    pass


class SMSTool(BaseTool):
    name = "sms"
    description = (
        "Send text messages via Twilio or email-to-SMS gateways. "
        "Email gateway mode is free and works for outbound texts to any US carrier. "
        "Twilio mode supports both sending and receiving. "
        "Contacts can be saved by name for easy reference."
    )
    actions = {
        "send": "Send a text. Params: to (phone or contact name), message, carrier (for gateway mode)",
        "contacts": "List saved contacts. No params.",
        "add_contact": "Save a contact. Params: name, phone, carrier (optional)",
        "carriers": "List supported carrier gateways. No params.",
        "status": "Check SMS backend status. No params.",
    }

    def is_configured(self) -> bool:
        backend = self.config.get("backend", "email_gateway")
        if backend == "twilio":
            return all(self.config.get(k) for k in ["twilio_sid", "twilio_token", "twilio_from"])
        elif backend == "email_gateway":
            gw = self.config.get("email_gateway", {})
            return all(gw.get(k) for k in ["smtp_host", "username", "password"])
        return False

    def execute(self, action: str, params: dict = None) -> dict:
        params = params or {}

        if action == "send":
            return self._send(params)
        elif action == "contacts":
            return self._contacts()
        elif action == "add_contact":
            return self._add_contact(params)
        elif action == "carriers":
            return {"ok": True, "carriers": CARRIER_GATEWAYS}
        elif action == "status":
            return self._status()

        return {"ok": False, "error": f"Unknown action: {action}"}

    def _resolve_contact(self, to: str) -> dict:
        """Resolve a name or phone number to contact info."""
        contacts = self.config.get("contacts", {})

        # Check if it's a saved contact name
        if to.lower() in contacts:
            return contacts[to.lower()]

        # Check case-insensitive match
        for name, info in contacts.items():
            if name.lower() == to.lower():
                return info

        # Assume it's a raw phone number
        return {"phone": to}

    def _send(self, params: dict) -> dict:
        to = params.get("to")
        message = params.get("message")

        if not to or not message:
            return {"ok": False, "error": "Missing required params: 'to', 'message'"}

        contact = self._resolve_contact(to)
        phone = contact.get("phone", to)
        carrier = params.get("carrier") or contact.get("carrier")

        backend = self.config.get("backend", "email_gateway")

        if backend == "twilio":
            return self._send_twilio(phone, message)
        elif backend == "email_gateway":
            return self._send_email_gateway(phone, message, carrier)
        else:
            return {"ok": False, "error": f"Unknown backend: {backend}"}

    def _send_twilio(self, phone: str, message: str) -> dict:
        if not _twilio_available:
            return {"ok": False, "error": "Twilio package not installed. Run: pip install twilio"}

        sid = self.config.get("twilio_sid")
        token = self.config.get("twilio_token")
        from_number = self.config.get("twilio_from")

        if not all([sid, token, from_number]):
            return {"ok": False, "error": "Twilio not configured. Set twilio_sid, twilio_token, twilio_from."}

        try:
            client = TwilioClient(sid, token)
            msg = client.messages.create(
                body=message,
                from_=from_number,
                to=phone
            )
            return {
                "ok": True,
                "sid": msg.sid,
                "to": phone,
                "status": msg.status,
                "timestamp": datetime.now().isoformat(),
            }
        except Exception as e:
            return {"ok": False, "error": f"Twilio error: {e}"}

    def _send_email_gateway(self, phone: str, message: str, carrier: str = None) -> dict:
        if not carrier:
            return {
                "ok": False,
                "error": (
                    "Carrier required for email gateway mode. "
                    f"Supported: {', '.join(CARRIER_GATEWAYS.keys())}. "
                    "Set it in the contact or pass carrier param."
                )
            }

        gateway = CARRIER_GATEWAYS.get(carrier.lower())
        if not gateway:
            return {"ok": False, "error": f"Unknown carrier '{carrier}'. Supported: {', '.join(CARRIER_GATEWAYS.keys())}"}

        # Strip non-digits from phone
        digits = "".join(c for c in phone if c.isdigit())
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]  # strip country code
        if len(digits) != 10:
            return {"ok": False, "error": f"Invalid US phone number: {phone} (got {len(digits)} digits)"}

        sms_email = f"{digits}@{gateway}"

        gw_config = self.config.get("email_gateway", {})
        smtp_host = gw_config.get("smtp_host")
        smtp_port = gw_config.get("smtp_port", 587)
        username = gw_config.get("username")
        password = gw_config.get("password")

        if not all([smtp_host, username, password]):
            return {"ok": False, "error": "Email gateway not configured. Set email_gateway.smtp_host/username/password."}

        try:
            msg = MIMEText(message)
            msg["From"] = username
            msg["To"] = sms_email
            # No subject for SMS — carriers sometimes prepend it

            server = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(username, password)
            server.sendmail(username, sms_email, msg.as_string())
            server.quit()

            return {
                "ok": True,
                "to": phone,
                "gateway": sms_email,
                "carrier": carrier,
                "message_length": len(message),
                "timestamp": datetime.now().isoformat(),
                "note": "Sent via email-to-SMS gateway. Delivery is carrier-dependent, usually arrives in 1-5 minutes.",
            }
        except Exception as e:
            return {"ok": False, "error": f"SMTP gateway error: {e}"}

    def _contacts(self) -> dict:
        contacts = self.config.get("contacts", {})
        return {"ok": True, "contacts": contacts}

    def _add_contact(self, params: dict) -> dict:
        name = params.get("name")
        phone = params.get("phone")

        if not name or not phone:
            return {"ok": False, "error": "Missing required params: 'name', 'phone'"}

        contacts = self.config.setdefault("contacts", {})
        contacts[name.lower()] = {
            "phone": phone,
            "carrier": params.get("carrier"),
        }

        return {
            "ok": True,
            "saved": name.lower(),
            "phone": phone,
            "carrier": params.get("carrier"),
            "note": "Contact saved. Remember to persist this via tool config update.",
        }

    def _status(self) -> dict:
        backend = self.config.get("backend", "email_gateway")
        return {
            "ok": True,
            "backend": backend,
            "twilio_available": _twilio_available,
            "configured": self.is_configured(),
            "contact_count": len(self.config.get("contacts", {})),
            "supported_carriers": list(CARRIER_GATEWAYS.keys()),
        }
