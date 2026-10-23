"""Research browser service: a few safe browser actions for an LLM-driven tool, on top of a Chromium it attaches to over CDP.

The Chromium (container `research-browser`) is the one Gordon can also use by hand in a web desktop, so cookies he sets and
"are you human" checks he passes carry over to what the agents see. This service holds no LLM and no credentials of its own:
it opens pages, returns readable text plus a numbered list of things that can be clicked or typed into, and does what it is told
with a ref number. The driver (a model in an Open WebUI tool) decides what to do. Every action is written to an audit log.

Guardrails (defence in depth; the egress proxy in front of the browser is the hard network limit):
  - http/https only, no credentials in URLs, no private/loopback/link-local hosts, no bare IPs that look internal;
  - READ_ONLY (default on): form submissions that navigate with POST/PUT/DELETE are aborted, XHR/fetch is left alone so pages work;
  - a cap on actions per session and on open tabs, idle tabs are closed, page text and element lists are size-capped;
  - one action at a time per session.
"""
import asyncio
import ipaddress
import json
import os
import re
import socket
import time
import urllib.parse
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright
from pydantic import BaseModel

CDP_URL = os.environ.get("CDP_URL", "http://research-browser:9223")
API_KEY = os.environ.get("RESEARCH_API_KEY", "")
READ_ONLY = os.environ.get("READ_ONLY", "1") != "0"
MAX_TABS = int(os.environ.get("MAX_TABS", "6"))
MAX_ACTIONS = int(os.environ.get("MAX_ACTIONS_PER_SESSION", "60"))
IDLE_SECONDS = int(os.environ.get("IDLE_SECONDS", "600"))
NAV_TIMEOUT_MS = int(os.environ.get("NAV_TIMEOUT_MS", "25000"))
TEXT_CHARS = int(os.environ.get("TEXT_CHARS", "3500"))      # page text returned per read
MAX_ELEMENTS = int(os.environ.get("MAX_ELEMENTS", "40"))
AUDIT_LOG = Path(os.environ.get("AUDIT_LOG", "/data/audit.jsonl"))


# ---------------------------------------------------------------- pure helpers (unit-tested)

class Refused(Exception):
    """An action the service will not perform; the message is written for the calling model."""


def check_url(url):
    """Return a clean URL or raise Refused."""
    url = (url or "").strip()
    if not url:
        raise Refused("No URL given.")
    if "://" not in url:
        url = "https://" + url
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https"):
        raise Refused(f"Only http and https pages can be opened, not {p.scheme}:")
    if p.username or p.password:
        raise Refused("Web addresses containing a user name or password are not allowed.")
    host = (p.hostname or "").lower()
    if not host or host == "localhost" or host.endswith((".local", ".internal", ".lan", ".localhost", ".home.arpa")) or "." not in host and ":" not in host:
        raise Refused("That address is not a public website.")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified
               or ip in ipaddress.ip_network("100.64.0.0/10")):
        raise Refused("That address is not a public website.")
    if p.port not in (None, 80, 443):
        raise Refused("Only the standard web ports (80 and 443) are allowed.")
    return urllib.parse.urlunsplit(p)


def clip(text, start, size):
    """Slice `text` for paging: returns (piece, next_start or None). Cuts at a paragraph or space where it can."""
    text = text or ""
    start = max(0, int(start or 0))
    piece = text[start:start + size]
    end = start + len(piece)
    if end < len(text):
        cut = max(piece.rfind("\n\n"), piece.rfind("\n"), piece.rfind(". "), piece.rfind(" "))
        if cut > size * 0.6:
            piece, end = piece[:cut + 1], start + cut + 1
        return piece.strip(), end
    return piece.strip(), None


