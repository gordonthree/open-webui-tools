"""
title: Nextcloud Mail
description: Read (and optionally send) email through the Nextcloud Mail app's OCS API, using a Nextcloud app password.
version: 0.5.0
requirements: requests
"""

import html
import re
from datetime import datetime, timezone

import requests
from pydantic import BaseModel, Field


_QUOTE_LINE = re.compile(r"^\s*>")
_ATTRIBUTION = re.compile(r"(wrote|writes|said|schrieb|écrit|escribió)\s*:?\s*$|\b(19|20)\d\d\b.*\b\d{1,2}:\d\d|^-{2,}\s*original message", re.I)
_ANCHOR = re.compile(r"<a\b[^>]*?href\s*=\s*[\"']([^\"']*)[\"'][^>]*>(.*?)</a>", re.I | re.S)
_BLOCKQUOTE = re.compile(r"<blockquote\b[^>]*>((?:(?!<blockquote\b).)*?)</blockquote>", re.I | re.S)


def _anchor_text(m: "re.Match") -> str:
    """A link as its words: a mailto or a link whose text is the address shows once; a web link keeps its address after the words."""
    href, label = m.group(1).strip(), re.sub(r"<[^>]+>", "", m.group(2)).strip()
    if href.lower().startswith("mailto:"):
        return label or href[7:]
    if not label or label.rstrip("/") == href.rstrip("/"):
        return href
    return f"{label} ({href})" if href.lower().startswith("http") else label


def _unquote_depth(line: str) -> str:
    return re.sub(r"^(\s*>)+ ?", "", line)


def tidy_quotes(text: str, mode: str) -> str:
    """Handle the '>' quoting of a plain-text reply. omit: a quoted block with nothing of the sender's after it (the usual reply) becomes one
    line naming who it quoted; a quote with the sender's own text after it (answers written between the quoted lines) is kept, since the
    answers need their questions. clean: every quote is kept without the '>' markers, set off by indentation. keep: left as written."""
    mode = (mode or "omit").strip().lower()
    if mode == "keep":
        return text
    lines = text.split("\n")
    blocks, i = [], 0  # (start, end) of each run of quoted lines (blank lines inside a run stay in it)
    while i < len(lines):
        if _QUOTE_LINE.match(lines[i]):
            j = i
            while j + 1 < len(lines) and (_QUOTE_LINE.match(lines[j + 1]) or (not lines[j + 1].strip() and j + 2 < len(lines) and _QUOTE_LINE.match(lines[j + 2]))):
                j += 1
            blocks.append((i, j))
            i = j + 1
        else:
            i += 1
    # The sender answered between the quoted lines when real text (not just a "... wrote:" line) follows the first quote.
    interleaved = bool(blocks) and any(
        l.strip() and not _QUOTE_LINE.match(l) and not _ATTRIBUTION.search(l) for l in lines[blocks[0][1] + 1:]
    )
    out, pos = [], 0
    for a, b in blocks:
        out.extend(lines[pos:a])
        gap = bool(out) and not out[-1].strip()
        while out and not out[-1].strip():
            out.pop()
        attribution = out.pop().strip() if out and len(out[-1]) < 250 and _ATTRIBUTION.search(out[-1]) else ""
        if gap and out:
            out.append("")
        body = [_unquote_depth(l).rstrip() for l in lines[a:b + 1]]
        if mode == "omit" and not interleaved:
            count = sum(1 for l in body if l.strip())
            who = f" from {attribution.rstrip(':')}" if attribution else ""
            out.append(f"[Quoted earlier message omitted{who}: {count} line{'s' if count != 1 else ''}]")
        else:
            out.append(f"[{attribution}]" if attribution else "[Quoted text:]")
            out.extend("    " + l if l.strip() else "" for l in body)
            out.append("[End of quoted text]")
        pos = b + 1
    out.extend(lines[pos:])
    return "\n".join(out)


def clean_email_text(body: str, is_html: bool, quoted: str = "omit") -> str:
    """An email body as plain text for a model: no markup or HTML entities (Nextcloud also sends plain-text mail HTML-escaped, with links as
    <a> tags), links reduced to their words, and reply quoting handled as `quoted` says (see tidy_quotes)."""
    text = body or ""
    text = re.sub(r"<(script|style)\b.*?</\1>", "", text, flags=re.S | re.I)
    text = _ANCHOR.sub(_anchor_text, text)
    if is_html:
        while True:  # innermost first, so nesting becomes '>>'
            text, n = _BLOCKQUOTE.subn(lambda m: "\n" + "\n".join("> " + l for l in re.sub(r"<br\s*/?>|</p>|</div>", "\n", m.group(1), flags=re.I).strip().split("\n")) + "\n", text)
            if not n:
                break
        text = re.sub(r"<br\s*/?>|<p\b[^>]*>|</p>|</div>|</tr>", "\n", text, flags=re.I)
    if is_html:
        text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = text.replace("\r\n", "\n")
    text = tidy_quotes(text, quoted)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"(?<=\S)[ \t]{2,}", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


