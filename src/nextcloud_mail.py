"""
title: Nextcloud Mail
description: Read (and optionally send) email through the Nextcloud Mail app's OCS API, using a Nextcloud app password.
version: 0.3.0
requirements: requests
"""

import html
import re
from datetime import datetime, timezone

import requests
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        NEXTCLOUD_URL: str = Field(
            default="https://nextcloud.dimension-x.net",
            description="Base URL of your Nextcloud",
        )
        NEXTCLOUD_USER: str = Field(default="", description="Nextcloud username")
        NEXTCLOUD_APP_PASSWORD: str = Field(
            default="", description="Nextcloud app password / token"
        )
        ACCOUNT_ID: int = Field(
            default=0, description="Mail account id to use (0 = first account)"
        )
        ALLOW_SEND: bool = Field(
            default=False, description="Allow the model to send email"
        )
        MAX_BODY_CHARS: int = Field(
            default=8000, description="Truncate message bodies to this length"
        )
        MARK_READ_ON_OPEN: bool = Field(
            default=True,
            description="read_email marks the email as read (like opening it in the Mail app)",
        )
        MARK_ALL_LIMIT: int = Field(
            default=500, description="Most emails mark_all_read will flag in one call"
        )

    def __init__(self):
        self.valves = self.Valves()

    # ---------- HTTP helpers ----------

    def _request(self, method: str, url: str, params=None, json=None):
        v = self.valves
        if not (v.NEXTCLOUD_USER and v.NEXTCLOUD_APP_PASSWORD):
            raise ValueError(
                "Nextcloud credentials are not configured in the tool valves."
            )
        try:
            return requests.request(
                method,
                url,
                params=params,
                json=json,
                auth=(v.NEXTCLOUD_USER, v.NEXTCLOUD_APP_PASSWORD),
                headers={"OCS-APIRequest": "true", "Accept": "application/json"},
                timeout=30,
            )
        except requests.RequestException as e:
            raise RuntimeError(f"Could not reach Nextcloud at {url}: {e}")

    def _set_seen(self, message_id: int, seen: bool = True) -> None:
        """Set or clear the read flag via the Mail app's own flags route."""
        url = (
            f"{self.valves.NEXTCLOUD_URL.rstrip('/')}"
            f"/index.php/apps/mail/api/messages/{int(message_id)}/flags"
        )
        r = self._request("PUT", url, json={"flags": {"seen": bool(seen)}})
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} setting read flag: {r.text[:200]}")

    def _ocs(self, method: str, path: str, params=None, json=None):
        url = f"{self.valves.NEXTCLOUD_URL.rstrip('/')}/ocs/v2.php/apps/mail{path}"
        r = self._request(method, url, params, json)
        try:
            payload = r.json()["ocs"]
        except Exception:
            raise RuntimeError(f"HTTP {r.status_code} from {url}: {r.text[:300]}")
        if r.status_code >= 400:
            raise RuntimeError(
                f"HTTP {r.status_code}: {payload.get('data') or payload.get('meta')}"
            )
        return payload["data"]

    def _account_id(self) -> int:
        if self.valves.ACCOUNT_ID:
            return self.valves.ACCOUNT_ID
        accounts = self._ocs("GET", "/account/list")
        if not accounts:
            raise RuntimeError("No mail accounts configured in Nextcloud Mail.")
        return accounts[0]["id"]

    def _mailbox_id(self, name: str) -> int:
        boxes = self._ocs(
            "GET", "/ocs/mailboxes", params={"accountId": self._account_id()}
        )
        for b in boxes:
            if b["name"].lower() == name.lower():
                return b["databaseId"]
        raise RuntimeError(
            f"Mailbox '{name}' not found. Available: {', '.join(b['name'] for b in boxes)}"
        )

    # ---------- formatting helpers ----------

    @staticmethod
    def _addrs(lst) -> str:
        return ", ".join(
            (
                f"{a.get('label')} <{a.get('email')}>"
                if a.get("label") and a.get("label") != a.get("email")
                else a.get("email", "")
            )
            for a in (lst or [])
        )

    @staticmethod
    def _date(ts) -> str:
        try:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            )
        except Exception:
            return ""

    def _clean_body(self, body: str, is_html: bool) -> str:
        text = body or ""
        if is_html:
            text = re.sub(r"<(script|style)\b.*?</\1>", "", text, flags=re.S | re.I)
            text = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", text, flags=re.I)
            text = re.sub(r"<[^>]+>", "", text)
            text = html.unescape(text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
        limit = self.valves.MAX_BODY_CHARS
        return text if len(text) <= limit else text[:limit] + "\n...[truncated]"

    # ---------- tools exposed to the model ----------

    def mail_guide(self) -> str:
        """
        Explain what the Nextcloud Mail tools can and cannot do. Call this first if unsure how to use them.
        """
        send = "ENABLED" if self.valves.ALLOW_SEND else "DISABLED (the user must turn on ALLOW_SEND)"
        return (
            "Nextcloud Mail tools\n"
            "\n"
            "Workflow: list_mailboxes -> list_emails (note each email's id) -> read_email(id).\n"
            "- list_mailboxes: folders and unread counts.\n"
            "- list_emails(mailbox, count, search): newest first, max 50. Shows id, date, read/UNREAD, sender,\n"
            "  subject, preview. 'search' filters by text. Ids are numbers; use them exactly as shown.\n"
            "- read_email(message_id, mark_read=True): full text, headers and attachment NAMES. It marks the email\n"
            "  as read; pass mark_read=false to peek without changing it.\n"
            "- mark_read(message_id, read=True): mark one email read, or unread with read=false.\n"
            "- mark_all_read(mailbox): marks EVERY unread email in the folder as read. Only when the user asks.\n"
            f"- send_email(to, subject, body, cc, in_reply_to_message_id): sending is {send}. Plain text only.\n"
            "  Only send after the user has approved the recipient and content. To reply, pass the Message-ID\n"
            "  line from read_email as in_reply_to_message_id.\n"
            "\n"
            "Limits: no attachments (reading or sending), no HTML or inline images, no deleting, moving or\n"
            "drafting. To share an image, put its URL in the body text. Long bodies are truncated, and HTML\n"
            "emails are converted to plain text, so links and formatting may be lost.\n"
        )

    def list_mailboxes(self) -> str:
        """
        List the email folders (mailboxes) and their unread counts.
        """
        try:
            boxes = self._ocs(
                "GET", "/ocs/mailboxes", params={"accountId": self._account_id()}
            )
            return (
                "\n".join(f"{b['name']} (unread: {b.get('unread', 0)})" for b in boxes)
                or "No mailboxes."
            )
        except Exception as e:
            return f"Error: {e}"

    def list_emails(
        self, mailbox: str = "INBOX", count: int = 10, search: str = ""
    ) -> str:
        """
        List the newest emails in a mailbox. Returns each email's id, date, sender, subject, read state and a preview.
        :param mailbox: Folder name, e.g. INBOX.
        :param count: Number of emails to return (1-50).
        :param search: Optional search text to filter emails.
        """
        try:
            params = {"limit": max(1, min(int(count), 50)), "view": "singleton"}
            if search:
                params["filter"] = search
            msgs = self._ocs(
                "GET",
                f"/ocs/mailboxes/{self._mailbox_id(mailbox)}/messages",
                params=params,
            )
            if not msgs:
                return "No emails found."
            lines = []
            for m in msgs:
                flags = m.get("flags", {})
                state = "read" if flags.get("seen") else "UNREAD"
                attach = " 📎" if flags.get("hasAttachments") else ""
                preview = (m.get("previewText") or "").strip().replace("\n", " ")[:140]
                lines.append(
                    f"id {m['databaseId']} | {self._date(m.get('dateInt'))} | {state}{attach}\n"
                    f"  From: {self._addrs(m.get('from'))}\n"
                    f"  Subject: {m.get('subject', '')}\n"
                    f"  Preview: {preview}"
                )
            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"

    def read_email(self, message_id: int, mark_read: bool = True) -> str:
        """
        Read the full content of one email, by the id returned from list_emails. Marks it as read.
        :param message_id: The email's id.
        :param mark_read: Mark the email as read (default true). Pass false to peek without changing its state.
        """
        try:
            message_id = int(message_id)
            m = self._ocs("GET", f"/message/{message_id}")
            out = (
                f"From: {self._addrs(m.get('from'))}\n"
                f"To: {self._addrs(m.get('to'))}\n"
            )
            if m.get("cc"):
                out += f"Cc: {self._addrs(m.get('cc'))}\n"
            out += f"Date: {self._date(m.get('dateInt'))}\nSubject: {m.get('subject', '')}\n"
            atts = [
                a.get("fileName") for a in m.get("attachments", []) if a.get("fileName")
            ]
            if atts:
                out += f"Attachments: {', '.join(atts)}\n"
            out += f"Message-ID: {m.get('messageId', '')}\n\n"
            out += self._clean_body(m.get("body", ""), bool(m.get("hasHtmlBody")))
            if mark_read and self.valves.MARK_READ_ON_OPEN:
                try:
                    self._set_seen(message_id, True)
                except Exception as e:
                    out += f"\n\n[Note: could not mark the email as read: {e}]"
            return out
        except Exception as e:
            return f"Error: {e}"

    def mark_read(self, message_id: int, read: bool = True) -> str:
        """
        Mark one email as read (or unread, with read=false).
        :param message_id: The email's id from list_emails.
        :param read: True to mark read, False to mark unread.
        """
        try:
            self._set_seen(int(message_id), bool(read))
            return f"Email {int(message_id)} marked {'read' if read else 'unread'}."
        except Exception as e:
            return f"Error: {e}"

    def mark_all_read(self, mailbox: str = "INBOX") -> str:
        """
        Mark every unread email in a mailbox as read. Only use when the user asked for it.
        :param mailbox: Folder name, e.g. INBOX.
        """
        try:
            mailbox_id = self._mailbox_id(mailbox)
            limit = max(1, int(self.valves.MARK_ALL_LIMIT))
            done, failed, handled = 0, [], set()
            while done + len(failed) < limit:
                msgs = self._ocs(
                    "GET",
                    f"/ocs/mailboxes/{mailbox_id}/messages",
                    params={"limit": 50, "view": "singleton", "filter": "is:unread"},
                )
                batch = [
                    m
                    for m in msgs or []
                    if not (m.get("flags") or {}).get("seen")
                    and m["databaseId"] not in handled
                ]
                if not batch:
                    break
                for m in batch:
                    handled.add(m["databaseId"])
                    try:
                        self._set_seen(m["databaseId"], True)
                        done += 1
                    except Exception as e:
                        failed.append(f"{m['databaseId']} ({e})")
                    if done + len(failed) >= limit:
                        break
            out = f"Marked {done} email(s) in {mailbox} as read."
            if failed:
                out += f" {len(failed)} failed: {'; '.join(failed[:5])}"
            if done + len(failed) >= limit:
                out += f" Stopped at the limit of {limit}; run again for more."
            return out
        except Exception as e:
            return f"Error: {e}"

    def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        in_reply_to_message_id: str = "",
    ) -> str:
        """
        Send a plain-text email (no attachments or HTML; put image links in the body as URLs). Only use when the user has explicitly asked to send it and approved the content.
        :param to: Comma-separated recipient email addresses.
        :param subject: Subject line.
        :param body: Plain-text message body.
        :param cc: Optional comma-separated CC addresses.
        :param in_reply_to_message_id: Optional Message-ID header of the email being replied to (from read_email).
        """
        if not self.valves.ALLOW_SEND:
            return "Sending is disabled. The user can enable ALLOW_SEND in the tool's valves."
        try:
            account_id = self._account_id()
            accounts = self._ocs("GET", "/account/list")
            from_email = next(a["email"] for a in accounts if a["id"] == account_id)

            def split(s):
                return [{"email": x.strip()} for x in s.split(",") if x.strip()]

            payload = {
                "accountId": account_id,
                "fromEmail": from_email,
                "subject": subject,
                "body": body,
                "isHtml": False,
                "to": split(to),
                "cc": split(cc),
            }
            if in_reply_to_message_id:
                payload["references"] = in_reply_to_message_id
            self._ocs("POST", "/message/send", json=payload)
            return f"Sent to {to}."
        except Exception as e:
            return f"Error: {e}"