def tidy(text):
    text = re.sub(r"[ \t ]+", " ", text or "")
    text = re.sub(r"\n[ \t]+", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def show_href(href, base):
    """A link target short enough to read: same-site links keep only the path."""
    try:
        u = urllib.parse.urlsplit(urllib.parse.urljoin(base, href))
        if u.netloc.endswith("duckduckgo.com") and u.path.startswith("/l/"):      # a search result: show where it really goes
            target = urllib.parse.parse_qs(u.query).get("uddg", [""])[0]
            if target:
                u = urllib.parse.urlsplit(target)
        b = urllib.parse.urlsplit(base)
        if u.scheme not in ("http", "https"):
            return ""
        shown = (u.path or "/") + (("?" + u.query) if u.query else "") if u.netloc == b.netloc else u.netloc + u.path
        return shown[:200] + ("..." if len(shown) > 200 else "")
    except ValueError:
        return ""


STATUS_WORDS = {401: "it needs a login", 403: "access was denied (the site may take this browser for a robot)", 404: "the page was not found",
                429: "the site is limiting how fast it can be used (too many requests); wait a while or use a different source",
                451: "it is not available for legal reasons"}


def status_problem(status, text_chars):
    """A sentence for the calling model when the site refused or failed, or None when the page is usable."""
    if not status or status < 400:
        return None
    if status in (401, 403, 429) or text_chars < 200:
        why = STATUS_WORDS.get(status) or ("the site has a problem" if status >= 500 else "the site refused the request")
        return f"The site answered HTTP {status}: {why}."
    return None


def format_elements(elements, base):
    lines = []
    for e in elements:
        label = re.sub(r"\s+", " ", e.get("label") or "").strip()[:80]
        kind = e.get("kind", "")
        extra = show_href(e["href"], base) if kind == "link" and e.get("href") else (e.get("type") or "")
        lines.append(f'[{e["ref"]}] {kind} "{label}"' + (f" -> {extra}" if extra else ""))
    return lines


# ---------------------------------------------------------------- page script

EXTRACT_JS = r"""(max) => {
  const visible = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  document.querySelectorAll('[data-rref]').forEach(e => e.removeAttribute('data-rref'));
  const chromeSel = 'nav, header, footer, aside, [role=navigation], [role=banner], [role=contentinfo], [role=complementary], ' +
    '[role=search], [aria-hidden=true], script, style, noscript, .navbox, .sidebar';
  const root = document.querySelector('main, article, [role=main]') || document.body;
  const inChrome = e => !!e.closest(chromeSel);

  // readable text without menus, sidebars and footers: hide them for the instant of reading, then put the page back exactly
  const hidden = [...root.querySelectorAll(chromeSel + ', select')].map(e => [e, e.getAttribute('style')]);
  hidden.forEach(([e]) => e.style.setProperty('display', 'none', 'important'));
  const text = root.innerText || '';
  hidden.forEach(([e, old]) => old === null ? e.removeAttribute('style') : e.setAttribute('style', old));

  const label = e => (e.getAttribute('aria-label') || e.innerText || e.getAttribute('title') || e.getAttribute('placeholder')
    || e.getAttribute('alt') || e.value || e.getAttribute('name') || '').replace(/\s+/g, ' ').trim();
  const all = [...document.querySelectorAll('a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button], [role=link]')]
    .filter(visible);
  const isField = e => ['input', 'textarea', 'select'].includes(e.tagName.toLowerCase());
  const isMenu = e => e.tagName.toLowerCase() === 'select';
  const fieldOk = e => isField(e) && !isMenu(e) && !['checkbox', 'radio', 'password', 'submit', 'button', 'image', 'file', 'reset', 'range', 'color'].includes((e.type || '').toLowerCase());
  const wanted = /^(search|next|previous|older|newer|more|show more|accept|agree|continue|allow|got it)\b|^page \d/i;
  const searchy = e => /search/i.test((e.type || '') + (e.getAttribute('name') || '') + (e.getAttribute('aria-label') || '') + (e.getAttribute('placeholder') || '') + (e.id || ''));

  const fields = all.filter(fieldOk).sort((a, b) => (searchy(b) ? 1 : 0) - (searchy(a) ? 1 : 0)).slice(0, 6);
  const rest = all.filter(e => !isField(e) && !isMenu(e) && label(e) && (!inChrome(e) || wanted.test(label(e))));
  const body = e => root.contains(e) && (!!e.closest('p, li, td, figcaption, h1, h2, h3, h4, blockquote') || label(e).length >= 30);
  const useful = rest.filter(e => inChrome(e) && wanted.test(label(e))).slice(0, 4);   // search, next page, accept cookies...
  const ranked = [...useful, ...rest.filter(e => !useful.includes(e) && body(e)),
                  ...rest.filter(e => !useful.includes(e) && root.contains(e) && !body(e)), ...rest.filter(e => !useful.includes(e) && !root.contains(e))];
  const seen = new Set(); const chosen = [...fields];
  for (const e of ranked) { const k = (e.href || '') + '|' + label(e); if (seen.has(k)) continue; seen.add(k); chosen.push(e); }
  const out = chosen.slice(0, max).map((e, i) => {
    e.setAttribute('data-rref', String(i + 1));
    const tag = e.tagName.toLowerCase();
    const kind = tag === 'a' ? 'link' : isField(e) ? 'field' : 'button';
    return {ref: i + 1, kind, label: label(e), href: tag === 'a' ? e.href : '', type: isField(e) ? (tag === 'input' ? (e.type || 'text') : tag) : ''};
  });
  return {title: document.title, url: location.href, text, elements: out};
}"""


# ---------------------------------------------------------------- browser + sessions

class Browser:
    def __init__(self):
        self.pw = None
        self.browser = None
        self.sessions = {}      # (user, session) -> dict(page, last, actions, lock)
        self.lock = asyncio.Lock()

    async def context(self):
        async with self.lock:
            if self.browser is None or not self.browser.is_connected():
                if self.pw is None:
                    self.pw = await async_playwright().start()
                host = urllib.parse.urlsplit(CDP_URL).hostname
                ip = socket.gethostbyname(host)     # Chromium refuses a CDP request whose Host header is a name
                port = urllib.parse.urlsplit(CDP_URL).port
                self.browser = await self.pw.chromium.connect_over_cdp(f"http://{ip}:{port}")
            return self.browser.contexts[0]         # the persistent profile Gordon also uses by hand

    async def session(self, user, name, create=True):
        key = (user or "anonymous", name or "default")
        await self.reap()
        s = self.sessions.get(key)
        if s and s["page"].is_closed():
            self.sessions.pop(key, None)
            s = None
        if s is None:
            if not create:
                raise Refused("No page is open in this session. Use open first.")
            if len(self.sessions) >= MAX_TABS:
                raise Refused("Too many pages are open; close one first.")
            ctx = await self.context()
            page = await ctx.new_page()
            page.set_default_timeout(NAV_TIMEOUT_MS)
            if READ_ONLY:
                async def guard(route):
                    r = route.request
                    if r.method not in ("GET", "HEAD", "OPTIONS") and r.resource_type == "document":
                        return await route.abort()
                    await route.continue_()
                await page.route("**/*", guard)
            s = self.sessions[key] = {"page": page, "last": time.time(), "actions": 0, "lock": asyncio.Lock()}
        s["last"] = time.time()
        return s

    async def reap(self):
        now = time.time()
        for key, s in list(self.sessions.items()):
            if now - s["last"] > IDLE_SECONDS or s["page"].is_closed():
                self.sessions.pop(key, None)
                try:
                    await s["page"].close()
                except PlaywrightError:
                    pass

    async def close(self, user, name):
        s = self.sessions.pop((user or "anonymous", name or "default"), None)
        if s:
            try:
                await s["page"].close()
            except PlaywrightError:
                pass
        return bool(s)


B = Browser()
app = FastAPI(title="research-browser service")


def audit(user, session, action, **fields):
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "user": user, "session": session, "action": action, **fields}) + "\n")
    except OSError:
        pass


