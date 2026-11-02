"""Dolmetscher (2. November 2026): Gespräch zu zweit in zwei Sprachen, erst einmal Deutsch und Slowakisch.

Die Seite /dolmetscher erkennt die Sprache im Browser selbst (Web Speech API, Chrome oder Safari), schickt den
erkannten Satz hierher und bekommt die Übersetzung von Claude zurück. Vorgelesen wird mit einer Stimme des
Browsers; fehlt sie für die Zielsprache, liefert /api/dolmetscher/sprechen eine Stimme von ElevenLabs.

Die Schlüssel bleiben auf dem Server (Avatar Twin/avatar_keys.txt). Zugang über einen Code aus
data/dolmetscher.json (beim ersten Start erzeugt), höchstens 60 Anfragen je Minute und Adresse.
Gespräche werden nicht gespeichert; das Protokoll kennt Längen und Zeiten, keine Inhalte.
Nachfolger des Ohr-Prototyps vom 1. November (Desktop/Ohr), ohne eigenen Server.
"""

from __future__ import annotations

import json
import secrets
import threading
import time

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from .kern import DATA, HIER, STATIC, app

KEYS = HIER.parent / "Avatar Twin" / "avatar_keys.txt"
CODE_DATEI = DATA / "dolmetscher.json"
MODELL = "claude-sonnet-5-5"       # Haiku 4.5 hat am 1.11. Polnisch falsch übersetzt, Sonnet war korrekt
STIMME = "JBFqnCBsd6RMkjVDRZzb"    # neutrale mehrsprachige ElevenLabs-Stimme
KONTEXT_MAX = 4
GRENZE_JE_MINUTE = 60

SPRACHEN = {
    "de": "Deutsch", "sk": "Slowakisch", "cs": "Tschechisch", "pl": "Polnisch", "hu": "Ungarisch",
    "en": "Englisch", "it": "Italienisch", "fr": "Französisch", "es": "Spanisch", "pt": "Portugiesisch",
    "hr": "Kroatisch", "sr": "Serbisch", "ro": "Rumänisch", "uk": "Ukrainisch", "tr": "Türkisch",
}

_treffer: dict[str, list[float]] = {}
_lock = threading.Lock()
_http = httpx.Client(timeout=30)


def _schluessel(name: str) -> str:
    if KEYS.is_file():
        for line in KEYS.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(name + "=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip()
    return ""


def zugangscode() -> str:
    """Der Code aus data/dolmetscher.json; fehlt die Datei, wird ein Code erzeugt und abgelegt."""
    with _lock:
        try:
            d = json.loads(CODE_DATEI.read_text(encoding="utf-8"))
            if d.get("code"):
                return str(d["code"])
        except (OSError, ValueError):
            pass
        code = "".join(secrets.choice("23456789abcdefghkmnpqrstuvwxyz") for _ in range(8))
        CODE_DATEI.parent.mkdir(parents=True, exist_ok=True)
        CODE_DATEI.write_text(json.dumps({"code": code, "seit": time.strftime("%Y-%m-%d")}, indent=1), encoding="utf-8")
        return code


def _pruefe(request: Request, code: str | None) -> None:
    if (code or "").strip() != zugangscode():
        raise HTTPException(401, "Zugangscode stimmt nicht.")
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0].strip()
    now = time.time()
    with _lock:
        hits = [t for t in _treffer.get(ip, []) if now - t < 60]
        if len(hits) >= GRENZE_JE_MINUTE:
            raise HTTPException(429, "Zu viele Anfragen, bitte kurz warten.")
        hits.append(now)
        _treffer[ip] = hits


class UebersetzenReq(BaseModel):
    text: str
    von: str = "de"
    nach: str = "sk"
    code: str = ""
    kontext: list[dict] = []   # [{"von": "de", "text": "..."}], die letzten Sätze beider Seiten


class SprechenReq(BaseModel):
    text: str
    sprache: str = "sk"
    code: str = ""


