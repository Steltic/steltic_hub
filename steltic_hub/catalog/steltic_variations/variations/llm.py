"""One OpenAI-compatible chat call, with the connection the hub pushes to /api/creds.

MOCK (or no connection) raises NotConfigured; every caller has an offline path.
"""
from __future__ import annotations
import json, re
import httpx

_CREDS: dict | None = None


class NotConfigured(RuntimeError):
    pass


def set_creds(d: dict):
    global _CREDS
    _CREDS = dict(d or {})


def creds() -> dict | None:
    return _CREDS


def available() -> bool:
    return bool(_CREDS and _CREDS.get("model") and _CREDS.get("model") != "MOCK" and _CREDS.get("base_url"))


def _headers() -> dict:
    h = {"Content-Type": "application/json", "Authorization": f"Bearer {_CREDS.get('api_key', '')}"}
    if "openrouter" in (_CREDS.get("base_url") or ""):
        h["HTTP-Referer"] = "https://github.com/Steltic/steltic-hub"; h["X-Title"] = "Steltic design variations"
    return h


def chat(system: str, user: str, max_tokens: int = 8000, temperature: float = 0.2, json_mode: bool = True) -> str:
    if not available():
        raise NotConfigured("no LLM connection (model MOCK or none set)")
    body = {"model": _CREDS["model"], "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens, "temperature": temperature}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    prov = (_CREDS.get("provider") or "").strip()
    if prov and "openrouter" in (_CREDS.get("base_url") or ""):
        body["provider"] = {"order": [prov], "allow_fallbacks": False}
    url = _CREDS["base_url"].rstrip("/") + "/chat/completions"
    with httpx.Client(timeout=httpx.Timeout(300.0, connect=20.0)) as cx:
        r = cx.post(url, headers=_headers(), json=body)
        if r.status_code == 400 and json_mode:                 # a provider that refuses response_format
            body.pop("response_format", None)
            r = cx.post(url, headers=_headers(), json=body)
        r.raise_for_status()
        d = r.json()
    return ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


def chat_json(system: str, user: str, **kw) -> dict:
    txt = chat(system, user, **kw)
    return parse_json(txt)


def parse_json(txt: str) -> dict:
    """The model's reply as a dict: bare JSON, or JSON inside a code fence / prose."""
    t = (txt or "").strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(t[i:j + 1])
        except Exception:
            pass
    raise ValueError("the model did not return JSON")