async def auth(authorization: str = Header(default=""), x_user_id: str = Header(default="anonymous")):
    if not API_KEY or authorization != f"Bearer {API_KEY}":
        raise HTTPException(401, "Bad or missing API key.")
    return re.sub(r"[^A-Za-z0-9_.-]", "", x_user_id)[:64] or "anonymous"


class Req(BaseModel):
    session: str = "default"
    url: str = ""
    ref: int = 0
    text: str = ""
    submit: bool = False
    start: int = 0
    elements: int = MAX_ELEMENTS


async def snapshot(page, start=0, elements=MAX_ELEMENTS):
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
        await page.wait_for_load_state("load", timeout=6000)
    except PlaywrightError:
        pass
    data = await page.evaluate(EXTRACT_JS, min(elements, MAX_ELEMENTS))
    if data["url"].startswith("chrome-error://"):
        return {"error": "That page could not be loaded. It may be down or blocked, or the last click tried to send a form, which this "
                         "browser does not allow. Use back to return to the previous page.", "url": data["url"]}
    text = tidy(data["text"])
    piece, nxt = clip(text, start, TEXT_CHARS)
    out = {"url": data["url"], "title": data["title"], "text": piece, "text_chars_total": len(text),
           "elements": format_elements(data["elements"], data["url"])}
    if nxt is not None:
        out["next_start"] = nxt
    if start == 0 and not piece:
        out["note"] = "The page has no readable text; it may still be loading, need a screenshot, or be blocked."
    return out