def uebersetzen(text: str, von: str, nach: str, kontext: list[dict]) -> str:
    key = _schluessel("ANTHROPIC_API_KEY")
    if not key:
        raise HTTPException(503, "Kein Schlüssel für die Übersetzung hinterlegt.")
    import anthropic
    system = (f"Du dolmetschst ein Gespräch zu zweit zwischen {SPRACHEN[von]} und {SPRACHEN[nach]}. "
              f"Übersetze den letzten Satz aus dem {SPRACHEN[von]} ins {SPRACHEN[nach]}. "
              "Gib nur die Übersetzung aus, nichts davor, nichts danach, keine Anführungszeichen. "
              "Sprich wie eine Muttersprachlerin im Gespräch: kurz, natürlich, idiomatisch; die Anrede (du oder Sie) "
              "übernimmst du aus dem Gespräch, im Zweifel höflich. Mundart und Umgangssprache gibst du in "
              "verständlichem Standard wieder. Der bisherige Verlauf dient nur dem Verständnis; ergänze unklare Bezüge "
              "nur, wenn der Satz sonst missverständlich wäre, und übersetze den Verlauf nicht noch einmal. "
              "Ist der Satz unverständlich, gib ihn so gut es geht wieder; erfinde nichts dazu.")
    teile = [f"{SPRACHEN.get(str(k.get('von')), 'Unbekannt')}: {str(k.get('text') or '')[:300]}" for k in kontext[-KONTEXT_MAX:]]
    user = ("Bisheriger Verlauf:\n" + "\n".join(teile) + "\n\n" if teile else "") + f"Zu übersetzender Satz:\n{text}"
    try:
        client = anthropic.Anthropic(api_key=key, timeout=20, max_retries=1)
        r = client.messages.create(model=MODELL, max_tokens=400, system=system,
                                   messages=[{"role": "user", "content": user}])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"Übersetzung nicht erreichbar ({type(e).__name__}).")
    return "".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip()


def sprechen(text: str, sprache: str) -> bytes:
    key = _schluessel("ELEVENLABS_API_KEY")
    if not key:
        raise HTTPException(503, "Keine Serverstimme hinterlegt.")
    try:
        r = _http.post(f"https://api.elevenlabs.io/v1/text-to-speech/{STIMME}", params={"output_format": "mp3_22050_32"},
                       headers={"xi-api-key": key, "accept": "audio/mpeg"},
                       json={"text": text, "model_id": "eleven_flash_v2_5", "language_code": sprache,
                             "voice_settings": {"stability": 0.6, "similarity_boost": 0.7, "speed": 0.95}})
    except httpx.HTTPError as e:
        raise HTTPException(503, f"Stimme nicht erreichbar ({type(e).__name__}).")
    if r.status_code != 200:
        raise HTTPException(503, f"Stimme antwortet mit {r.status_code}.")
    return r.content


@app.get("/dolmetscher")
def dolmetscher_seite():
    return FileResponse(STATIC / "dolmetscher.html", media_type="text/html",
                        headers={"Cache-Control": "no-cache", "X-Robots-Tag": "noindex, nofollow"})


@app.post("/api/dolmetscher/uebersetzen")
def api_uebersetzen(req: UebersetzenReq, request: Request):
    _pruefe(request, req.code)
    if req.von not in SPRACHEN or req.nach not in SPRACHEN or req.von == req.nach:
        raise HTTPException(400, "Unbekannte Sprache.")
    text = req.text.strip()[:1200]
    if not text:
        raise HTTPException(400, "Kein Text.")
    t = time.time()
    out = uebersetzen(text, req.von, req.nach, [k for k in req.kontext if isinstance(k, dict)])
    dauer = round(time.time() - t, 2)
    print(f"[dolmetscher] {req.von}->{req.nach} {len(text)} Zeichen, Kontext {len(req.kontext)}, {dauer} s", flush=True)
    return {"uebersetzt": out, "dauer": dauer}


@app.post("/api/dolmetscher/sprechen")
def api_sprechen(req: SprechenReq, request: Request):
    _pruefe(request, req.code)
    if req.sprache not in SPRACHEN:
        raise HTTPException(400, "Unbekannte Sprache.")
    text = req.text.strip()[:1200]
    if not text:
        raise HTTPException(400, "Kein Text.")
    mp3 = sprechen(text, req.sprache)
    print(f"[dolmetscher] stimme {req.sprache} {len(text)} Zeichen", flush=True)
    return Response(mp3, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/dolmetscher/status")
def api_status(request: Request, code: str = ""):
    ok = (code or "").strip() == zugangscode()
    return {"code_ok": ok, "uebersetzung": bool(_schluessel("ANTHROPIC_API_KEY")),
            "serverstimme": bool(_schluessel("ELEVENLABS_API_KEY")), "sprachen": SPRACHEN}
