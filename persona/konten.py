"""Konten: Login, Sitzung, Passwort, Rollen, Nutzerverwaltung, Einwilligung und Leads.

Aus platform_app.py herausgelöst am 31. Oktober 2026 (Bestandsaufnahme Punkt 8), Code unverändert.
"""

import json
import re
import threading
import urllib.parse
import urllib.request
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel
import auth
from .kern import HIER, _t, app, require_user, user_avatars

# ------------------------------- Auth -------------------------------
# Login-Haertung: max. 5 Fehlversuche je IP bzw. je Nutzername in 15 Min,
# 1 s Bremse pro Fehlversuch (Brute-Force), Secure-Cookie hinter HTTPS.
_login_fails: dict[str, list[float]] = {}

_LOGIN_WINDOW = 900.0

_LOGIN_MAX = 5

def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd
            else (request.client.host if request.client else "?"))

@app.middleware("http")
async def _security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp

@app.post("/api/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    import time
    now = time.time()
    keys = (f"ip:{_client_ip(request)}", f"u:{username.lower().strip()[:40]}")
    with _lock:
        for k in keys:
            _login_fails[k] = [t for t in _login_fails.get(k, []) if now - t < _LOGIN_WINDOW]
        if any(len(_login_fails[k]) >= _LOGIN_MAX for k in keys):
            raise HTTPException(429, "Too many attempts — please wait 15 minutes and try again.")
    u = auth.verify(username, password)
    if not u:
        time.sleep(1.0)
        with _lock:
            for k in keys:
                _login_fails.setdefault(k, []).append(now)
        raise HTTPException(401, "Wrong username or password.")
    with _lock:
        for k in keys:
            _login_fails.pop(k, None)
    resp = JSONResponse({"ok": True, "username": u["username"]})
    resp.set_cookie("session", auth.make_token(u["id"]), httponly=True,
                    samesite="lax", max_age=30 * 86400,
                    secure=(request.headers.get("x-forwarded-proto") == "https"))
    return resp

@app.post("/api/password")
def change_password(request: Request, current: str = Form(...), new: str = Form(...)):
    u = require_user(request)
    if not auth.verify(u["username"], current):
        import time
        time.sleep(1.0)
        raise HTTPException(401, "Current password is wrong.")
    if len(new) < 8:
        raise HTTPException(400, "New password must be at least 8 characters.")
    auth.set_password(u["username"], new)
    return {"ok": True}

@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("session")
    return resp

@app.get("/api/me")
def me(request: Request):
    u = require_user(request)
    return {"username": u["username"], "role": u["role"], "avatars": user_avatars(u["id"])}

def _is_editor(u: dict) -> bool:
    """Redaktionsrechte auf der Website: Admin oder Rolle 'editor' (fremde Beitraege/Profile bearbeiten)."""
    return u.get("role") in ("admin", "editor")

def _require_editor(request: Request) -> dict:
    """Redaktion oder Admin (die Kette veroeffentlicht mit einem Redaktionskonto)."""
    u = require_user(request)
    if not _is_editor(u):
        raise HTTPException(403, "Editors only.")
    return u

def require_admin(request: Request):
    u = require_user(request)
    if u.get("role") != "admin":
        raise HTTPException(403, "Admin only.")
    return u

# ------------------------------ Leads (Landingpage) ------------------------------
# Oeffentliches Formular "Kostenlose Probe / Pilot / Warteliste". Schutz: Honeypot-Feld,
# Pflicht-Einwilligung, Laengen-Limits, max. 5 Anfragen je IP pro Stunde.
_lead_hits: dict[str, list[float]] = {}

_LEAD_WINDOW = 3600.0

_LEAD_MAX = 5

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")

_LEAD_ROLES = {"berater", "podcast", "kommunikation", "politik", "agentur", "sonstiges"}

_LEAD_INTERESTS = {"probe", "pilot", "warteliste"}

class LeadReq(BaseModel):
    name: str
    email: str
    role: str = ""
    interest: str = "probe"
    message: str = ""
    consent: bool = False
    website: str = ""   # Honeypot: Menschen lassen es leer

@app.post("/api/lead")
def lead_create(req: LeadReq, request: Request):
    import time as _t
    if req.website.strip():                      # Bot -> still "ok", nichts speichern
        return {"ok": True}
    name = req.name.strip()
    email = req.email.strip().lower()
    if not (2 <= len(name) <= 120):
        raise HTTPException(400, "Bitte einen Namen angeben.")
    if len(email) > 200 or not _EMAIL_RE.match(email):
        raise HTTPException(400, "Bitte eine gültige E-Mail-Adresse angeben.")
    if not req.consent:
        raise HTTPException(400, "Bitte der Datenschutzerklärung zustimmen.")
    role = req.role.strip().lower()
    interest = req.interest.strip().lower()
    if role and role not in _LEAD_ROLES:
        role = "sonstiges"
    if interest not in _LEAD_INTERESTS:
        interest = "probe"
    msg = req.message.strip()[:2000]
    ip = _client_ip(request)
    now = _t.time()
    with _lock:
        hits = [t for t in _lead_hits.get(ip, []) if now - t < _LEAD_WINDOW]
        if len(hits) >= _LEAD_MAX:
            raise HTTPException(429, "Zu viele Anfragen. Bitte später erneut versuchen.")
        hits.append(now)
        _lead_hits[ip] = hits
    lid = auth.add_lead(name, email, role, interest, msg, ip)
    print(f"[lead] #{lid} {interest} {name} <{email}> {role}", flush=True)
    threading.Thread(target=notify_lead, args=(lid, name, email, role, interest, msg),
                     daemon=True).start()
    return {"ok": True, "id": lid}

# Benachrichtigung bei neuem Lead: Push via ntfy.sh (Topic aus notify.json, im Deploy enthalten).
# Spaeter ergaenzbar um E-Mail (SMTP/Resend), gleiche Stelle.
NOTIFY_FILE = HIER / "notify.json"

def _notify_cfg() -> dict:
    try:
        return json.loads(NOTIFY_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}

def _ntfy(title: str, body: str, tags: str = "bell", tag: str = "ntfy") -> bool:
    cfg = _notify_cfg()
    topic = (cfg.get("ntfy_topic") or "").strip()
    if not topic:
        return False
    server = (cfg.get("ntfy_server") or "https://ntfy.sh").rstrip("/")
    import urllib.request
    req = urllib.request.Request(
        f"{server}/{topic}", data=body.encode("utf-8"), method="POST",
        headers={"Title": title.encode("utf-8").decode("latin-1", "ignore"),
                 "Priority": "high", "Tags": tags,
                 "Content-Type": "text/plain; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            ok = 200 <= r.status < 300
    except Exception as e:  # noqa: BLE001
        print(f"[{tag}] ntfy failed: {e}", flush=True)
        return False
    print(f"[{tag}] ntfy sent -> {topic}", flush=True)
    return ok

def notify_lead(lid: int, name: str, email: str, role: str, interest: str, msg: str) -> bool:
    label = {"probe": "Kostenlose Probe", "pilot": "Pilot", "warteliste": "Warteliste"}.get(interest, interest)
    body = f"{name} <{email}>\n{role or '-'} · {label}"
    if msg:
        body += f"\n\n{msg[:400]}"
    return _ntfy(f"Persona Lead #{lid}: {label}", body, tags="star,email", tag="lead")

# ------------------------------ Einwilligung (Consent) ------------------------------
# Oeffentliche Einwilligungserklaerung der ABGEBILDETEN Person (Gesicht + Stimme), DE/EN.
# Speichert den Nachweis (wer, wann, welche Textversion, welche Haekchen, IP) in SQLite.
CONSENT_VERSION = "2026-08-21.1"

_CONSENT_REQUIRED = ("identity", "purpose", "processors", "label", "withdraw", "notpolitical")

_consent_hits: dict[str, list[float]] = {}

class ConsentReq(BaseModel):
    name: str
    email: str
    lang: str = "de"
    client: str = ""          # Auftraggeber/Organisation, fuer die Videos erstellt werden (optional)
    scope: str = "self"       # self | client | persona
    checks: dict = {}
    text_hash: str = ""       # SHA-256 des angezeigten Erklaerungstexts (vom Browser berechnet)
    website: str = ""         # Honeypot

@app.post("/api/consent")
def consent_create(req: ConsentReq, request: Request):
    import time as _t
    if req.website.strip():
        return {"ok": True}
    name = req.name.strip()
    email = req.email.strip().lower()
    if not (2 <= len(name) <= 120):
        raise HTTPException(400, "Bitte den vollständigen Namen angeben. / Please enter your full name.")
    if len(email) > 200 or not _EMAIL_RE.match(email):
        raise HTTPException(400, "Bitte eine gültige E-Mail-Adresse angeben. / Please enter a valid email.")
    checks = {k: bool(v) for k, v in (req.checks or {}).items()}
    missing = [k for k in _CONSENT_REQUIRED if not checks.get(k)]
    if missing:
        raise HTTPException(400, "Bitte alle Pflichtfelder bestätigen. / Please confirm all required items.")
    lang = req.lang if req.lang in ("de", "en") else "de"
    scope = req.scope if req.scope in ("self", "client", "persona") else "self"
    ip = _client_ip(request)
    now = _t.time()
    with _lock:
        hits = [t for t in _consent_hits.get(ip, []) if now - t < _LEAD_WINDOW]
        if len(hits) >= 10:
            raise HTTPException(429, "Zu viele Anfragen. Bitte später erneut versuchen.")
        hits.append(now)
        _consent_hits[ip] = hits
    cid = auth.add_consent(name, email, lang, req.client[:200], scope, checks, CONSENT_VERSION,
                           req.text_hash[:64], ip, request.headers.get("user-agent", ""))
    print(f"[consent] #{cid} {name} <{email}> scope={scope} client={req.client[:60]!r}", flush=True)
    threading.Thread(target=_ntfy, args=(f"Persona Einwilligung #{cid}",
                                         f"{name} <{email}>\nUmfang: {scope} · Auftraggeber: {req.client[:80] or '-'}",
                                         "white_check_mark", "consent"), daemon=True).start()
    return {"ok": True, "id": cid, "version": CONSENT_VERSION}

@app.get("/api/admin/consents")
def admin_consents(request: Request):
    require_admin(request)
    return {"consents": auth.list_consents()}

@app.get("/api/admin/leads")
def admin_leads(request: Request):
    require_admin(request)
    return {"leads": auth.list_leads()}


# Späte Bindung: diese Namen werden nur in Funktionskörpern gebraucht. Die Module dürfen sich
# gegenseitig nutzen; der Import steht am Ende, damit beim Laden keine Kreisabhängigkeit entsteht.
# (Reihenfolge des Ladens: persona/__init__.py)
from .render import _lock  # noqa: E402