async def run(user, req, action, fn):
    try:
        s = await B.session(user, req.session, create=action == "open")
        async with s["lock"]:
            if s["actions"] >= MAX_ACTIONS:
                raise Refused("This session has used all its actions. Start a new one.")
            s["actions"] += 1
            result = await fn(s)
        audit(user, req.session, action, url=result.get("url") if isinstance(result, dict) else None, ref=req.ref or None)
        return result
    except Refused as e:
        audit(user, req.session, action, refused=str(e), url=req.url or None)
        return {"error": str(e)}
    except PlaywrightError as e:
        msg = str(e).splitlines()[0][:200]
        audit(user, req.session, action, error=msg, url=req.url or None)
        return {"error": f"The browser could not do that: {msg}"}


@app.get("/health")
async def health():
    return {"ok": True, "read_only": READ_ONLY, "sessions": len(B.sessions)}


@app.post("/open")
async def open_page(req: Req, user: str = Depends(auth)):
    async def go(s):
        url = check_url(req.url)
        resp = await s["page"].goto(url, wait_until="domcontentloaded")
        out = await snapshot(s["page"], 0, req.elements)
        out["status"] = resp.status if resp else None
        problem = status_problem(out["status"], out.get("text_chars_total", 0))
        if problem:
            return {"error": problem, "url": out.get("url"), "status": out["status"]}
        return out
    try:
        check_url(req.url)
    except Refused as e:
        audit(user, req.session, "open", refused=str(e), url=req.url)
        return {"error": str(e)}
    return await run(user, req, "open", go)


@app.post("/read")
async def read(req: Req, user: str = Depends(auth)):
    return await run(user, req, "read", lambda s: snapshot(s["page"], req.start, req.elements))


@app.post("/click")
async def click(req: Req, user: str = Depends(auth)):
    async def go(s):
        page = s["page"]
        before = set(page.context.pages)
        loc = page.locator(f'[data-rref="{int(req.ref)}"]').first
        if await loc.count() == 0:
            raise Refused("No element with that number. Read the page again and use a number from its list.")
        await loc.click(timeout=8000)
        await asyncio.sleep(0.8)
        fresh = [p for p in page.context.pages if p not in before]
        if fresh:                                       # the click opened a new tab: follow it
            s["page"] = fresh[-1]
            s["page"].set_default_timeout(NAV_TIMEOUT_MS)
            await page.close()
        return await snapshot(s["page"], 0, req.elements)
    return await run(user, req, "click", go)


@app.post("/type")
async def type_text(req: Req, user: str = Depends(auth)):
    async def go(s):
        page = s["page"]
        loc = page.locator(f'[data-rref="{int(req.ref)}"]').first
        if await loc.count() == 0:
            raise Refused("No element with that number. Read the page again and use a number from its list.")
        editable = await loc.evaluate("e => ['INPUT','TEXTAREA'].includes(e.tagName) || e.isContentEditable")
        if not editable:
            raise Refused("That item is a link or button, not a text field. Click it, then read the page again to find the field to type into.")
        await loc.fill(req.text[:500], timeout=8000)
        if req.submit:
            await loc.press("Enter")
            await asyncio.sleep(0.8)
        return await snapshot(page, 0, req.elements)
    return await run(user, req, "type", go)


@app.post("/back")
async def back(req: Req, user: str = Depends(auth)):
    async def go(s):
        await s["page"].go_back(wait_until="domcontentloaded")
        return await snapshot(s["page"], 0, req.elements)
    return await run(user, req, "back", go)


@app.post("/screenshot")
async def screenshot(req: Req, user: str = Depends(auth)):
    from fastapi.responses import Response
    try:
        s = await B.session(user, req.session, create=False)
        async with s["lock"]:
            png = await s["page"].screenshot(type="png", full_page=False)
        audit(user, req.session, "screenshot", url=s["page"].url)
        return Response(content=png, media_type="image/png")
    except Refused as e:
        return {"error": str(e)}
    except PlaywrightError as e:
        return {"error": f"The browser could not do that: {str(e).splitlines()[0][:200]}"}


@app.post("/close")
async def close(req: Req, user: str = Depends(auth)):
    audit(user, req.session, "close")
    return {"closed": await B.close(user, req.session)}
