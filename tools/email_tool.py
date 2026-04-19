"""
Email Tool — SMTP send + IMAP receive.

Zero external dependencies. Uses Python's built-in smtplib/imaplib/email.
Supports TLS (port 587) and SSL (port 465).

Config required in tools_config.json:
{
    "email": {
        "enabled": true,
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "username": "agent@example.com",
        "password": "app-password-here",
        "from_name": "Harness Agent",
        "use_ssl": true
    }
}
"""

import smtplib
import imaplib
import email as email_lib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr, parseaddr, formatdate
from email.header import decode_header
from datetime import datetime
from typing import Optional

from . import BaseTool


def _decode_header_value(value):
    """Decode an email header that might be encoded."""
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)


def _extract_body(msg) -> str:
    """Extract plain text body from an email message."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if content_type == "text/plain" and "attachment" not in disposition:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        # Fallback: try HTML
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return f"[HTML] {payload.decode(charset, errors='replace')[:2000]}"
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""


class EmailTool(BaseTool):
    name = "email"
    description = (
        "Send and receive email via SMTP/IMAP. "
        "Can compose, send, read inbox, search, and manage folders. "
        "Use for communicating with team members, receiving notifications, "
        "or any email-based workflow."
    )
    actions = {
        "send": "Send an email. Params: to, subject, body, cc (optional), reply_to (optional)",
        "inbox": "Read recent inbox messages. Params: limit (default 10), folder (default INBOX)",
        "read": "Read a specific email by UID. Params: uid, folder (default INBOX)",
        "search": "Search emails. Params: query (IMAP search string), folder (default INBOX), limit (default 10)",
        "folders": "List available mailbox folders. No params.",
        "mark_read": "Mark an email as read. Params: uid, folder (default INBOX)",
        "count": "Count messages in a folder. Params: folder (default INBOX), status (ALL/UNSEEN/SEEN, default ALL)",
    }

    def is_configured(self) -> bool:
        required = ["smtp_host", "username", "password"]
        return all(self.config.get(k) for k in required)

    def _get_smtp(self) -> smtplib.SMTP:
        """Connect to SMTP server."""
        host = self.config["smtp_host"]
        port = self.config.get("smtp_port", 587)

        if self.config.get("use_ssl") and port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.ehlo()
            server.starttls()
            server.ehlo()

        server.login(self.config["username"], self.config["password"])
        return server

    def _get_imap(self) -> imaplib.IMAP4_SSL:
        """Connect to IMAP server."""
        host = self.config.get("imap_host", self.config.get("smtp_host", ""))
        port = self.config.get("imap_port", 993)
        conn = imaplib.IMAP4_SSL(host, port)
        conn.login(self.config["username"], self.config["password"])
        return conn

    def execute(self, action: str, params: dict = None) -> dict:
        params = params or {}

        if not self.is_configured():
            return {"ok": False, "error": "Email not configured. Set smtp_host, username, and password in tools config."}

        if action == "send":
            return self._send(params)
        elif action == "inbox":
            return self._inbox(params)
        elif action == "read":
            return self._read(params)
        elif action == "search":
            return self._search(params)
        elif action == "folders":
            return self._folders()
        elif action == "mark_read":
            return self._mark_read(params)
        elif action == "count":
            return self._count(params)

        return {"ok": False, "error": f"Unknown action: {action}"}

    def _send(self, params: dict) -> dict:
        """Send an email."""
        to = params.get("to")
        subject = params.get("subject", "(no subject)")
        body = params.get("body", "")
        cc = params.get("cc")
        reply_to = params.get("reply_to")

        if not to:
            return {"ok": False, "error": "Missing required param: 'to'"}

        from_name = self.config.get("from_name", "Harness Agent")
        from_addr = self.config["username"]

        msg = MIMEMultipart()
        msg["From"] = formataddr((from_name, from_addr))
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)

        if cc:
            msg["Cc"] = cc
        if reply_to:
            msg["Reply-To"] = reply_to

        msg.attach(MIMEText(body, "plain", "utf-8"))

        recipients = [to]
        if cc:
            recipients.extend([addr.strip() for addr in cc.split(",")])

        try:
            server = self._get_smtp()
            server.sendmail(from_addr, recipients, msg.as_string())
            server.quit()
            return {
                "ok": True,
                "message": f"Email sent to {to}",
                "subject": subject,
                "timestamp": datetime.now().isoformat(),
            }
        except Exception as e:
            return {"ok": False, "error": f"SMTP error: {e}"}

    def _inbox(self, params: dict) -> dict:
        """Read recent inbox messages (headers + preview)."""
        folder = params.get("folder", "INBOX")
        limit = min(params.get("limit", 10), 50)

        try:
            conn = self._get_imap()
            conn.select(folder, readonly=True)
            _, data = conn.search(None, "ALL")
            uids = data[0].split()

            # Get most recent
            recent_uids = uids[-limit:] if len(uids) > limit else uids
            recent_uids.reverse()  # newest first

            messages = []
            for uid in recent_uids:
                _, msg_data = conn.fetch(uid, "(RFC822.HEADER FLAGS)")
                if msg_data and msg_data[0]:
                    raw = msg_data[0][1]
                    msg = email_lib.message_from_bytes(raw)
                    flags_raw = msg_data[0][0].decode() if isinstance(msg_data[0][0], bytes) else str(msg_data[0][0])
                    is_read = "\\Seen" in flags_raw

                    messages.append({
                        "uid": uid.decode() if isinstance(uid, bytes) else str(uid),
                        "from": _decode_header_value(msg.get("From", "")),
                        "to": _decode_header_value(msg.get("To", "")),
                        "subject": _decode_header_value(msg.get("Subject", "")),
                        "date": msg.get("Date", ""),
                        "read": is_read,
                    })

            conn.close()
            conn.logout()

            return {"ok": True, "folder": folder, "total": len(uids), "messages": messages}

        except Exception as e:
            return {"ok": False, "error": f"IMAP error: {e}"}

    def _read(self, params: dict) -> dict:
        """Read full email by UID."""
        uid = params.get("uid")
        folder = params.get("folder", "INBOX")

        if not uid:
            return {"ok": False, "error": "Missing required param: 'uid'"}

        try:
            conn = self._get_imap()
            conn.select(folder, readonly=True)
            _, msg_data = conn.fetch(str(uid).encode(), "(RFC822)")

            if not msg_data or not msg_data[0]:
                conn.logout()
                return {"ok": False, "error": f"Message UID {uid} not found"}

            raw = msg_data[0][1]
            msg = email_lib.message_from_bytes(raw)
            body = _extract_body(msg)

            # Get attachments info
            attachments = []
            if msg.is_multipart():
                for part in msg.walk():
                    disp = str(part.get("Content-Disposition", ""))
                    if "attachment" in disp:
                        filename = part.get_filename() or "unnamed"
                        size = len(part.get_payload(decode=True) or b"")
                        attachments.append({"filename": filename, "size": size})

            conn.close()
            conn.logout()

            return {
                "ok": True,
                "uid": uid,
                "from": _decode_header_value(msg.get("From", "")),
                "to": _decode_header_value(msg.get("To", "")),
                "cc": _decode_header_value(msg.get("Cc", "")),
                "subject": _decode_header_value(msg.get("Subject", "")),
                "date": msg.get("Date", ""),
                "body": body[:10000],  # cap at 10k chars
                "body_truncated": len(body) > 10000,
                "attachments": attachments,
            }

        except Exception as e:
            return {"ok": False, "error": f"IMAP error: {e}"}

    def _search(self, params: dict) -> dict:
        """Search emails using IMAP search syntax."""
        query = params.get("query", "ALL")
        folder = params.get("folder", "INBOX")
        limit = min(params.get("limit", 10), 50)

        try:
            conn = self._get_imap()
            conn.select(folder, readonly=True)
            _, data = conn.search(None, query)
            uids = data[0].split()

            recent_uids = uids[-limit:] if len(uids) > limit else uids
            recent_uids.reverse()

            messages = []
            for uid in recent_uids:
                _, msg_data = conn.fetch(uid, "(RFC822.HEADER)")
                if msg_data and msg_data[0]:
                    raw = msg_data[0][1]
                    msg = email_lib.message_from_bytes(raw)
                    messages.append({
                        "uid": uid.decode() if isinstance(uid, bytes) else str(uid),
                        "from": _decode_header_value(msg.get("From", "")),
                        "subject": _decode_header_value(msg.get("Subject", "")),
                        "date": msg.get("Date", ""),
                    })

            conn.close()
            conn.logout()

            return {"ok": True, "query": query, "matches": len(uids), "messages": messages}

        except Exception as e:
            return {"ok": False, "error": f"IMAP error: {e}"}

    def _folders(self) -> dict:
        """List available mailbox folders."""
        try:
            conn = self._get_imap()
            _, folders = conn.list()
            folder_names = []
            for f in folders:
                decoded = f.decode() if isinstance(f, bytes) else str(f)
                # Parse IMAP folder listing: (flags) "delimiter" "name"
                parts = decoded.rsplit('"', 2)
                if len(parts) >= 2:
                    folder_names.append(parts[-1].strip().strip('"'))
                else:
                    folder_names.append(decoded)
            conn.logout()
            return {"ok": True, "folders": folder_names}
        except Exception as e:
            return {"ok": False, "error": f"IMAP error: {e}"}

    def _mark_read(self, params: dict) -> dict:
        """Mark an email as read."""
        uid = params.get("uid")
        folder = params.get("folder", "INBOX")

        if not uid:
            return {"ok": False, "error": "Missing required param: 'uid'"}

        try:
            conn = self._get_imap()
            conn.select(folder)
            conn.store(str(uid).encode(), "+FLAGS", "\\Seen")
            conn.close()
            conn.logout()
            return {"ok": True, "uid": uid, "marked": "read"}
        except Exception as e:
            return {"ok": False, "error": f"IMAP error: {e}"}

    def _count(self, params: dict) -> dict:
        """Count messages in a folder."""
        folder = params.get("folder", "INBOX")
        status = params.get("status", "ALL")

        try:
            conn = self._get_imap()
            conn.select(folder, readonly=True)
            _, data = conn.search(None, status)
            count = len(data[0].split()) if data[0] else 0
            conn.close()
            conn.logout()
            return {"ok": True, "folder": folder, "status": status, "count": count}
        except Exception as e:
            return {"ok": False, "error": f"IMAP error: {e}"}