class MailError(RuntimeError):
    """A failed Nextcloud call, keeping the HTTP status so errors can be explained."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


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
        QUOTED_TEXT: str = Field(
            default="omit",
            description="What read_email does with the quoted earlier messages in a reply (lines starting with '>'): omit = replace a quote at the end of the mail with one line saying who it quoted (a quote with the sender's answers written between its lines is kept), clean = keep every quote but drop the '>' marks and indent it, keep = leave it as written.",
        )
        MARK_READ_ON_OPEN: bool = Field(
            default=True,
            description="read_email marks the email as read (like opening it in the Mail app)",
        )
        SYNC_BEFORE_LIST: bool = Field(
            default=True,
            description="Ask Nextcloud Mail to fetch new mail from the mail server before listing (the web GUI does this on open)",
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
            raise MailError(f"Could not reach Nextcloud at {url}: {e}", -1)

    def _sync_mailbox(self, mailbox_id: int) -> str:
        """Ask the Mail app to pull new mail from IMAP into its cache. Returns "" or a note on failure."""
        if not self.valves.SYNC_BEFORE_LIST:
            return ""
        url = (
            f"{self.valves.NEXTCLOUD_URL.rstrip('/')}"
            f"/index.php/apps/mail/api/mailboxes/{int(mailbox_id)}/sync"
        )
        try:
            r = self._request("POST", url, json={"ids": [], "init": True})
            if r.status_code >= 400:
                return f"[Note: could not refresh from the mail server (HTTP {r.status_code}); the list may be missing the newest mail.]\n"
        except Exception as e:
            return f"[Note: could not refresh from the mail server ({e}); the list may be missing the newest mail.]\n"
        return ""

    def _set_seen(self, message_id: int, seen: bool = True) -> None:
        """Set or clear the read flag via the Mail app's own flags route."""
        url = (
            f"{self.valves.NEXTCLOUD_URL.rstrip('/')}"
            f"/index.php/apps/mail/api/messages/{int(message_id)}/flags"
        )
        r = self._request("PUT", url, json={"flags": {"seen": bool(seen)}})
        if r.status_code >= 400:
            raise MailError(
                f"HTTP {r.status_code} setting read flag: {r.text[:200]}", r.status_code
            )

    def _ocs(self, method: str, path: str, params=None, json=None):
        url = f"{self.valves.NEXTCLOUD_URL.rstrip('/')}/ocs/v2.php/apps/mail{path}"
        r = self._request(method, url, params, json)
        try:
            payload = r.json()["ocs"]
        except Exception:
            raise MailError(
                f"HTTP {r.status_code} from {url}: {r.text[:300]}", r.status_code
            )
        if r.status_code >= 400:
            raise MailError(
                f"HTTP {r.status_code}: {payload.get('data') or payload.get('meta')}",
                r.status_code,
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

    @staticmethod
    def _fail(e: Exception) -> str:
        """Turn an exception into an error message that tells the model what to do next."""
        text = f"Error: {e}"
        status = getattr(e, "status", 0)
        if isinstance(e, ValueError) and "invalid literal" in str(e):
            hint = (
                "message_id must be a plain number, copied exactly from the 'id' shown "
                "by list_emails."
            )
        elif status in (400, 404):
            hint = (
                "Nextcloud did not accept this request. If you passed a message_id, "
                "that id does not exist in this mailbox account: do not guess ids (1, 2, 3...). "
                "Call list_emails and use an 'id' it printed, exactly as shown. If you passed a "
                "mailbox name, call list_mailboxes for the exact names."
            )
        elif status in (401, 403):
            hint = (
                "Nextcloud rejected the login or permission. This is a configuration problem "
                "(user name or app password in the tool's valves). Tell the user; retrying will not help."
            )
        elif status >= 500:
            hint = "The Nextcloud server had an internal error. Wait a moment and retry once, then tell the user."
        elif status == -1:
            hint = "The Nextcloud server could not be reached. Retrying once is fine; if it fails again, tell the user."
        else:
            return text
        return f"{text}\nWhat to do: {hint}"

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
        text = clean_email_text(body, is_html, self.valves.QUOTED_TEXT)
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
            return self._fail(e)

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
            mailbox_id = self._mailbox_id(mailbox)
            note = self._sync_mailbox(mailbox_id)
            msgs = self._ocs(
                "GET", f"/ocs/mailboxes/{mailbox_id}/messages", params=params
            )
            if not msgs:
                return note + "No emails found."
            lines = [note.rstrip()] if note else []
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
            return self._fail(e)

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
            return self._fail(e)

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
            return self._fail(e)

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
            return self._fail(e)

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
            from_email = next(
                (a["email"] for a in accounts if a["id"] == account_id), None
            )
            if not from_email:
                return (
                    f"Error: mail account id {account_id} was not found. "
                    "What to do: tell the user to check the ACCOUNT_ID valve."
                )

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
            return self._fail(e)
