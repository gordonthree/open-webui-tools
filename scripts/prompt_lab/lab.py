"""Prompt lab for Mara's memory entries: a tool-free copy of the medium-mara preset, driven through Open WebUI's API."""
import json, re, sqlite3, sys, time
from pathlib import Path
import requests

sys.path.insert(0, "/home/gordon/dev/open-webui-tools/scripts")
from push_tool import DEFAULT_SECRETS_FILE, parse_secrets, server_connection, configured_servers  # noqa

COMPANION = Path.home() / "docker/open-webui/companion-docs/autonomy/stories"
WEBUI_DB = Path.home() / "docker/open-webui/data/webui.db"
NOTES_DB = Path.home() / "docker/open-webui/data/agent_notes.sqlite3"
LAB_ID = "zz-memory-lab"


def conn():
    secrets = parse_secrets(Path(DEFAULT_SECRETS_FILE))
    servers = configured_servers(secrets)
    url, key = server_connection(secrets, "local")
    return url.rstrip("/"), key


def H(key):
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


SOURCE = ["medium-mara"]  # which preset's base model and parameters the lab copies


def preset(model_id=None):
    c = sqlite3.connect(f"file:{WEBUI_DB}?mode=ro", uri=True)
    _id, base, params, meta = c.execute("select id, base_model_id, params, meta from model where id=?", (model_id or SOURCE[0],)).fetchone()
    return base, json.loads(params), json.loads(meta)


def persona_system(extra_note=""):
    """Her real operating manual, without the session start protocol (a tool-free test has nothing to read)."""
    base, params, meta = preset("medium-mara")
    system = params["system"]
    cut = system.index("# You are MARA VOSS")
    body = system[cut:]
    return body + "\n\n" + extra_note


def persona_context(max_chars=9000):
    """Her core persona notes as plain text, read straight from the database (no read counters are touched)."""
    c = sqlite3.connect(f"file:{NOTES_DB}?mode=ro", uri=True)
    out = []
    for pk, name in c.execute("select n.note_pk, n.note_name from note_id n join note_tag nt using(note_pk) join tag t using(tag_id) where t.tag_name='mara-core' order by n.note_pk"):
        rows = c.execute("select note_text from note_data where note_pk=? order by abs(weight-50) desc, entry_no limit 6", (pk,)).fetchall()
        out.append(f"## {name}\n" + "\n".join("- " + r[0] for r in rows))
    text = "\n\n".join(out)
    return text[:max_chars]


def create_model():
    url, key = conn()
    base, params, meta = preset()
    p = {k: v for k, v in params.items() if k not in ("system", "compact_token_threshold")}
    body = {"id": LAB_ID, "base_model_id": base, "name": "zz memory lab (temporary)", "meta": {"description": "temporary prompt lab, no tools"}, "params": p, "is_active": True}
    r = requests.post(f"{url}/api/v1/models/create", headers=H(key), json=body, timeout=30)
    if r.status_code == 400 and "already" in r.text.lower():
        requests.post(f"{url}/api/v1/models/model/delete", headers=H(key), json={"id": LAB_ID}, timeout=30)
        r = requests.post(f"{url}/api/v1/models/create", headers=H(key), json=body, timeout=30)
    r.raise_for_status()
    return r.json().get("id")


def delete_model():
    url, key = conn()
    r = requests.post(f"{url}/api/v1/models/model/delete", headers=H(key), json={"id": LAB_ID}, timeout=30)
    return r.status_code


def ask(messages, timeout=1500, poll=4):
    """One turn on the tool-free preset, run the way scripts/rating_pass.py runs a turn (a throwaway backing chat, polled until done)."""
    import uuid
    url, key = conn()
    t = time.time()
    user_id, asst_id, now = str(uuid.uuid4()), str(uuid.uuid4()), int(time.time())
    umsg = {"id": user_id, "parentId": None, "childrenIds": [asst_id], "role": "user", "content": "lab", "timestamp": now, "models": [LAB_ID]}
    amsg = {"id": asst_id, "parentId": user_id, "childrenIds": [], "role": "assistant", "content": "", "model": LAB_ID, "modelName": LAB_ID, "timestamp": now}
    chat = {"title": "[prompt lab] zz", "models": [LAB_ID], "timestamp": now * 1000,
            "history": {"messages": {user_id: umsg, asst_id: amsg}, "currentId": asst_id}, "messages": [umsg, amsg]}
    r = requests.post(f"{url}/api/v1/chats/new", headers=H(key), json={"chat": chat}, timeout=30); r.raise_for_status()
    chat_id = r.json()["id"]
    try:
        body = {"model": LAB_ID, "stream": True, "chat_id": chat_id, "id": asst_id, "session_id": str(uuid.uuid4()), "messages": messages}
        r = requests.post(f"{url}/api/chat/completions", headers=H(key), json=body, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"start failed {r.status_code}: {r.text[:300]}")
        while time.time() - t < timeout:
            time.sleep(poll)
            g = requests.get(f"{url}/api/v1/chats/{chat_id}", headers=H(key), timeout=30); g.raise_for_status()
            m = (((g.json().get("chat") or {}).get("history") or {}).get("messages") or {}).get(asst_id) or {}
            if m.get("done"):
                raw = m.get("content") or ""
                thinking = "\n\n".join(re.findall(r"<details\b.*?</details>", raw, flags=re.S | re.I) + re.findall(r"<think>.*?</think>", raw, flags=re.S))
                for o in m.get("output") or []:  # newer Open WebUI keeps the reasoning as output items, not in the content
                    if o.get("type") == "reasoning":
                        parts = o.get("content") or o.get("reasoning_details") or []
                        thinking += "\n\n" + (parts if isinstance(parts, str) else "".join(p.get("text", "") for p in parts if isinstance(p, dict)))
                text = re.sub(r"<details\b.*?</details>", "", raw, flags=re.S | re.I)
                text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
                return {"text": text, "thinking": thinking, "usage": m.get("usage"), "seconds": round(time.time() - t, 1), "error": m.get("error")}
        raise TimeoutError("turn did not finish")
    finally:
        try:
            requests.delete(f"{url}/api/v1/chats/{chat_id}", headers=H(key), timeout=20)
        except requests.RequestException:
            pass
