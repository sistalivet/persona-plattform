"""
Twin-Plattform (Beta) — Multi-User-Oberflaeche mit echten Accounts + Isolation.
Jeder Nutzer sieht NUR seine Avatare und Videos (data/users/<id>/).
Nutzt die vorhandene Pipeline (Avatar Twin/twin_pipeline.py) mit --data-dir.
API-Keys sind geteilt (Avatar Twin/avatar_keys.txt = Max zahlt).

Start:
    ..\\share-bundle-2.1\\venv\\Scripts\\python.exe -m uvicorn platform_app:app --app-dir <dieser Ordner> --port 8020
"""
import contextvars
import datetime as _dt
import functools
import tempfile
import hashlib
import html as _htmlmod
import io
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import unicodedata
import urllib.parse
import urllib.request
import uuid
import webbrowser
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import auth
import suche as _suche_mod

HIER = Path(__file__).resolve().parent
DATA = HIER / "data"
STATIC = HIER / "static"
_BASE = Path(os.environ.get("PERSONA_BASE", r"C:/Users/maxst/Desktop"))
_AVATAR_TWIN = Path(os.environ.get("PERSONA_TOOLS", str(_BASE / "Avatar Twin")))
TWIN_SCRIPT = _AVATAR_TWIN / "twin_pipeline.py"
REFRAME_SCRIPT = _AVATAR_TWIN / "reframe.py"
# ffmpeg findbar (lokal via venv-Scripts; Server hat System-ffmpeg) + ElevenLabs-Modul importierbar
_VENV_SCRIPTS = _BASE / "share-bundle-2.1" / "venv" / "Scripts"
if _VENV_SCRIPTS.is_dir():
    os.environ["PATH"] = str(_VENV_SCRIPTS) + os.pathsep + os.environ.get("PATH", "")
if str(_AVATAR_TWIN) not in sys.path:
    sys.path.insert(0, str(_AVATAR_TWIN))
FORMATS = ("9:16", "4:5", "1:1", "16:9")
# Newsroom-Ressorts (exakt wie in PERSONA.md / Uploader-Datenbank, inkl. Umlauten)
RESSORTS = ("Innenpolitik", "Außenpolitik", "Umwelt & Klima", "Meinung")
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
STEPS = {1: "Voice", 2: "Base", 3: "Lip sync", 4: "Enhance", 5: "Subtitles", 6: "Branding"}

app = FastAPI(title="Twin-Plattform")


@app.on_event("startup")
def _maybe_open_browser():
    url = os.environ.get("AUTO_OPEN_BROWSER")
    if url:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()


def user_dir(uid: int) -> Path:
    return DATA / "users" / str(uid)


def user_avatars(uid: int):
    p = user_dir(uid) / "presets"
    return sorted(x.stem for x in p.iterdir() if x.suffix.lower() in VIDEO_EXT) if p.is_dir() else []


def current_uid(request: Request):
    tok = request.cookies.get("session")
    return auth.read_token(tok) if tok else None


def require_user(request: Request):
    uid = current_uid(request)
    u = auth.get_user(uid) if uid else None
    if not u:
        raise HTTPException(401, "Please log in.")
    if u.get("role") == "gesperrt":   # gesperrtes Konto (28.10.2026): auch ein noch gueltiges Cookie oeffnet nichts mehr
        raise HTTPException(403, "This account is locked.")
    return u


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


# ElevenLabs premade voices (library) — cached in memory (rarely changes).
_voices_cache: list | None = None
_voices_ids: set[str] = set()


def _load_voices():
    global _voices_cache, _voices_ids
    if _voices_cache is None:
        try:
            import elevenlabs_voice as ev
            _voices_cache = ev.list_voices()
        except Exception:  # noqa: BLE001  (API/Key-Problem → leere Liste, Avatar-Stimme bleibt nutzbar)
            _voices_cache = []
        _voices_ids = {v["voice_id"] for v in _voices_cache}
    return _voices_cache


def _own_voices(uid: int) -> list:
    """Geklonte Stimmen dieses Kontos (aus voices.json), je voice_id einmal, Name = Avatar-Name(n)."""
    vmap = _load_json(user_dir(uid) / "presets" / "voices.json")
    by_id: dict[str, list] = {}
    for avatar, vid in vmap.items():
        if vid:
            by_id.setdefault(vid, []).append(avatar)
    return [{"voice_id": vid, "name": " / ".join(names)} for vid, names in by_id.items()]


@app.get("/api/voices")
def voices_list(request: Request):
    u = require_user(request)
    return {"voices": _load_voices(), "own": _own_voices(u["id"])}


def _user_bg_dir(uid: int) -> Path:
    return user_dir(uid) / "backgrounds"


def _user_bgs(uid: int) -> list:
    d = _user_bg_dir(uid)
    return sorted(p.stem for p in d.glob("*.jpg")) if d.is_dir() else []


def _bg_name_ok(name: str) -> str:
    name = (name or "").strip()[:40]
    if not name or any(c in name for c in ("/", "\\", "..", ":")):
        raise HTTPException(400, "Invalid background name.")
    return name


@app.get("/api/backgrounds")
def backgrounds_list(request: Request):
    u = require_user(request)
    import grade
    return {"backgrounds": [{"id": k, "label": v[0]} for k, v in grade.BACKGROUNDS.items()],
            "own": _user_bgs(u["id"])}


@app.post("/api/backgrounds/upload")
def bg_upload(request: Request, name: str = Form(...), file: UploadFile = File(...)):
    u = require_user(request)
    name = _bg_name_ok(name)
    if len(_user_bgs(u["id"])) >= 20 and name not in _user_bgs(u["id"]):
        raise HTTPException(400, "Too many backgrounds (max 20).")
    raw = file.file.read()
    if len(raw) > 15 * 1024 * 1024:
        raise HTTPException(400, "Image too large (max 15 MB).")
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        im.thumbnail((2200, 2200))
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "Not a valid image.")
    d = _user_bg_dir(u["id"])
    d.mkdir(parents=True, exist_ok=True)
    im.save(d / f"{name}.jpg", "JPEG", quality=88)
    return {"ok": True, "name": name}


class BgGenReq(BaseModel):
    name: str
    prompt: str


@app.post("/api/backgrounds/generate")
def bg_generate(req: BgGenReq, request: Request):
    u = require_user(request)
    name = _bg_name_ok(req.name)
    prompt = (req.prompt or "").strip()[:300]
    if not prompt:
        raise HTTPException(400, "Describe the background first.")
    if len(_user_bgs(u["id"])) >= 20 and name not in _user_bgs(u["id"]):
        raise HTTPException(400, "Too many backgrounds (max 20).")
    import time
    import urllib.request as ur
    import matte
    matte.lade_keys()
    tok = os.environ.get("REPLICATE_API_TOKEN")
    if not tok:
        raise HTTPException(502, "Replicate token missing.")
    full = (f"{prompt}, background for a vertical video, blurred bokeh, shallow depth of field, "
            f"empty scene, no people, no text, photorealistic")
    body = json.dumps({"input": {"prompt": full, "aspect_ratio": "9:16",
                                 "output_format": "jpg", "num_outputs": 1,
                                 "megapixels": "1"}}).encode()
    hdr = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}
    try:
        pr = None
        for _att in range(3):   # Replicate wirft gelegentlich transiente 503
            try:
                pr = json.load(ur.urlopen(ur.Request(
                    "https://api.replicate.com/v1/models/black-forest-labs/flux-schnell/predictions",
                    data=body, headers=hdr), timeout=30))
                break
            except Exception:  # noqa: BLE001
                if _att == 2:
                    raise
                time.sleep(4)
        get = pr["urls"]["get"]
        st = {"status": "starting"}
        for _ in range(45):
            try:
                st = json.load(ur.urlopen(ur.Request(get, headers=hdr), timeout=30))
            except Exception:  # noqa: BLE001  (transiente 503 beim Polling ueberstehen)
                time.sleep(2)
                continue
            if st["status"] in ("succeeded", "failed", "canceled"):
                break
            time.sleep(2)
        if st["status"] != "succeeded":
            raise HTTPException(502, "Image generation failed.")
        out = st["output"]
        url = out[0] if isinstance(out, list) else out
        d = _user_bg_dir(u["id"])
        d.mkdir(parents=True, exist_ok=True)
        ur.urlretrieve(url, str(d / f"{name}.jpg"))
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Image generation failed: {str(e)[:120]}")
    return {"ok": True, "name": name}


@app.delete("/api/backgrounds/{name}")
def bg_delete(name: str, request: Request):
    u = require_user(request)
    name = _bg_name_ok(name)
    (_user_bg_dir(u["id"]) / f"{name}.jpg").unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/backgrounds/image/{name}")
def bg_image(name: str, request: Request):
    u = require_user(request)
    p = _user_bg_dir(u["id"]) / f"{_bg_name_ok(name)}.jpg"
    if not p.is_file():
        raise HTTPException(404, "Not found.")
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})


# Stil-Vorlagen: benannte Kombination aller Gestaltungs-Einstellungen, pro Konto.
_STYLE_KEYS = {"look", "lipsync", "background", "captions", "cap_style", "cap_y", "cap_size",
               "vignette", "speed", "pause_every", "contrast", "warmth", "brightness", "branding"}


def _styles_file(uid: int) -> Path:
    return user_dir(uid) / "styles.json"


class StyleReq(BaseModel):
    name: str
    settings: dict


@app.get("/api/styles")
def styles_get(request: Request):
    u = require_user(request)
    return {"styles": _load_json(_styles_file(u["id"]))}


@app.post("/api/styles")
def styles_save(req: StyleReq, request: Request):
    u = require_user(request)
    name = (req.name or "").strip()[:40]
    if not name:
        raise HTTPException(400, "Style name missing.")
    settings = {k: v for k, v in (req.settings or {}).items() if k in _STYLE_KEYS}
    sf = _styles_file(u["id"])
    styles = _load_json(sf)
    if name not in styles and len(styles) >= 30:
        raise HTTPException(400, "Too many styles (max 30).")
    styles[name] = settings
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps(styles, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": True}


@app.delete("/api/styles/{name}")
def styles_delete(name: str, request: Request):
    u = require_user(request)
    sf = _styles_file(u["id"])
    styles = _load_json(sf)
    styles.pop(name, None)
    sf.write_text(json.dumps(styles, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": True}


class ScriptReq(BaseModel):
    input: str
    seconds: int | None = None
    tone: str = ""
    preset: str = ""
    lang: str = "de"


class TranslateReq(BaseModel):
    text: str
    lang: str = "en"


@app.post("/api/translate")
def script_translate(req: TranslateReq, request: Request):
    require_user(request)
    txt = (req.text or "").strip()[:6000]
    if not txt:
        raise HTTPException(400, "No text.")
    try:
        import script_engine
        out = script_engine.translate(txt, req.lang)
    except SystemExit:
        raise HTTPException(502, "Script assistant not configured (ANTHROPIC key).")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Translation failed: {str(e)[:150]}")
    return {"script": out}


@app.post("/api/script")
def script_suggest(req: ScriptReq, request: Request):
    u = require_user(request)
    txt = (req.input or "").strip()[:4000]
    if not txt:
        raise HTTPException(400, "No input.")
    secs = req.seconds if (req.seconds and 5 <= req.seconds <= 300) else None
    tone = (req.tone or "").strip()[:200]
    try:
        import script_engine
        out = script_engine.generate(txt, secs, tone=tone or None, lang=req.lang)
    except SystemExit:
        raise HTTPException(502, "Script assistant not configured (ANTHROPIC key).")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Script assistant failed: {str(e)[:150]}")
    if req.preset and req.preset in user_avatars(u["id"]):   # Ton pro Avatar merken
        sp = user_dir(u["id"]) / "presets" / "settings.json"
        settings = _load_json(sp)
        cur = settings.get(req.preset, {}); cur["tone"] = tone; settings[req.preset] = cur
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"script": out}


# --------------------------- Video erstellen ------------------------
_jobs: dict[str, dict] = {}
_lock = threading.Lock()
# Warteschlange: sequenziell abarbeiten, damit die kleine Box nicht ueberlastet
# (mehrere gleichzeitige Renders liefen sonst parallel -> Absturz-Risiko).
_jobq: "queue.Queue" = queue.Queue()
_pending: list[str] = []   # wartende job_ids in Reihenfolge (unter _lock)
_WORKERS = max(1, int(os.environ.get("PERSONA_WORKERS", "1")))


def _worker_loop():
    while True:
        job_id, uid, preset, text, opts, out_path = _jobq.get()
        try:
            with _lock:
                if job_id in _pending:
                    _pending.remove(job_id)
                j = _jobs.get(job_id)
                if j is not None:
                    j["queued"] = False
                    j["running"] = True
            if j is not None:
                _run(job_id, uid, preset, text, opts, out_path)
        except BaseException as e:  # noqa: BLE001  (Worker darf nie sterben, auch nicht an einem SystemExit
            # aus einem Hilfsmodul: am 17.10.2026 blieb so ein Auftrag ewig bei "Voice" stehen)
            with _lock:
                if job_id in _jobs:
                    _jobs[job_id].update(queued=False, running=False, ok=False,
                                         error=str(e)[:300] or type(e).__name__)
        finally:
            _jobq.task_done()


@app.on_event("startup")
def _start_workers():
    for _ in range(_WORKERS):
        threading.Thread(target=_worker_loop, daemon=True).start()


def _write_sidecar(out_path: Path, preset: str, text: str, opts: dict):
    """Begleitdatei <name>.json neben dem Video, Format lt. PERSONA.md (Newsroom-Uploader).
    UTF-8, alle Texte NFC-normalisiert. Fehlende Angaben werden weggelassen, nie geraten."""
    def _n(s: str) -> str:
        return unicodedata.normalize("NFC", str(s))
    rec = {}
    title = (opts.get("title") or "").strip()
    if title:
        rec["titel"] = _n(title)
    rec["sprache"] = _n((opts.get("lang") or "de")[:5])
    ressort = (opts.get("ressort") or "").strip()
    if ressort:
        rec["ressort"] = _n(ressort)
    rec["herkunft"] = "persona"
    rec["avatar"] = _n((_profile(preset[3:]) or {}).get("name", preset) if preset.startswith("hg:") else preset)
    rec["skript"] = _n(text.strip())
    # Belegte Aussage, keine Vermutung: true nur, wenn das Label wirklich eingebrannt wurde.
    rec["ki_kennzeichnung_eingebrannt"] = bool(opts.get("ai_label", True))
    if opts.get("_kennzeichen"):
        rec["kennzeichen"] = opts["_kennzeichen"]   # Code des Herkunftsregisters, im Bild eingebrannt (30.10.2026)
    rec["quelle"] = None
    rec["datum"] = _dt.date.today().isoformat()
    rec["format"] = None   # leitet der Uploader aus dem Video ab
    try:
        out_path.with_suffix(".json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass   # Begleitdatei darf einen fertigen Render nie scheitern lassen


def _heygen_render(job_id: str, uid: int, slug: str, text: str, opts: dict, work: Path) -> tuple[Path, Path]:
    """Foto-Avatar eines Teamprofils ueber HeyGen: ElevenLabs-Stimme (mit Wort-Zeiten) -> HeyGen -> Rohvideo.
    Liefert (video, alignment). Das Bild-Asset wird im Profil gemerkt (einmaliger Upload)."""
    import elevenlabs_voice as ev
    import heygen
    prof = _profile(slug)
    work.mkdir(parents=True, exist_ok=True)
    audio = work / "stimme.mp3"
    voice = (prof or {}).get("voice_id") or opts.get("voice") or None

    def log(msg):
        with _lock:
            _jobs[job_id]["log"] = (_jobs[job_id].get("log", "") + msg + chr(10))[-2500:]

    with _lock:
        _jobs[job_id]["step"] = 1
    hg_voice = (prof or {}).get("heygen_voice_id")
    if hg_voice:
        # HeyGen-eigene Stimme des Profils, mit Wortzeiten (seit 17.10.2026 der Normalfall; ElevenLabs
        # bleibt nur fuer Profile ohne heygen_voice_id)
        log(f"[1/6] Stimme erzeugen (HeyGen, {(prof or {}).get('heygen_voice_name') or hg_voice}) …")
        heygen.speech(text, hg_voice, audio, speed=float(opts.get("speed", 1.0) or 1.0),
                      language=(prof or {}).get("sprache") or "de")
    else:
        log("[1/6] Stimme erzeugen (ElevenLabs) …")
        ev.tts(text, str(audio), voice, "eleven_multilingual_v2")
    if not prof.get("heygen_asset_id"):
        log("[2/6] Profilbild einmalig zu HeyGen hochladen …")
        # Hochformat-Portraet (<slug>_hg.jpg, Kopf im oberen Drittel) vor dem quadratischen Profilfoto:
        # aus dem Quadrat machte HeyGen ein bildfuellendes Gesicht, dessen Mund in den Untertiteln lag (19.10.2026)
        bild = PROFILES_DIR / (slug + "_hg.jpg")
        if not bild.is_file():
            bild = PROFILES_DIR / (slug + ".jpg")
        a = heygen.upload_asset(bild)
        rec = _profile(slug)
        rec["heygen_asset_id"] = a["asset_id"]
        (PROFILES_DIR / (slug + ".json")).write_text(json.dumps({k: v for k, v in rec.items() if k not in ("id", "slug", "photo")},
                                                              ensure_ascii=False, indent=2), encoding="utf-8")
        prof = rec
    with _lock:
        _jobs[job_id]["step"] = 3
    log("[3/6] HeyGen rendert den Foto-Avatar …")
    vorher = heygen.wallet()
    b = heygen.upload_asset(audio)
    vid = heygen.create_image_video(prof["heygen_asset_id"], b["asset_id"], opts.get("title") or slug)
    raw = work / "heygen.mp4"
    heygen.wait_and_download(vid, raw, log=log)
    nachher = heygen.wallet()
    with _lock:   # for the usage record: the real HeyGen charge, as far as the wallet shows it
        _jobs[job_id]["heygen_wallet"] = {"vorher": vorher, "nachher": nachher, "video_id": vid}
    return raw, Path(str(audio) + ".align.json")


def _run(job_id: str, uid: int, preset: str, text: str, opts: dict, out_path: Path):
    import time as _time
    _t0 = _time.time()
    _hg_from = []
    if preset.startswith("hg:"):
        try:
            raw, align = _heygen_render(job_id, uid, preset[3:], text, opts, user_dir(uid) / "outputs" / f"_hg_{job_id}")
            _hg_from = ["--from-video", str(raw)] + (["--from-align", str(align)] if align.is_file() else [])
        except BaseException as e:  # noqa: BLE001  (auch SystemExit aus elevenlabs_voice/heygen)
            with _lock:
                _jobs[job_id].update(running=False, ok=False, error=f"HeyGen/Stimme: {str(e)[:200] or type(e).__name__}")
            return
    if opts.get("ai_label", True):
        # Kennzeichen des Herkunftsregisters (30.10.2026): beim Render vergeben, in die Kennzeichnungszeile
        # eingebrannt, beim Anhaengen an den Beitrag Teil des Registereintrags
        try:
            import herkunft
            opts["_kennzeichen"] = herkunft.neuer_code(job_id, uid)
            with _lock:
                _jobs[job_id]["kennzeichen"] = opts["_kennzeichen"]
        except Exception as e:  # noqa: BLE001  (ohne Code rendert das Video trotzdem)
            print(f"[herkunft] kein Kennzeichen fuer {job_id}: {type(e).__name__}: {e}", flush=True)
    cmd = [sys.executable, "-u", str(TWIN_SCRIPT), "--data-dir", str(user_dir(uid)),
           "--text", text, "-o", str(out_path)] + (_hg_from or ["--preset", preset]) + [
           "--look", opts["look"], "--green", opts["green"],
           "--vignette", str(opts.get("vignette", 0.4)),
           "--speed", str(opts.get("speed", 1.0)),
           "--contrast", str(opts.get("contrast", 0.0)),
           "--warmth", str(opts.get("warmth", 0.0)),
           "--brightness", str(opts.get("brightness", 0.0)),
           "--cap-style", opts["cap_style"], "--cap-pos", opts["cap_pos"],
           "--cap-size", opts["cap_size"], "--cap-y", str(opts.get("cap_y", 0.80))]
    if opts.get("voice"):
        cmd += ["--voice-id", opts["voice"]]   # library voice overrides the avatar's cloned voice
    _bgo = str(opts.get("background", "none"))
    if _bgo != "none":
        if _bgo.startswith("u:"):
            cmd += ["--background-file", str(user_dir(uid) / "backgrounds" / (_bgo[2:] + ".jpg"))]
        else:
            cmd += ["--background", _bgo]
    if not opts["captions"]:
        cmd.append("--no-captions")
    if not opts.get("branding", True):
        cmd.append("--no-branding")
    if not opts.get("ai_label", True):
        cmd.append("--no-ai-label")
    if opts.get("_kennzeichen"):
        cmd += ["--kennzeichen", opts["_kennzeichen"]]
    if opts.get("pause_every", 0):
        cmd += ["--pause-every", str(opts["pause_every"])]
    cmd += ["--lipsync-model",
            "sync/lipsync-2-pro" if opts.get("lipsync") == "premium" else "sync/lipsync-2"]
    lang = str(opts.get("lang", "de"))[:5]
    if preset.startswith("hg:"):   # Fotoavatar eines Profils: Sprache des Profils (englische Ausgabe: en)
        lang = (_profile(preset[3:]) or {}).get("sprache") or lang
    cmd += ["--lang", lang]
    _gp_file = None
    if isinstance(opts.get("grafik_plan"), dict) and opts["grafik_plan"]:
        byline = opts.get("byline") if isinstance(opts.get("byline"), dict) else {}
        if not byline and preset.startswith("hg:"):
            prof = _profile(preset[3:]) or {}
            byline = {"name": prof.get("name", ""), "bauchbinde": prof.get("bauchbinde") or prof.get("kurz") or ""}
        _gp_file = user_dir(uid) / "outputs" / f"_gp_{job_id}.json"
        _gp_file.write_text(json.dumps({"plan": opts["grafik_plan"], "byline": byline,
                                        "ressort": opts.get("ressort", ""), "lang": lang}, ensure_ascii=False), encoding="utf-8")
        cmd += ["--grafik-plan", str(_gp_file)]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
        for line in proc.stdout:
            m = re.search(r"\[(\d)/6\]", line)
            with _lock:
                if m:
                    _jobs[job_id]["step"] = int(m.group(1))
                _jobs[job_id]["log"] = (_jobs[job_id].get("log", "") + line)[-2500:]
        proc.wait()
        ok = proc.returncode == 0 and out_path.exists()
        with _lock:
            j = _jobs[job_id]
            _log = j.get("log", "")
            j["lip_cached"] = "Lippensync aus Cache" in _log     # aus Cache -> 0 $ Lippensync
            j["running"] = False
            j["ok"] = ok
            if ok:
                j.update(url=f"/media/{out_path.name}", file=out_path.name)
            else:
                log = j.get("log", "")
                if any(m in log for m in ("TEXT_TOO_LONG", "Video duration must be 30",
                                          "30 seconds or less")):
                    j["error"] = "Text too long — please shorten the script (max ~10 minutes)."
                else:
                    j["error"] = "Generation failed."
        if _hg_from:
            shutil.rmtree(user_dir(uid) / "outputs" / f"_hg_{job_id}", ignore_errors=True)
        if _gp_file:
            _gp_file.unlink(missing_ok=True)
        if ok:
            _save_title(uid, out_path.name, opts.get("title", ""))
            _write_sidecar(out_path, preset, text, opts)
            _record_usage(uid, out_path, opts, _log, took=round(_time.time() - _t0), preset=preset,
                          heygen=_jobs.get(job_id, {}).get("heygen_wallet"))
    except Exception as e:  # noqa: BLE001
        with _lock:
            _jobs[job_id].update(running=False, ok=False, error=str(e)[:300])


def _save_script(uid: int, text: str, title: str, lang: str, preset: str):
    """Skript-Verlauf pro Konto (letzte 50), damit nichts verloren geht / wiederverwendbar."""
    sf = user_dir(uid) / "scripts.json"
    try:
        recs = json.loads(sf.read_text(encoding="utf-8")) if sf.exists() else []
        if not isinstance(recs, list):
            recs = []
    except (ValueError, OSError):
        recs = []
    text = (text or "").strip()
    if recs and recs[-1].get("text") == text:   # identisches Skript nicht doppeln
        recs[-1].update(at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                        title=(title or recs[-1].get("title", ""))[:80])
    else:
        recs.append({"at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                     "title": (title or "")[:80], "lang": (lang or "de")[:5],
                     "preset": preset, "text": text[:6000]})
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps(recs[-50:], ensure_ascii=False), encoding="utf-8")


@app.get("/api/scripts")
def scripts_list(request: Request):
    u = require_user(request)
    sf = user_dir(u["id"]) / "scripts.json"
    try:
        recs = json.loads(sf.read_text(encoding="utf-8")) if sf.exists() else []
        if not isinstance(recs, list):
            recs = []
    except (ValueError, OSError):
        recs = []
    return {"scripts": list(reversed(recs))}


def _titles_file(uid: int) -> Path:
    return user_dir(uid) / "outputs" / "titles.json"


def _save_title(uid: int, filename: str, title: str):
    title = (title or "").strip()[:80]
    if not title:
        return
    tf = _titles_file(uid)
    titles = _load_json(tf)
    titles[filename] = title
    # Eintraege geloeschter Videos aufraeumen
    out_dir = user_dir(uid) / "outputs"
    titles = {k: v for k, v in titles.items() if (out_dir / k).exists() or k == filename}
    tf.parent.mkdir(parents=True, exist_ok=True)
    tf.write_text(json.dumps(titles, ensure_ascii=False), encoding="utf-8")


def _heygen_rate() -> float:
    """USD per second of a HeyGen photo-avatar video (wallet billing). In data/limits.json as
    "heygen_usd_per_sec"; the default is the rate measured on 15 October 2026."""
    cfg = _load_json(DATA / "limits.json")
    try:
        return float((cfg or {}).get("heygen_usd_per_sec", HEYGEN_USD_PER_SEC))
    except (TypeError, ValueError):
        return HEYGEN_USD_PER_SEC


HEYGEN_USD_PER_SEC = 0.0385   # measured 15.10.2026 from the wallet: 1.55 $ for 40.5 s, 1.17 $ for 30.3 s; overridable in limits.json


def _record_usage(uid: int, out_path: Path, opts: dict, log: str, took: int = 0, preset: str = "",
                  heygen: dict | None = None):
    """Verbrauch pro fertigem Video protokollieren (Schaetzkosten, aus-Cache-Info).
    HeyGen-Fotoavatare (preset hg:…) kosten HeyGen-Guthaben je Sekunde, keinen Replicate-Lippensync;
    bis 15.10.2026 wurden sie faelschlich mit dem Replicate-Satz geschaetzt."""
    try:
        pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                             "-of", "default=nk=1:nw=1", str(out_path)], capture_output=True, text=True)
        dur = float(pr.stdout.strip() or 0)
    except (ValueError, OSError):
        dur = 0.0
    model = opts.get("lipsync", "standard")
    bg = opts.get("background", "none") != "none"
    matting_ran = "Matting via Replicate" in log
    if preset.startswith("hg:"):
        model = "heygen"
        lip_cached = False
        est = dur * _heygen_rate() + (0.15 if matting_ran else 0.0)
        saved = 0.0
    else:
        rate = 0.083 if model == "premium" else 0.05
        lip_cached = "Lippensync aus Cache" in log
        est = (0.0 if lip_cached else dur * rate + 0.05) + (0.15 if matting_ran else 0.0)
        saved = (dur * rate + 0.05 if lip_cached else 0.0) + (0.15 if (bg and not matting_ran) else 0.0)
    rec = {"at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
           "dur": round(dur, 1), "model": model, "bg": bg, "cached": lip_cached,
           "title": (opts.get("title") or "").strip()[:80],
           "took": took,
           "est": round(est, 3), "saved": round(saved, 3)}
    if heygen:
        rec["heygen_wallet"] = heygen   # balance before and after, for checking the rate
    uf = user_dir(uid) / "usage.json"
    try:
        recs = json.loads(uf.read_text(encoding="utf-8")) if uf.exists() else []
        if not isinstance(recs, list):
            recs = []
    except (ValueError, OSError):
        recs = []
    recs.append(rec)
    uf.write_text(json.dumps(recs[-1000:], ensure_ascii=False), encoding="utf-8")


def _usage_records(uid: int) -> list:
    uf = user_dir(uid) / "usage.json"
    try:
        recs = json.loads(uf.read_text(encoding="utf-8")) if uf.exists() else []
        return recs if isinstance(recs, list) else []
    except (ValueError, OSError):
        return []


def _month_cost(uid: int) -> float:
    ym = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m")
    return sum(r.get("est", 0) for r in _usage_records(uid) if str(r.get("at", "")).startswith(ym))


def _user_limit(uid: int) -> float:
    """Weiches Monatsbudget (geschaetzte $) pro Konto aus data/limits.json;
    0 = unbegrenzt. Aendern: per_user["<uid>"] bzw. default anpassen."""
    cfg = _load_json(DATA / "limits.json")
    per = cfg.get("per_user", {}) if isinstance(cfg, dict) else {}
    try:
        return float(per.get(str(uid), cfg.get("default", 15.0)))
    except (TypeError, ValueError):
        return 15.0


@app.get("/api/usage")
def usage(request: Request):
    u = require_user(request)
    uf = user_dir(u["id"]) / "usage.json"
    try:
        recs = json.loads(uf.read_text(encoding="utf-8")) if uf.exists() else []
        if not isinstance(recs, list):
            recs = []
    except (ValueError, OSError):
        recs = []
    ym = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m")
    month = [r for r in recs if str(r.get("at", "")).startswith(ym)]
    return {"total": len(recs),
            "total_cost": round(sum(r.get("est", 0) for r in recs), 2),
            "total_saved": round(sum(r.get("saved", 0) for r in recs), 2),
            "month_count": len(month),
            "month_cost": round(sum(r.get("est", 0) for r in month), 2),
            "limit": _user_limit(u["id"]),
            "cached_count": sum(1 for r in recs if r.get("cached")),
            "recent": list(reversed(recs[-8:]))}


class CreateReq(BaseModel):
    text: str
    preset: str
    title: str = ""   # optionaler Anzeigename des Videos
    look: str = "warm"
    green: str = "off"
    captions: bool = True
    cap_style: str = "box"
    cap_pos: str = "bottom"
    cap_size: str = "m"
    cap_y: float = 0.80   # freie vertikale Untertitel-Position 0..1 (oben..unten)
    branding: bool = True
    vignette: float = 0.4
    speed: float = 1.0
    voice: str = ""   # ElevenLabs library voice_id; "" = avatar's own cloned voice
    contrast: float = 0.0   # fine-tune delta -0.3..0.3 (0 = neutral)
    warmth: float = 0.0     # -1..1 (neg = cooler/blue, pos = warmer/yellow)
    brightness: float = 0.0 # -0.3..0.3 (0 = neutral)
    background: str = "none"   # studio background id, or "none" = keep original
    ai_label: bool = True      # "KI-erzeugt"-Kennzeichnung einbrennen (EU-AI-Act)
    pause_every: int = 2       # natuerliche Pause nach jedem N-ten Satz (0 = aus)
    lipsync: str = "standard"  # "standard" = sync/lipsync-2, "premium" = sync/lipsync-2-pro
    lang: str = "de"           # Sprache des Skripts (TTS erkennt automatisch; fuer Label/Assistent)
    ressort: str = ""          # Newsroom-Ressort fuer die Begleitdatei ("" = keins)
    grafik_plan: dict | None = None   # Grafikstrecke: Chip, Karten, Quellenzeile (Kette, vertrag/grafiken.md)
    byline: dict | None = None        # {"name", "bauchbinde"} fuer die Bauchbinde; leer = aus dem hg:-Profil


@app.post("/api/create")
def create(req: CreateReq, request: Request):
    u = require_user(request)
    if not req.text.strip():
        raise HTTPException(400, "No text.")
    if req.preset.startswith("hg:"):
        _pr = _profile(req.preset[3:])
        if not _pr or not _pr.get("photo"):
            raise HTTPException(400, "Profile for HeyGen avatar not found (needs a photo).")
    elif req.preset not in user_avatars(u["id"]):
        raise HTTPException(400, "Avatar not found.")
    for val, ok, label in ((req.look, ("subtle", "warm", "strong", "none"), "Look"),
                           (req.green, ("off", "mild", "strong"), "Green"),
                           (req.cap_style, ("clean", "gelb", "box"), "Style"),
                           (req.cap_pos, ("bottom", "middle", "top"), "Position"),
                           (req.cap_size, ("s", "m", "l"), "Size")):
        if val not in ok:
            raise HTTPException(400, f"Invalid {label}.")
    req.vignette = max(0.0, min(1.0, float(req.vignette)))
    req.speed = max(0.5, min(1.5, float(req.speed)))
    req.contrast = max(-0.3, min(0.3, float(req.contrast)))
    req.warmth = max(-1.0, min(1.0, float(req.warmth)))
    req.brightness = max(-0.3, min(0.3, float(req.brightness)))
    req.cap_y = max(0.0, min(1.0, float(req.cap_y)))
    req.pause_every = max(0, min(5, int(req.pause_every)))
    req.lipsync = req.lipsync if req.lipsync in ("standard", "premium") else "standard"
    req.ressort = unicodedata.normalize("NFC", req.ressort.strip())
    alle_ressorts = set(RESSORTS)
    for key in _ausgaben():                 # Renders jeder Ausgabe tragen deren Ressorts
        with _lang(key):
            alle_ressorts.update(_site()["ressorts"])
    if req.ressort and req.ressort not in alle_ressorts:
        raise HTTPException(400, "Invalid Ressort.")
    lim = _user_limit(u["id"])
    if lim > 0:
        spent = _month_cost(u["id"])
        if spent >= lim:
            raise HTTPException(429, f"Monthly budget reached (~${spent:.2f} of ${lim:.2f} est.). "
                                     f"Ask the admin to raise your limit.")
    if req.background != "none":
        if str(req.background).startswith("u:"):
            if not (_user_bg_dir(u["id"]) / (str(req.background)[2:] + ".jpg")).is_file():
                raise HTTPException(400, "Invalid background.")
        else:
            import grade
            if req.background not in grade.BACKGROUNDS:
                raise HTTPException(400, "Invalid background.")
    if req.voice:
        _load_voices()                       # ensures _voices_ids is populated
        own_ids = {v["voice_id"] for v in _own_voices(u["id"])}
        if req.voice not in _voices_ids and req.voice not in own_ids:   # library or OWN clones only
            raise HTTPException(400, "Invalid voice.")
    out_dir = user_dir(u["id"]) / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Platten-Schutz: nur juengste 30 Videos behalten
    olds = sorted(out_dir.glob("twin_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)[30:]
    for p in olds:
        p.unlink(missing_ok=True)
        p.with_suffix(".json").unlink(missing_ok=True)   # Begleitdatei mit entsorgen
    job_id = uuid.uuid4().hex[:12]
    out_path = out_dir / f"twin_{job_id}.mp4"
    opts = req.model_dump()
    _save_script(u["id"], req.text, req.title, req.lang, req.preset)   # Skript-Verlauf
    with _lock:
        _jobs[job_id] = {"uid": u["id"], "queued": True, "running": False, "step": 0,
                         "ok": None, "error": None, "url": None, "file": None,
                         "preset": req.preset, "title": req.title.strip()[:80]}
        _pending.append(job_id)
    _jobq.put((job_id, u["id"], req.preset, req.text, opts, out_path))
    return {"job_id": job_id}


@app.get("/api/status/{job_id}")
def status(job_id: str, request: Request):
    u = require_user(request)
    with _lock:
        j = _jobs.get(job_id)
        if not j or j["uid"] != u["id"]:      # Isolation: nur eigene Jobs
            raise HTTPException(404, "Job not found.")
        out = {k: v for k, v in j.items() if k not in ("log", "uid")}
        if j.get("queued"):
            out["position"] = (_pending.index(job_id) + 1) if job_id in _pending else 1
            out["step_name"] = ""
        else:
            out["step_name"] = STEPS.get(j["step"], "…") if j["running"] else ""
    return out


@app.get("/api/videos")
def videos(request: Request):
    u = require_user(request)
    out_dir = user_dir(u["id"]) / "outputs"
    _suf = ("_9x16.mp4", "_4x5.mp4", "_1x1.mp4", "_16x9.mp4")   # Format-Varianten ausblenden
    files = [f for f in out_dir.glob("twin_*.mp4") if not f.name.endswith(_suf)] \
        if out_dir.is_dir() else []
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    titles = _load_json(_titles_file(u["id"]))
    return {"videos": [{"file": f.name, "url": f"/media/{f.name}",
                        "size_mb": round(f.stat().st_size / 1e6, 1),
                        "title": titles.get(f.name, ""),
                        "ts": int(f.stat().st_mtime)} for f in files]}


class ReframeReq(BaseModel):
    file: str
    formats: list[str] = ["1:1", "4:5", "16:9"]


@app.post("/api/reframe")
def reframe_ep(req: ReframeReq, request: Request):
    u = require_user(request)
    if any(c in req.file for c in ("..", "/", "\\")):
        raise HTTPException(400, "Invalid.")
    src = user_dir(u["id"]) / "outputs" / req.file
    if not src.is_file():
        raise HTTPException(404, "Video not found.")
    fmts = [f for f in req.formats if f in FORMATS] or ["1:1", "4:5", "16:9"]
    variants = []
    for f in fmts:
        suf = f.replace(":", "x")
        out = src.with_name(f"{src.stem}_{suf}.mp4")
        p = subprocess.run([sys.executable, str(REFRAME_SCRIPT), "--video", str(src),
                            "--format", f, "-o", str(out)], capture_output=True, text=True)
        if p.returncode == 0 and out.is_file():
            sc = src.with_suffix(".json")
            if sc.is_file():   # Begleitdatei auch neben die Format-Variante legen
                try:
                    out.with_suffix(".json").write_bytes(sc.read_bytes())
                except OSError:
                    pass
            variants.append({"format": f, "file": out.name, "url": f"/media/{out.name}"})
    return {"variants": variants}


@app.get("/media/{name}")
def media(name: str, request: Request):
    u = require_user(request)                 # Isolation: nur eigener Output-Ordner
    if any(c in name for c in ("..", "/", "\\")):
        raise HTTPException(400, "Invalid.")
    p = user_dir(u["id"]) / "outputs" / name
    if not p.is_file():
        raise HTTPException(404, "Nicht gefunden.")
    if name.endswith(".json"):   # Begleitdatei zum Video (Newsroom-Uploader)
        return FileResponse(p, media_type="application/json")
    return FileResponse(p, media_type="video/mp4")


def _is_editor(u: dict) -> bool:
    """Redaktionsrechte auf der Website: Admin oder Rolle 'editor' (fremde Beitraege/Profile bearbeiten)."""
    return u.get("role") in ("admin", "editor")


# ------------------------------ Clip (Momente schneiden) ------------
# "Clip your moment": Ausschnitt aus einem fertigen Video oder einer hochgeladenen
# langen Aufnahme, optional in mehrere Formate. Rein lokal (ffmpeg) = 0 $ pro Clip.
_CLIP_MAX_SEC = 180.0        # max. Cliplaenge
_CLIP_SRC_CAP = 500 * 1024 * 1024   # Upload-Limit 500 MB
_CLIP_SRC_KEEP = 3           # nur juengste Quell-Aufnahmen behalten (Platte)


def _clip_src_dir(uid: int) -> Path:
    return user_dir(uid) / "clipsrc"


def _video_seconds(p: Path) -> float:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                              "format=duration", "-of", "csv=p=0", str(p)],
                             capture_output=True, text=True, timeout=30)
        return round(float(out.stdout.strip()), 1)
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return 0.0


@app.post("/api/clip/upload")
def clip_upload(request: Request, file: UploadFile = File(...)):
    u = require_user(request)
    name = Path(file.filename or "aufnahme.mp4").name
    if Path(name).suffix.lower() not in VIDEO_EXT:
        raise HTTPException(400, "Please upload a video file.")
    safe = re.sub(r"[^\w\-. äöüÄÖÜß]+", "", Path(name).stem).strip()[:60] or "aufnahme"
    d = _clip_src_dir(u["id"])
    d.mkdir(parents=True, exist_ok=True)
    dest = d / (safe + Path(name).suffix.lower())
    size = 0
    try:
        with dest.open("wb") as f:
            while True:
                chunk = file.file.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > _CLIP_SRC_CAP:
                    raise HTTPException(413, "File too large (max 500 MB).")
                f.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    for old in sorted((x for x in d.iterdir() if x.is_file()),
                      key=lambda p: p.stat().st_mtime, reverse=True)[_CLIP_SRC_KEEP:]:
        old.unlink(missing_ok=True)
    return {"ok": True, "name": dest.name, "seconds": _video_seconds(dest)}


@app.get("/api/clip/sources")
def clip_sources(request: Request):
    u = require_user(request)
    d = _clip_src_dir(u["id"])
    files = sorted((x for x in d.iterdir() if x.is_file()),
                   key=lambda p: p.stat().st_mtime, reverse=True) if d.is_dir() else []
    return {"sources": [{"name": f.name, "seconds": _video_seconds(f),
                         "size_mb": round(f.stat().st_size / 1e6, 1)} for f in files]}


@app.get("/api/clip/media/{name}")
def clip_media(name: str, request: Request):
    u = require_user(request)
    if any(c in name for c in ("..", "/", "\\")):
        raise HTTPException(400, "Invalid.")
    p = _clip_src_dir(u["id"]) / name
    if not p.is_file():
        raise HTTPException(404, "Nicht gefunden.")
    return FileResponse(p, media_type="video/mp4")


class ClipReq(BaseModel):
    source: str          # "out:<datei>" (My videos) oder "src:<datei>" (Upload)
    start: float = 0.0
    end: float = 20.0
    formats: list[str] = []   # zusaetzlich zum Original, z. B. ["9:16","1:1"]


def _clip_job(job_id: str, uid: int, src: Path, start: float, end: float,
              formats: list, title: str):
    out_dir = user_dir(uid) / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / f"twin_clip{uuid.uuid4().hex[:8]}.mp4"
    try:
        p = subprocess.run(["ffmpeg", "-y", "-ss", f"{start:.2f}", "-to", f"{end:.2f}",
                            "-i", str(src), "-c:v", "libx264", "-crf", "19",
                            "-preset", "veryfast", "-pix_fmt", "yuv420p",
                            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
                            str(base)], capture_output=True, text=True, timeout=900)
        if p.returncode != 0 or not base.is_file():
            raise RuntimeError((p.stderr or "ffmpeg failed")[-300:])
        variants = []
        for f in formats:
            suf = f.replace(":", "x")
            out = base.with_name(f"{base.stem}_{suf}.mp4")
            r = subprocess.run([sys.executable, str(REFRAME_SCRIPT), "--video", str(base),
                                "--format", f, "-o", str(out)],
                               capture_output=True, text=True, timeout=900)
            if r.returncode == 0 and out.is_file():
                variants.append({"format": f, "file": out.name, "url": f"/media/{out.name}"})
        _save_title(uid, base.name, title)
        with _lock:
            _jobs[job_id].update(running=False, ok=True, url=f"/media/{base.name}",
                                 file=base.name, variants=variants)
    except Exception as e:  # noqa: BLE001
        base.unlink(missing_ok=True)
        with _lock:
            _jobs[job_id].update(running=False, ok=False, error=str(e)[:300])


@app.post("/api/clip")
def clip_create(req: ClipReq, request: Request):
    u = require_user(request)
    kind, _, name = req.source.partition(":")
    if kind not in ("out", "src") or any(c in name for c in ("..", "/", "\\")) or not name:
        raise HTTPException(400, "Invalid source.")
    src = (user_dir(u["id"]) / "outputs" / name) if kind == "out" else (_clip_src_dir(u["id"]) / name)
    if not src.is_file():
        raise HTTPException(404, "Source not found.")
    start, end = max(0.0, float(req.start)), float(req.end)
    if end <= start:
        raise HTTPException(400, "End must be after start.")
    if end - start > _CLIP_MAX_SEC:
        raise HTTPException(400, f"Clip too long (max {int(_CLIP_MAX_SEC)} s).")
    fmts = [f for f in req.formats if f in FORMATS]
    titles = _load_json(_titles_file(u["id"]))
    label = titles.get(name, "") or Path(name).stem
    title = f"Clip · {label}"[:80]
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _jobs[job_id] = {"uid": u["id"], "queued": False, "running": True, "step": 0,
                         "ok": None, "error": None, "url": None, "file": None,
                         "preset": "", "title": title}
    threading.Thread(target=_clip_job, args=(job_id, u["id"], src, start, end, fmts, title),
                     daemon=True).start()
    return {"job_id": job_id}


# ------------------------------ Ziele / Kennzahlen -------------------
# Cookieloser Reichweiten-Zaehler fuer /news (Besuche, Video-Starts, Durchsicht,
# Reaktionsknoepfe) + manuelle Social-Wochenwerte. Besucher = Tages-Hash aus
# IP+Browser+Geheimnis (nicht rueckrechenbar, wechselt taeglich), keine Rohdaten.
METRICS_DB = DATA / "metrics.db"
_EVENT_KINDS = ("view", "seen", "play", "progress", "react")
_REACTIONS = ("neu", "mut", "teilen")
_PROGRESS = ("25", "50", "75", "100")
_PLATFORMS = ("facebook", "instagram", "youtube", "tiktok")
_ev_counter: dict[str, list] = {}   # visitor -> [day, count]  (Missbrauchsbremse)


def _mdb() -> sqlite3.Connection:
    METRICS_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(METRICS_DB, timeout=10)
    con.execute("CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts TEXT, day TEXT, "
                "kind TEXT, post TEXT, value TEXT, source TEXT, visitor TEXT)")
    con.execute("CREATE INDEX IF NOT EXISTS ev_day ON events(day)")
    con.execute("CREATE INDEX IF NOT EXISTS ev_post ON events(post, kind)")
    con.execute("CREATE TABLE IF NOT EXISTS social_weekly(week TEXT, platform TEXT, reach INTEGER, "
                "views INTEGER, followers INTEGER, shares INTEGER, pos INTEGER, neg INTEGER, "
                "notes TEXT, uid INTEGER, at TEXT, PRIMARY KEY(week, platform))")
    return con


def _visitor(request: Request) -> str:
    raw = f"{_dt.date.today().isoformat()}|{_client_ip(request)}|{request.headers.get('user-agent', '')}"
    return hashlib.sha256(auth._secret() + raw.encode("utf-8")).hexdigest()[:16]


def _week_key(day: str) -> str:
    y, w, _ = _dt.date.fromisoformat(day).isocalendar()
    return f"{y}-W{w:02d}"


class EventReq(BaseModel):
    kind: str
    post: str = "index"
    value: str = ""
    source: str = ""


@app.get("/api/metrics/summary")
def metrics_summary(request: Request, von: str = "", bis: str = ""):
    """Zugriffe je Ausgabe fuer einen Zeitraum (Tage, einschliesslich), fuer die Kette (Qualitaetsbericht,
    19.10.2026): Aufrufe, Besucher (Naeherung ueber Tages-Hashes), Videostarts, bis zum Ende gesehen,
    Reaktionen, die drei meistgelesenen Beitraege, Herkunft, Newsletter-Stand. Keine Personendaten."""
    require_user(request)
    try:
        d_bis = _dt.date.fromisoformat(bis) if bis else _dt.date.today() - _dt.timedelta(days=1)
        d_von = _dt.date.fromisoformat(von) if von else d_bis - _dt.timedelta(days=6)
    except ValueError:
        raise HTTPException(400, "von/bis as YYYY-MM-DD.")
    posts = {}
    for j in BLOG_DIR.glob("*.json"):
        try:
            rec = json.loads(j.read_text(encoding="utf-8"))
            posts[j.stem] = (rec.get("titel") or j.stem, _eintrag_ausgabe(rec))
        except (ValueError, OSError):
            continue
    leer = lambda: {"views": 0, "besucher": 0, "plays": 0, "p100": 0, "react": 0}  # noqa: E731
    z = {k: leer() for k in _ausgaben()}
    top, quellen = {}, {}
    con = sqlite3.connect(DATA / "metrics.db", timeout=10)
    try:
        rows = con.execute("SELECT day, kind, value, post, source, COUNT(*), COUNT(DISTINCT visitor) FROM events "
                           "WHERE day >= ? AND day <= ? GROUP BY day, kind, value, post, source",
                           (d_von.isoformat(), d_bis.isoformat())).fetchall()
    finally:
        con.close()
    for day, kind, value, post, source, n, uniq in rows:
        key = "de" if post == "index" else posts.get(post, ("", "de"))[1]
        e = z.setdefault(key, leer())
        if kind == "view":
            e["views"] += n
            e["besucher"] += uniq
            quellen[source or "direkt"] = quellen.get(source or "direkt", 0) + uniq
            if post != "index":
                top[post] = top.get(post, 0) + uniq
        elif kind == "play":
            e["plays"] += uniq
        elif kind == "progress" and value == "100":
            e["p100"] += uniq
        elif kind == "react":
            e["react"] += uniq
    nl = {}
    try:
        import newsletter
        nl = newsletter.status()
    except Exception:  # noqa: BLE001
        pass
    return {"von": d_von.isoformat(), "bis": d_bis.isoformat(), "ausgaben": z,
            "top": [{"id": pid, "titel": posts.get(pid, (pid, "de"))[0], "ausgabe": posts.get(pid, ("", "de"))[1],
                     "besucher": n, "url": _public_url(pid, posts.get(pid, ("", "de"))[1])}
                    for pid, n in sorted(top.items(), key=lambda x: -x[1])[:10]],
            "quellen": sorted(quellen.items(), key=lambda x: -x[1])[:5], "newsletter": nl}


@app.post("/api/metrics/event")
def metrics_event(req: EventReq, request: Request):
    if req.kind not in _EVENT_KINDS:
        raise HTTPException(400, "Invalid.")
    post = req.post.strip()[:40]
    if post != "index" and (any(c in post for c in ("..", "/", "\\"))
                            or not (BLOG_DIR / (post + ".json")).is_file()):
        raise HTTPException(400, "Invalid.")
    value = req.value.strip()[:20]
    if (req.kind == "react" and value not in _REACTIONS) or \
       (req.kind == "progress" and value not in _PROGRESS):
        raise HTTPException(400, "Invalid.")
    # Kennung = utm_source, Format "plattform-konto" (z. B. facebook-hauptseite); Plattform = Teil vor dem ersten "-"
    source = re.sub(r"[^a-z0-9_-]", "", req.source.lower())[:40]
    vis, day = _visitor(request), _dt.date.today().isoformat()
    c = _ev_counter.get(vis)
    if not c or c[0] != day:
        c = _ev_counter[vis] = [day, 0]
    c[1] += 1
    if c[1] > 300:
        raise HTTPException(429, "Too many events.")
    if len(_ev_counter) > 5000:
        for k in [k for k, v in _ev_counter.items() if v[0] != day]:
            _ev_counter.pop(k, None)
    con = _mdb()
    try:
        con.execute("INSERT INTO events(ts, day, kind, post, value, source, visitor) VALUES (?,?,?,?,?,?,?)",
                    (_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"), day,
                     req.kind, post, value, source, vis))
        con.commit()
    finally:
        con.close()
    return {"ok": True}


def _reaction_counts() -> dict:
    con = _mdb()
    try:
        rows = con.execute("SELECT post, value, COUNT(DISTINCT visitor) FROM events "
                           "WHERE kind='react' GROUP BY post, value").fetchall()
    finally:
        con.close()
    out: dict = {}
    for p, v, n in rows:
        out.setdefault(p, {})[v] = n
    return out


@app.get("/api/goals")
def goals(request: Request, weeks: int = 6):
    require_user(request)
    weeks = max(1, min(26, int(weeks)))
    since = (_dt.date.today() - _dt.timedelta(days=7 * weeks)).isoformat()
    con = _mdb()
    try:
        rows = con.execute("SELECT day, kind, value, post, source, COUNT(*), COUNT(DISTINCT visitor) "
                           "FROM events WHERE day>=? GROUP BY day, kind, value, post, source",
                           (since,)).fetchall()
        social = con.execute("SELECT week, platform, reach, views, followers, shares, pos, neg, notes "
                             "FROM social_weekly WHERE week>=? ORDER BY week DESC, platform",
                             (_week_key(since),)).fetchall()
    finally:
        con.close()
    weekly: dict = {}
    posts: dict = {}
    sources: dict = {}

    def wk(day):
        return weekly.setdefault(_week_key(day), {"week": _week_key(day), "visits": 0, "views": 0,
                                                  "plays": 0, "p50": 0, "p100": 0,
                                                  "neu": 0, "mut": 0, "teilen": 0})
    for day, kind, value, post, source, n, uniq in rows:
        w = wk(day)
        if kind == "view":
            w["visits"] += uniq
            w["views"] += n
            sources[source or "direkt"] = sources.get(source or "direkt", 0) + uniq
        if post != "index":
            p = posts.setdefault(post, {"id": post, "seen": 0, "plays": 0, "p50": 0, "p100": 0,
                                        "neu": 0, "mut": 0, "teilen": 0})
        if kind == "seen":
            p["seen"] += uniq
        elif kind == "play":
            w["plays"] += uniq
            p["plays"] += uniq
        elif kind == "progress" and value in ("50", "100"):
            w["p" + value] += uniq
            p["p" + value] += uniq
        elif kind == "react" and value in _REACTIONS:
            w[value] += uniq
            p[value] += uniq
    meta = {e["id"]: e for e in _blog_entries(None)}
    for pid, p in posts.items():
        m = meta.get(pid, {})
        p.update(titel=m.get("titel", pid), ressort=m.get("ressort", ""), avatar=m.get("avatar", ""),
                 konstruktiv=(m.get("konstruktiv") or {}).get("score"))
    # Ziel "konstruktiv": Anteil der im Fenster veroeffentlichten Beitraege mit Score >= 60
    scored = [e for e in meta.values() if str(e.get("published_at", ""))[:10] >= since
              and isinstance(e.get("konstruktiv"), dict)]
    k_share = round(100 * sum(1 for e in scored if e["konstruktiv"].get("score", 0) >= 60) / len(scored)) \
        if scored else None
    k_avg = round(sum(e["konstruktiv"].get("score", 0) for e in scored) / len(scored)) if scored else None
    platforms: dict = {}
    for s, n in sources.items():
        platforms[s.split("-", 1)[0]] = platforms.get(s.split("-", 1)[0], 0) + n
    return {"weeks": sorted(weekly.values(), key=lambda w: w["week"], reverse=True),
            "posts": sorted(posts.values(), key=lambda p: p["seen"], reverse=True)[:30],
            "sources": sources, "platforms": platforms,
            "konstruktiv": {"share60": k_share, "avg": k_avg, "n": len(scored),
                            "published": sum(1 for e in meta.values()
                                             if str(e.get("published_at", ""))[:10] >= since)},
            "social": [dict(zip(("week", "platform", "reach", "views", "followers", "shares",
                                 "pos", "neg", "notes"), r)) for r in social],
            "current_week": _week_key(_dt.date.today().isoformat())}


_meta_lock = threading.Lock()


@app.get("/api/goals/meta")
def goals_meta(request: Request, weeks: int = 8):
    """Facebook-Werte aus meta_insights (automatisch): Wochen je Seite + Top-Beitraege."""
    require_user(request)
    import meta_insights
    weeks = max(1, min(26, int(weeks)))
    since = (_dt.date.today() - _dt.timedelta(days=7 * weeks)).isoformat()
    st = meta_insights.status()
    configured = bool(st.get("pages"))
    con = _mdb()
    try:
        daily = con.execute("SELECT day, page, metric, value FROM meta_daily WHERE day>=?",
                            (since,)).fetchall() if configured else []
        posts = con.execute("SELECT id, page, created, kind, message, permalink, metric, value "
                            "FROM meta_posts WHERE created>=?", (since,)).fetchall() if configured else []
    except sqlite3.Error:
        daily, posts = [], []
    finally:
        con.close()
    wk: dict = {}
    for day, page, metric, value in daily:
        w = wk.setdefault((_week_key(day), page), {"week": _week_key(day), "page": page, "views": 0.0,
                                                    "viewers_days": [], "engagements": 0.0,
                                                    "video_views": 0.0, "follows": None,
                                                    "follows_net": 0.0})
        if metric in ("page_media_view", "page_total_media_view"):
            w["views"] += value
        elif metric == "page_total_media_view_unique":
            w["viewers_days"].append(value)
        elif metric == "page_post_engagements":
            w["engagements"] += value
        elif metric == "page_video_views":
            w["video_views"] += value
        elif metric == "page_follows":
            w["follows"] = value
        elif metric == "page_daily_follows":
            w["follows_net"] += value
        elif metric == "page_daily_unfollows":
            w["follows_net"] -= value
    rows = []
    for w in wk.values():
        vd = w.pop("viewers_days")
        w["viewers_avg"] = round(sum(vd) / len(vd)) if vd else None
        for k in ("views", "engagements", "video_views", "follows_net"):
            w[k] = int(w[k])
        w["follows"] = int(w["follows"]) if w["follows"] is not None else None
        rows.append(w)
    rows.sort(key=lambda r: (r["week"], r["page"]), reverse=True)
    pm: dict = {}
    for pid, page, created, kind, message, permalink, metric, value in posts:
        p = pm.setdefault(pid, {"id": pid, "page": page, "created": created[:10], "kind": kind,
                                "message": message, "permalink": permalink, "views": 0.0,
                                "pos": 0.0, "neg": 0.0, "clicks": 0.0})
        if metric in ("post_media_view", "blue_reels_play_count", "fb_reels_total_plays",
                      "post_video_views"):
            p["views"] = max(p["views"], value)
        elif metric.startswith("post_reactions_by_type_total."):
            typ = metric.rsplit(".", 1)[1]
            if typ in meta_insights.POSITIVE:
                p["pos"] += value
            elif typ in ("anger", "angry"):
                p["neg"] += value
        elif metric == "post_clicks":
            p["clicks"] = value
    top = sorted(pm.values(), key=lambda p: p["views"], reverse=True)[:15]
    for p in top:
        for k in ("views", "pos", "neg", "clicks"):
            p[k] = int(p[k])
    return {"configured": configured, "status": st, "weeks": rows, "top_posts": top}


@app.post("/api/goals/meta/refresh")
def goals_meta_refresh(request: Request):
    u = require_user(request)
    if u["role"] != "admin":
        raise HTTPException(403, "Admin only.")
    import meta_insights
    if not meta_insights.status().get("pages"):
        raise HTTPException(400, "Meta not configured yet (data/meta_pages.json).")
    if not _meta_lock.acquire(blocking=False):
        raise HTTPException(409, "Refresh already running.")

    def _go():
        try:
            meta_insights.pull()
        finally:
            _meta_lock.release()
    threading.Thread(target=_go, daemon=True).start()
    return {"ok": True}


class SocialReq(BaseModel):
    week: str
    platform: str
    reach: int = 0
    views: int = 0
    followers: int = 0
    shares: int = 0
    pos: int = 0
    neg: int = 0
    notes: str = ""


@app.post("/api/goals/social")
def goals_social(req: SocialReq, request: Request):
    u = require_user(request)
    if not re.fullmatch(r"\d{4}-W\d{2}", req.week) or req.platform not in _PLATFORMS:
        raise HTTPException(400, "Invalid week or platform.")
    vals = [max(0, int(v)) for v in (req.reach, req.views, req.followers, req.shares, req.pos, req.neg)]
    con = _mdb()
    try:
        con.execute("INSERT OR REPLACE INTO social_weekly(week, platform, reach, views, followers, "
                    "shares, pos, neg, notes, uid, at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (req.week, req.platform, *vals, req.notes.strip()[:300], u["id"],
                     _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")))
        con.commit()
    finally:
        con.close()
    return {"ok": True}


# ------------------------------ Blog (öffentliches Testgelände) -----
# Interner Redaktions-Blog unter /news: veröffentlichte Videos + Skripttext als
# Artikel. Bewusst noindex (nur per Link teilen), keine Marke, kein CMS.
BLOG_DIR = DATA / "blog"


_BLOG_CACHE: dict = {"sig": None, "alle": []}
_BLOG_LOCK = threading.Lock()
_SUCHE = _suche_mod.Index(DATA / "suche.sqlite")


def _blog_alle() -> list:
    """Alle Beitraege aller Ausgaben, neueste zuerst, aus dem Speicher (31.10.2026, Bestandsaufnahme Punkt 7):
    neu gelesen wird nur, wenn sich im Blog-Ordner eine Datei geaendert hat (Name und Aenderungszeit der
    JSON-Dateien, Namen der Medien). Beim Neulesen wird der Suchindex (suche.py) nachgefuehrt.
    Die Liste ist geteilt; Aufrufer bekommen ueber _blog_entries() Kopien."""
    if not BLOG_DIR.is_dir():
        return []
    sig, namen = [], set()
    try:
        for e in os.scandir(BLOG_DIR):
            if e.name.endswith(".json"):
                try:
                    sig.append((e.name, e.stat().st_mtime_ns))
                except OSError:
                    continue
            else:
                namen.add(e.name)
    except OSError:
        return _BLOG_CACHE["alle"]
    sig.sort()
    schluessel = (tuple(sig), tuple(sorted(namen)))
    with _BLOG_LOCK:
        if _BLOG_CACHE["sig"] == schluessel:
            return _BLOG_CACHE["alle"]
    out, zeilen = [], []
    for name, mt in sig:
        try:
            rec = json.loads((BLOG_DIR / name).read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if isinstance(rec, dict) and rec.get("titel"):
            stem = name[:-5]
            rec["id"] = stem
            rec["video"] = (stem + ".mp4") in namen
            rec["poster"] = (stem + ".jpg") in namen
            rec["audio"] = (stem + ".mp3") in namen
            out.append(rec)
            zeilen.append((stem, mt, dict(rec, _ausgabe=_eintrag_ausgabe(rec))))
    out.sort(key=lambda r: str(r.get("published_at", "")), reverse=True)
    with _BLOG_LOCK:
        _BLOG_CACHE["sig"], _BLOG_CACHE["alle"] = schluessel, out
    try:
        _SUCHE.aktualisieren(zeilen)
    except Exception as e:  # noqa: BLE001  (der Index darf die Seite nie aufhalten)
        print(f"[suche] Index nicht aktualisiert: {type(e).__name__}: {e}", file=sys.stderr)
    return out


def _blog_entries(lang: str | None = "") -> list:
    """Beitraege der Ausgabe: lang "" = aktuelle Ausgabe (_LANG), None = alle, sonst der Schluessel
    einer Ausgabe (de, en, ...). Beitraege ohne `ausgabe` und ohne `sprache` gehoeren zur deutschen.
    Flache Kopien der Eintraege im Speicher (_blog_alle)."""
    if lang == "":
        lang = _LANG.get()
    return [dict(r) for r in _blog_alle() if not lang or _eintrag_ausgabe(r) == lang]


def _blog_entry(pid: str) -> dict | None:
    if any(c in pid for c in ("..", "/", "\\")) or not pid:
        return None
    j = BLOG_DIR / (pid + ".json")
    if not j.is_file():
        return None
    try:
        rec = json.loads(j.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    if not isinstance(rec, dict):
        return None
    rec["id"] = pid
    rec["video"] = (BLOG_DIR / (pid + ".mp4")).is_file()
    rec["poster"] = (BLOG_DIR / (pid + ".jpg")).is_file()
    rec["audio"] = (BLOG_DIR / (pid + ".mp3")).is_file()
    return rec


def _extract_audio(pid: str, video: Path) -> dict:
    """Tonspur des Beitrags als <pid>.mp3 fuer den Podcast (19.10.2026). Liefert {"audio_s": Sekunden}."""
    mp3 = BLOG_DIR / (pid + ".mp3")
    try:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vn", "-c:a", "libmp3lame",
                        "-b:a", "128k", "-ar", "44100", str(mp3)], capture_output=True, timeout=300)
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp3)],
                             capture_output=True, text=True, timeout=30)
        return {"audio_s": int(float(out.stdout.strip() or 0))}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        mp3.unlink(missing_ok=True)
        return {}


def _attach_video(pid: str, src: Path) -> dict:
    """Video in den Blog-Ordner kopieren, Vorschaubild ziehen, Ausrichtung merken."""
    BLOG_DIR.mkdir(parents=True, exist_ok=True)
    dst = BLOG_DIR / (pid + ".mp4")
    shutil.copyfile(src, dst)
    info = {"w": 0, "h": 0}
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height", "-of", "csv=p=0:s=x", str(dst)],
                             capture_output=True, text=True, timeout=30)
        w, h = out.stdout.strip().split("x")
        info = {"w": int(w), "h": int(h)}
    except (ValueError, OSError, subprocess.TimeoutExpired):
        pass
    try:
        subprocess.run(["ffmpeg", "-y", "-ss", "1", "-i", str(dst), "-frames:v", "1",
                        "-vf", "scale=720:-2", "-q:v", "4", str(BLOG_DIR / (pid + ".jpg"))],
                       capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        pass
    info.update(_extract_audio(pid, dst))
    # Karten der Grafikstrecke (twin_<job>_k1.png …) oeffentlich neben dem Beitrag ablegen
    # (<pid>_k1.png …, Postenstrecke: Instagram-Karussell) und alte entfernen
    for old in BLOG_DIR.glob(f"{pid}_k*.png"):
        old.unlink(missing_ok=True)
    n_karten = 0
    for i, k in enumerate(sorted(src.parent.glob(f"{src.stem}_k*.png")), 1):
        shutil.copyfile(k, BLOG_DIR / f"{pid}_k{i}.png")
        n_karten += 1
    info["karten"] = n_karten
    # 4:5-Fassung (1080 x 1350, Balken seitlich) fuer Kanaele, die kein 9:16 nehmen; im Hintergrund
    def _v45():
        try:
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(dst),
                            "-vf", "scale=759:1350,pad=1080:1350:(ow-iw)/2:0:black",
                            "-c:v", "libx264", "-crf", "20", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                            "-c:a", "copy", "-movflags", "+faststart", str(BLOG_DIR / f"{pid}_45.mp4")],
                           capture_output=True, timeout=900)
        except (OSError, subprocess.TimeoutExpired):
            pass
    (BLOG_DIR / f"{pid}_45.mp4").unlink(missing_ok=True)
    threading.Thread(target=_v45, daemon=True).start()
    return info


def _save_entry(pid: str, rec: dict):
    for k in ("id", "video", "poster"):
        rec.pop(k, None)
    BLOG_DIR.mkdir(parents=True, exist_ok=True)
    (BLOG_DIR / (pid + ".json")).write_text(json.dumps(rec, ensure_ascii=False, indent=2),
                                            encoding="utf-8")


def _nfc(s) -> str:
    return unicodedata.normalize("NFC", str(s or "")).strip()


class BlogReq(BaseModel):
    file: str


@app.post("/api/blog/publish")
def blog_publish(req: BlogReq, request: Request):
    u = require_user(request)
    if any(c in req.file for c in ("..", "/", "\\")) or not req.file.endswith(".mp4"):
        raise HTTPException(400, "Invalid.")
    src = user_dir(u["id"]) / "outputs" / req.file
    if not src.is_file():
        raise HTTPException(404, "Video not found.")
    rec = {}
    sc = src.with_suffix(".json")
    if sc.is_file():
        try:
            rec = json.loads(sc.read_text(encoding="utf-8"))
            if not isinstance(rec, dict):
                rec = {}
        except (ValueError, OSError):
            rec = {}
    titel = (rec.get("titel") or _load_json(_titles_file(u["id"])).get(req.file, "")).strip()
    if not titel:
        raise HTTPException(400, "Please set a title before publishing (title field when creating the video).")
    rec["titel"] = titel
    # Konstruktiv-Score bewusst NICHT hier: redaktionelles Urteil lebt im Newsroom (Björn),
    # Spezifikation siehe Desktop/Konstruktiv-Score Spezifikation.md.
    old = _blog_entry(src.stem) or {}
    if _nfc(rec.get("ressort", "")) not in _site()["ressorts"]:
        rec["ressort"] = old.get("ressort", "") if old.get("ressort") in _site()["ressorts"] else ""
    rec.update(published_at=old.get("published_at") or _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
               uid=old.get("uid", u["id"]), autor=old.get("autor", u["username"]),
               teaser=old.get("teaser", ""), text=old.get("text") or rec.get("skript", ""),
               quellen=old.get("quellen", []))
    rec.update(_attach_video(src.stem, src))
    _save_entry(src.stem, rec)
    _score_post_async(src.stem)
    return {"ok": True, "id": src.stem, "url": f"/news/{src.stem}"}


class PostReq(BaseModel):
    id: str = ""            # leer = neuer Beitrag
    titel: str
    teaser: str = ""
    text: str = ""
    ressort: str = ""
    quellen: list[str] = []
    video: str = ""         # Datei aus My videos; "" = kein Video bzw. bestehendes behalten
    remove_video: bool = False
    social_links: list[str] = []   # Zeilen: "URL" oder "Titel | URL"
    avatar: str | None = None      # Byline: Profilname/-slug oder Persona-Avatarname; None = unveraendert
    sprache: str | None = None     # alt: de oder en (en = Ausgabe en); None = bestehenden Wert behalten
    ausgabe: str | None = None     # Schluessel der Ausgabe (de, en, ...); hat Vorrang vor sprache
    # Spur des Beitrags (26.10.2026): "aktuell" = Nachmittagsausgabe nach Aktualitaet, auf der Seite mit
    # "Aktuell" statt des Konstruktiv-Scores gekennzeichnet; "" = keine Spur; None = unveraendert
    spur: str | None = None
    narrativ: str | None = None    # Narrativ des Natur-Scans (narrative.md der Kette); "" = keines; None = unveraendert
    korrektur: str | None = None   # haengt eine Korrektur (Datum heute, Text) an den Beitrag (26.10.2026); None = keine


@app.post("/api/videos/upload")
def videos_upload(request: Request, file: UploadFile = File(...), title: str = Form("")):
    """Fertiges Video (z. B. extern erstellter Avatar) direkt hochladen -> erscheint in My videos
    und kann im Blog-Editor gewaehlt werden. Wird auf H.264/AAC normalisiert (max 1080p)."""
    u = require_user(request)
    name = Path(file.filename or "video.mp4").name
    if Path(name).suffix.lower() not in VIDEO_EXT:
        raise HTTPException(400, "Please upload a video file (mp4, mov, webm, mkv).")
    out_dir = user_dir(u["id"]) / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / f"upload_{uuid.uuid4().hex[:8]}{Path(name).suffix.lower()}"
    size = 0
    try:
        with tmp.open("wb") as f:
            while True:
                chunk = file.file.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > _CLIP_SRC_CAP:
                    raise HTTPException(413, "File too large (max 500 MB).")
                f.write(chunk)
        dest = out_dir / f"twin_up{uuid.uuid4().hex[:8]}.mp4"
        p = subprocess.run(["ffmpeg", "-y", "-i", str(tmp),
                            "-vf", "scale='min(1080,iw)':-2", "-c:v", "libx264", "-crf", "20",
                            "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                            "-movflags", "+faststart", str(dest)],
                           capture_output=True, text=True, timeout=1200)
        if p.returncode != 0 or not dest.is_file():
            dest.unlink(missing_ok=True)
            raise HTTPException(400, "Could not read this video.")
    finally:
        tmp.unlink(missing_ok=True)
    _save_title(u["id"], dest.name, (title or Path(name).stem)[:80])
    return {"ok": True, "file": dest.name, "url": f"/media/{dest.name}",
            "seconds": _video_seconds(dest)}


@app.get("/api/blog/posts")
def blog_posts(request: Request, lang: str = "de", ausgabe: str = ""):
    """ausgabe (oder lang): Schluessel der Ausgabe (de, en, ...) oder alle."""
    u = require_user(request)
    key = ausgabe or lang
    ents = _blog_entries(None if key == "alle" else _ausgabe_key(key))
    return {"posts": [{"id": e["id"], "titel": e.get("titel", ""), "teaser": e.get("teaser", ""),
                       "text": e.get("text", ""), "ressort": e.get("ressort", ""),
                       "quellen": e.get("quellen", []), "avatar": e.get("avatar", ""),
                       "video": e["video"], "published_at": e.get("published_at", ""),
                       "autor": e.get("autor", ""), "w": e.get("w", 0), "h": e.get("h", 0),
                       "karten": int(e.get("karten") or 0),           # oeffentlich: /media/<id>_k<n>.png
                       "video45": (BLOG_DIR / f"{e['id']}_45.mp4").is_file(),   # /media/<id>_45.mp4
                       "konstruktiv": e.get("konstruktiv"), "social": e.get("social", []),
                       # Herkunftsregister (30.10.2026): Eintraege und Kennzeichen des Beitrags, fuer die Kette
                       "register": e.get("register") if isinstance(e.get("register"), dict) else None,
                       "spur": e.get("spur") or "", "narrativ": e.get("narrativ") or "",
                       "mine": e.get("uid") == u["id"] or _is_editor(u)} for e in ents]}


@app.post("/api/blog/post")
def blog_post_save(req: PostReq, request: Request):
    u = require_user(request)
    titel = _nfc(req.titel)[:120]
    if not titel:
        raise HTTPException(400, "Title is required.")
    quellen = [_nfc(q)[:300] for q in req.quellen if _nfc(q)]
    if any(not re.match(r"https?://", q) for q in quellen) or len(quellen) > 10:
        raise HTTPException(400, "Sources must be http(s) links (max 10).")
    if req.id:
        rec = _blog_entry(req.id)
        if not rec:
            raise HTTPException(404, "Post not found.")
        if rec.get("uid") != u["id"] and not _is_editor(u):
            raise HTTPException(403, "Only the author or an admin can edit this post.")
        pid = req.id
    else:
        pid = "p_" + uuid.uuid4().hex[:10]
        rec = {"published_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
               "uid": u["id"], "autor": u["username"], "herkunft": "persona"}
    # Die Ausgabe entscheidet, gegen welche Ressortliste geprueft wird und wo der Beitrag erscheint
    key = (req.ausgabe or "").strip().lower()[:16]
    if not key:
        sp = (req.sprache or "").strip().lower()[:5]
        key = ("en" if sp == "en" else "de") if sp else (rec.get("ausgabe") or _eintrag_ausgabe(rec))
    if key not in _ausgaben():
        raise HTTPException(400, "Unknown edition (ausgabe).")
    lang = key
    rec["ausgabe"] = key
    rec["sprache"] = _ausgabe(key)["sprache"]
    ressort = _nfc(req.ressort)
    with _lang(key):
        if ressort and ressort not in _site()["ressorts"]:
            raise HTTPException(400, "Invalid Ressort.")
    # Score nur neu berechnen, wenn sich der Text aendert (Kette: Video/Social-Links anhaengen
    # laesst Datum und Score unveraendert)
    text_neu = (titel, _nfc(req.teaser)[:300], _nfc(req.text)[:20000])
    text_geaendert = (rec.get("titel"), rec.get("teaser", ""), rec.get("text", "")) != text_neu or not rec.get("konstruktiv")
    rec.update(titel=titel, teaser=_nfc(req.teaser)[:300], text=_nfc(req.text)[:20000],
               ressort=ressort, quellen=quellen,
               updated_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"))
    if req.avatar is not None:
        av = _nfc(req.avatar)[:80]
        prof = _profile(av) or _profile_for_avatar(av)
        rec["avatar"] = prof["name"] if prof else av
    for feld, breite in (("spur", 20), ("narrativ", 60)):
        wert = getattr(req, feld)
        if wert is None:
            continue
        wert = _nfc(wert).strip()[:breite]
        if feld == "spur" and wert and wert != "aktuell":
            raise HTTPException(400, "Unknown spur (allowed: aktuell).")
        if wert:
            rec[feld] = wert
        else:
            rec.pop(feld, None)
    if req.korrektur is not None and _nfc(req.korrektur):
        ks = [k for k in (rec.get("korrekturen") or []) if isinstance(k, dict)]
        ks.append({"datum": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"), "text": _nfc(req.korrektur)[:1000],
                   "von": u["username"]})
        rec["korrekturen"] = ks[-20:]
    old_social = {s.get("url"): s for s in (rec.get("social") or []) if isinstance(s, dict)}
    parsed = _parse_social_lines(req.social_links)
    BLOG_DIR.mkdir(parents=True, exist_ok=True)
    rec["social"] = [_social_enrich(pid, i, url, label, old_social.get(url)) for i, (label, url) in enumerate(parsed)]
    keep = {s.get("thumb") for s in rec["social"] if s.get("thumb")}
    for f in BLOG_DIR.glob(f"{pid}_s*.jpg"):   # verwaiste Vorschaubilder entfernen
        if f.name not in keep:
            f.unlink(missing_ok=True)
    if req.remove_video:
        for ext in (".mp4", ".jpg"):
            (BLOG_DIR / (pid + ext)).unlink(missing_ok=True)
        rec.pop("w", None); rec.pop("h", None); rec.pop("avatar", None)
    elif req.video:
        if any(c in req.video for c in ("..", "/", "\\")) or not req.video.endswith(".mp4"):
            raise HTTPException(400, "Invalid video.")
        src = user_dir(u["id"]) / "outputs" / req.video
        if not src.is_file():
            raise HTTPException(404, "Video not found.")
        sc = src.with_suffix(".json")
        kennz = ""
        if sc.is_file():   # Avatar/Skript aus der Begleitdatei uebernehmen
            try:
                side = json.loads(sc.read_text(encoding="utf-8"))
                if isinstance(side, dict):
                    rec["avatar"] = side.get("avatar", rec.get("avatar", ""))
                    rec["skript"] = side.get("skript", rec.get("skript", ""))
                    kennz = str(side.get("kennzeichen") or "")
            except (ValueError, OSError):
                pass
        rec.update(_attach_video(pid, src))
        # Herkunftsregister (30.10.2026): Content Credentials in die oeffentliche Datei, Eintrag mit Hashes
        # und Signatur fuer Video und Karten, dazu das eingebrannte Kennzeichen; ein Fehler hier haelt die
        # Veroeffentlichung nie auf
        try:
            import herkunft
            rec["register"] = herkunft.beitrag_registrieren(pid, rec, _ausgabe_home_abs(lang).rstrip("/"), kennz)
        except Exception as e:  # noqa: BLE001
            print(f"[herkunft] {pid}: {type(e).__name__}: {e}", flush=True)
    _save_entry(pid, rec)
    if text_geaendert:
        _score_post_async(pid)
    indexnow_submit([_public_url(pid, lang), _ausgabe_home_abs(lang)])
    return {"ok": True, "id": pid, "url": f"/news{_ausgabe(lang).get('prefix') or ''}/{pid}",
            "public_url": _public_url(pid, lang)}


@app.post("/api/blog/unpublish")
def blog_unpublish(req: BlogReq, request: Request):
    u = require_user(request)
    stem = req.file[:-4] if req.file.endswith(".mp4") else req.file
    if any(c in stem for c in ("..", "/", "\\")):
        raise HTTPException(400, "Invalid.")
    j = BLOG_DIR / (stem + ".json")
    if not j.is_file():
        raise HTTPException(404, "Not published.")
    try:
        rec = json.loads(j.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        rec = {}
    if rec.get("uid") != u["id"] and not _is_editor(u):
        raise HTTPException(403, "Only the author or an admin can unpublish.")
    j.unlink(missing_ok=True)
    for ext in (".mp4", ".jpg"):
        (BLOG_DIR / (stem + ext)).unlink(missing_ok=True)
    for f in BLOG_DIR.glob(f"{stem}_s*.jpg"):
        f.unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/blog/mine")
def blog_mine(request: Request):
    u = require_user(request)
    return {"published": [e["id"] for e in _blog_entries(None)
                          if e.get("uid") == u["id"] or _is_editor(u)]}


@app.get("/news/media/{name}")
def news_media(name: str):
    if any(c in name for c in ("..", "/", "\\")) or not name.endswith((".mp4", ".jpg", ".png", ".mp3")):
        raise HTTPException(400, "Invalid.")
    p = BLOG_DIR / name
    if not p.is_file():
        raise HTTPException(404, "Nicht gefunden.")
    mt = {"mp4": "video/mp4", "jpg": "image/jpeg", "png": "image/png", "mp3": "audio/mpeg"}[name.rsplit(".", 1)[1]]
    return FileResponse(p, media_type=mt, headers={"X-Robots-Tag": "noindex, nofollow"})


# ------------------------------ Avatar-Profile (Team-Seite) -----------
# Steckbriefe der Avatare fuer die Website: Foto, Kurzbeschreibung, Text, Ressorts,
# Verknuepfung zum Persona-Avatar (fuer "Beitraege von …"). Daten: data/profiles/<slug>.json + .jpg
PROFILES_DIR = DATA / "profiles"


def _profiles(lang: str | None = "") -> list:
    """Teamprofile; `lang` filtert nach der Ausgabe des Profils (Feld `ausgabe`, sonst `sprache` en = en,
    sonst de): "" = aktuelle Ausgabe (_LANG), None = alle. Jede Ausgabe hat eigene Profile."""
    if lang == "":
        lang = _LANG.get()
    out = []
    if PROFILES_DIR.is_dir():
        for j in PROFILES_DIR.glob("*.json"):
            try:
                rec = json.loads(j.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if lang and isinstance(rec, dict) and _eintrag_ausgabe(rec) != lang:
                continue
            if isinstance(rec, dict) and rec.get("name"):
                rec["slug"] = j.stem
                rec["photo"] = (PROFILES_DIR / (j.stem + ".jpg")).is_file()
                out.append(rec)
    out.sort(key=lambda r: (int(r.get("order") or 0), str(r.get("name", "")).lower()))
    return out


def _profile(slug: str) -> dict | None:
    if not slug or any(c in slug for c in ("..", "/", "\\")):
        return None
    j = PROFILES_DIR / (slug + ".json")
    if not j.is_file():
        return None
    try:
        rec = json.loads(j.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    rec["slug"] = slug
    rec["photo"] = (PROFILES_DIR / (slug + ".jpg")).is_file()
    return rec


def _profile_for_avatar(avatar: str, profiles: list | None = None) -> dict | None:
    """Profil zum Persona-Avatarnamen eines Beitrags (Feld 'avatar' im Beitrag)."""
    a = _nfc(avatar).lower()
    if not a:
        return None
    for p in (profiles if profiles is not None else _profiles(None)):
        names = {_nfc(p.get("name", "")).lower()} | {_nfc(x).lower() for x in (p.get("persona_avatare") or [])}
        if a in names:
            return p
    return None


class ProfileReq(BaseModel):
    slug: str = ""              # leer = neu (aus Name)
    name: str
    kurz: str = ""              # eine Zeile, z. B. "Spricht Politik und Wirtschaft"
    text: str = ""              # Steckbrief (Leerzeile = Absatz)
    ressorts: list[str] = []
    persona_avatare: list[str] = []   # Persona-Avatarnamen, deren Videos diesem Profil gehoeren
    order: int = 0
    sichtbar: bool = True
    fiktiv: bool | None = None   # erfundene Figur (KI-Avatar ohne reale Person); None = Wert behalten
    ausgabe: str | None = None   # Schluessel der Ausgabe (de, en, ...); None = bestehenden Wert behalten (neu: de)


@app.get("/api/profiles")
def profiles_list(request: Request, lang: str = "de", ausgabe: str = ""):
    """ausgabe (oder lang): Schluessel der Ausgabe (de, en, ...) oder alle."""
    require_user(request)
    key = ausgabe or lang
    return {"profiles": _profiles(None if key == "alle" else _ausgabe_key(key))}


@app.post("/api/profiles")
def profiles_save(req: ProfileReq, request: Request):
    u = require_user(request)
    name = _nfc(req.name)[:60]
    if not name:
        raise HTTPException(400, "Name is required.")
    slug = req.slug.strip() or _slug(name)
    if not slug or any(c in slug for c in ("..", "/", "\\")):
        raise HTTPException(400, "Invalid name.")
    old = _profile(slug) or {}
    if old and old.get("uid") not in (None, u["id"]) and not _is_editor(u):
        raise HTTPException(403, "Only the creator or an admin can edit this profile.")
    key = (req.ausgabe or "").strip().lower()[:16] or (_eintrag_ausgabe(old) if old else "de")
    if key not in _ausgaben():
        raise HTTPException(400, "Unknown edition (ausgabe).")
    with _lang(key):
        site = _site()
    ressorts = [_nfc(r) for r in req.ressorts if _nfc(r) in site["ressorts"]]
    # Felder, die nicht ueber dieses Formular kommen (Stimmen, HeyGen-Asset, Ensemble, Bauchbinde), bleiben erhalten
    rec = dict(old)
    rec.update({"name": name, "kurz": _nfc(req.kurz)[:120], "text": _nfc(req.text)[:6000], "ressorts": ressorts,
                "persona_avatare": [_nfc(a)[:60] for a in req.persona_avatare if _nfc(a)][:10],
                "order": max(0, min(999, int(req.order))), "sichtbar": bool(req.sichtbar),
                "fiktiv": bool(old.get("fiktiv", False)) if req.fiktiv is None else bool(req.fiktiv),
                "ausgabe": key, "sprache": _ausgabe(key)["sprache"],
                "uid": old.get("uid", u["id"]), "autor": old.get("autor", u["username"]),
                "created_at": old.get("created_at") or _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")})
    rec.pop("slug", None); rec.pop("photo", None)
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    (PROFILES_DIR / (slug + ".json")).write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "slug": slug, "url": f"/news{_ausgabe(key).get('prefix') or ''}/team/{slug}"}


@app.delete("/api/profiles/{slug}")
def profiles_delete(slug: str, request: Request):
    u = require_user(request)
    p = _profile(slug)
    if not p:
        raise HTTPException(404, "Profile not found.")
    if p.get("uid") not in (None, u["id"]) and not _is_editor(u):
        raise HTTPException(403, "Only the creator or an admin can delete this profile.")
    for ext in (".json", ".jpg"):
        (PROFILES_DIR / (slug + ext)).unlink(missing_ok=True)
    return {"ok": True}


def _save_profile_photo(slug: str, src: Path):
    """Foto als quadratisches JPG (720 px, oberes Drittel bevorzugt, da Gesichter oben sitzen)."""
    from PIL import Image, ImageOps
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        w, h = im.size
        s = min(w, h)
        top = 0 if h > w else 0
        left = (w - s) // 2
        im = im.crop((left, top, left + s, top + s)).resize((720, 720), Image.LANCZOS)
        PROFILES_DIR.mkdir(parents=True, exist_ok=True)
        im.save(PROFILES_DIR / (slug + ".jpg"), "JPEG", quality=88)


@app.post("/api/profiles/photo")
def profiles_photo(request: Request, slug: str = Form(...), file: UploadFile = File(...)):
    u = require_user(request)
    p = _profile(slug)
    if not p:
        raise HTTPException(404, "Profile not found. Save the profile first.")
    if p.get("uid") not in (None, u["id"]) and not _is_editor(u):
        raise HTTPException(403, "Not your profile.")
    if Path(file.filename or "").suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
        raise HTTPException(400, "Please upload a JPG, PNG or WebP image.")
    tmp = PROFILES_DIR / f"tmp_{uuid.uuid4().hex[:8]}"
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        data = file.file.read(15 * 1024 * 1024 + 1)
        if len(data) > 15 * 1024 * 1024:
            raise HTTPException(413, "Image too large (max 15 MB).")
        tmp.write_bytes(data)
        try:
            _save_profile_photo(slug, tmp)
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "Could not read this image.")
    finally:
        tmp.unlink(missing_ok=True)
    return {"ok": True}


class ProfileStillReq(BaseModel):
    slug: str
    preset: str


@app.post("/api/profiles/photo-from-avatar")
def profiles_photo_from_avatar(req: ProfileStillReq, request: Request):
    """Standbild aus dem eigenen Avatar-Video als Profilfoto (Sekunde 1)."""
    u = require_user(request)
    p = _profile(req.slug)
    if not p:
        raise HTTPException(404, "Profile not found. Save the profile first.")
    if req.preset not in user_avatars(u["id"]):
        raise HTTPException(400, "Avatar not found in your account.")
    pdir = user_dir(u["id"]) / "presets"
    vid = next((x for x in pdir.iterdir() if x.stem == req.preset and x.suffix.lower() in VIDEO_EXT), None)
    if not vid:
        raise HTTPException(404, "Avatar video not found.")
    tmp = PROFILES_DIR / f"tmp_{uuid.uuid4().hex[:8]}.jpg"
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(["ffmpeg", "-y", "-ss", "1", "-i", str(vid), "-frames:v", "1", "-q:v", "3", str(tmp)],
                           capture_output=True, timeout=60)
        if r.returncode != 0 or not tmp.is_file():
            raise HTTPException(400, "Could not extract a still frame.")
        _save_profile_photo(req.slug, tmp)
    finally:
        tmp.unlink(missing_ok=True)
    return {"ok": True}


@app.get("/news/media/avatar/{name}")
def news_profile_photo(name: str):
    if any(c in name for c in ("..", "/", "\\")) or not name.endswith(".jpg"):
        raise HTTPException(400, "Invalid.")
    p = PROFILES_DIR / name
    if not p.is_file():
        raise HTTPException(404, "Nicht gefunden.")
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


_PROFILE_CSS = """
.team{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
.prof{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:18px;text-align:center}
.prof img,.prof .ph{width:120px;height:120px;border-radius:50%;object-fit:cover;display:block;margin:0 auto 12px;background:var(--accent-soft)}
.prof .ph{display:flex;align-items:center;justify-content:center;font-family:"Source Serif 4",Georgia,serif;font-size:40px;color:var(--accent-dark)}
.prof h3{font-family:"Source Serif 4",Georgia,serif;font-size:19px;margin:0 0 4px}
.prof h3 a{color:inherit;text-decoration:none}
.prof .kurz{color:var(--muted);font-size:13.5px;margin:0 0 6px}
.prof .fik{font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:var(--faint);margin:0}
.fiknote{font-size:13px;color:var(--muted);background:var(--accent-soft);border-radius:10px;padding:10px 14px;margin:10px 0 4px;max-width:78ch}
.prof .meta{justify-content:center;margin:0}
.profile{display:grid;grid-template-columns:220px 1fr;gap:28px;background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:28px 30px;margin-bottom:22px}
.profile img,.profile .ph{width:200px;height:200px;border-radius:50%;object-fit:cover;background:var(--accent-soft)}
.profile .ph{display:flex;align-items:center;justify-content:center;font-family:"Source Serif 4",Georgia,serif;font-size:64px;color:var(--accent-dark)}
.profile h1{margin-bottom:2px}
.profile .kurz{color:var(--muted);margin:0 0 12px;font-size:15px}
.speakers{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;margin:-8px 0 20px;font-size:13.5px;color:var(--muted)}
.speakers a{display:inline-flex;align-items:center;gap:8px;color:var(--ink);text-decoration:none}
.speakers img,.speakers .ph{width:34px;height:34px;border-radius:50%;object-fit:cover;background:var(--accent-soft)}
.speakers .ph{display:inline-flex;align-items:center;justify-content:center;font-size:15px;color:var(--accent-dark)}
@media(max-width:900px){.team{grid-template-columns:repeat(2,1fr)}}
@media(max-width:620px){.team{grid-template-columns:1fr}.profile{grid-template-columns:1fr;text-align:center}.profile img,.profile .ph{margin:0 auto}}
"""


def _avatar_img(p: dict, P: str, cls: str = "") -> str:
    esc = _htmlmod.escape
    if p.get("photo"):
        try:
            v = int((PROFILES_DIR / (p["slug"] + ".jpg")).stat().st_mtime)
        except OSError:
            v = 0
        return f'<img class="{cls}" src="{P}/media/avatar/{esc(p["slug"])}.jpg?v={v}" alt="{esc(p["name"])}" loading="lazy">'
    return f'<div class="ph {cls}">{esc(p["name"][:1].upper())}</div>'


def _speakers_html(profiles: list, P: str) -> str:
    if not profiles:
        return ""
    esc = _htmlmod.escape
    compact = len(profiles) > 8
    return (f'<div class="speakers"><span>{_t("speakers")}</span>' + "".join(
        f'<a href="{P}/team/{esc(p["slug"])}" title="{esc(p["name"])}, {esc(p.get("kurz", ""))}">{_avatar_img(p, P)}{"" if compact else esc(p["name"])}</a>'
        for p in profiles) + (f'<a href="{P}/{_t("p_about")}" style="color:var(--muted)">{_t("all_profiles")}</a>' if compact else "") + "</div>")


def _prof_card(p: dict, P: str) -> str:
    esc = _htmlmod.escape
    fik = f'<p class="fik">{_t("fictional")}</p>' if p.get("fiktiv") else ""
    return (f'<div class="prof">{_avatar_img(p, P)}<h3><a href="{P}/team/{esc(p["slug"])}">{esc(p["name"])}</a></h3>'
            f'<p class="kurz">{esc(p.get("kurz", ""))}</p>{fik}</div>')


def _team_grid_html(P: str) -> str:
    """Profile auf 'Ueber uns', gruppiert nach dem ersten Ressort jeder Figur (Reihenfolge = Menue)."""
    esc = _htmlmod.escape
    profs = [p for p in _profiles() if p.get("sichtbar", True)]
    if not profs:
        return ""
    site = _site()
    out, used = [], set()
    for r in site["ressorts"]:
        grp = [p for p in profs if (p.get("ressorts") or [None])[0] == r]
        if not grp:
            continue
        used.update(p["slug"] for p in grp)
        out.append(f'<div class="rowhead"><h2>{esc(r)}</h2><a href="{P}/{_t("p_sec")}/{_slug(r)}">{_t("to_section")}</a></div>'
                   f'<div class="team">{"".join(_prof_card(p, P) for p in grp)}</div>')
    rest = [p for p in profs if p["slug"] not in used]
    if rest:
        out.append(f'<div class="rowhead"><h2>{_t("others")}</h2></div><div class="team">{"".join(_prof_card(p, P) for p in rest)}</div>')
    note = ""
    if any(p.get("fiktiv") for p in profs):
        note = f'<p class="fiknote">{_t("fiknote")}</p>'
    return note + "".join(out)


@app.get("/news/team")
def news_team(request: Request):
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"{_prefix(request)}/{_t('p_about')}", status_code=301)


@app.get("/news/team/{slug}")
def news_profile(slug: str, request: Request):
    p = _profile(slug)
    if p and not p.get("sichtbar", True) and _eintrag_ausgabe(p) == _LANG.get():
        # ein ausgeblendetes Profil (die 59 alten Ensembles seit 18.10.) ist dauerhaft weg: 410 statt 404,
        # damit Suchmaschinen die Adresse schneller fallen lassen (Search-Console-Bericht 26.10.2026)
        raise HTTPException(410, _t("err_profile"))
    if not p or _eintrag_ausgabe(p) != _LANG.get():
        raise HTTPException(404, _t("err_profile"))
    site, P, esc, base = _site(), _prefix(request), _htmlmod.escape, _abs_base(request)
    paras = "".join(f"<p>{esc(x.strip())}</p>" for x in str(p.get("text") or "").split("\n") if x.strip())
    ress = "".join(f'<a class="r" href="{P}/{_t("p_sec")}/{_slug(r)}">{esc(r)}</a>' for r in p.get("ressorts", []))
    posts = [e for e in _blog_entries() if _profile_for_avatar(e.get("avatar", ""), [p])][:9]
    plist = (f'<div class="rowhead"><h2>{_t("posts_by")} ' + esc(p["name"]) + "</h2></div><div class=\"grid\">"
             + "".join(_card_html(e, P) for e in posts) + "</div>") if posts else ""
    og = (f'<meta property="og:type" content="profile"><meta property="og:title" content="{esc(p["name"])} · {esc(site["name"])}">'
          f'<meta property="og:description" content="{esc(p.get("kurz", ""))}">'
          + (f'<meta property="og:image" content="{base}/media/avatar/{esc(slug)}.jpg">' if p.get("photo") else "")
          + f"<style>{_PROFILE_CSS}</style>")
    inner = (f'<a class="back" href="{P}/{_t("p_about")}">← {_t("about")}</a><div class="profile">{_avatar_img(p, P)}'
             f'<div><h1>{esc(p["name"])}</h1><p class="kurz">{esc(p.get("kurz", ""))}</p><div class="meta">{ress}</div>{paras}'
             + (f'<p class="fiknote">{_t("fiknote_profile")}</p>' if p.get("fiktiv") else "")
             + f'</div></div>{plist}')
    return _news_shell(request, f"{p['name']} · {site['name']}", inner, head_extra=og, active="ueber", desc=p.get("kurz", ""))


# ------------------------------ Live-Ticker (Schlagzeilen) -------------
# Laufband oben auf der Website. Quelle: freigegebene Schlagzeilen aus dem Newsroom-Scan
# (Redaktion klickt "+ Ticker") oder eigene Zeilen; optional "auto" = alle Scan-Schlagzeilen.
TICKER_FILE = DATA / "ticker.json"
_TICKER_MAX = 15


def _ticker_file() -> Path:
    """Ticker je Ausgabe: ticker.json (de) bzw. ticker_<key>.json (andere Ausgaben)."""
    key = _LANG.get()
    return DATA / f"ticker_{key}.json" if key != "de" else TICKER_FILE


def _ticker_cfg() -> dict:
    d = {"auto": False, "max_age_hours": 24, "items": [], "hidden": []}
    try:
        if _ticker_file().is_file():
            x = json.loads(_ticker_file().read_text(encoding="utf-8"))
            if isinstance(x, dict):
                d.update({k: v for k, v in x.items() if k in d})
    except (ValueError, OSError):
        pass
    d["items"] = [i for i in d.get("items", []) if isinstance(i, dict) and i.get("text")]
    # hidden: Scan-Schlagzeilen, die die Redaktion im Auto-Modus ausgeblendet hat (Text, klein geschrieben)
    d["hidden"] = [str(h).strip().lower() for h in (d.get("hidden") or []) if str(h).strip()][:60]
    return d


def _ticker_save(d: dict):
    _ticker_file().parent.mkdir(parents=True, exist_ok=True)
    _ticker_file().write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def _ticker_items() -> list:
    """Sichtbare Ticker-Zeilen: manuelle/freigegebene (nicht abgelaufen) + bei auto die Scan-Schlagzeilen."""
    cfg = _ticker_cfg()
    now = _dt.datetime.now(_dt.timezone.utc)
    max_age = _dt.timedelta(hours=max(1, int(cfg.get("max_age_hours") or 48)))
    out, seen = [], set()
    for i in cfg["items"]:
        try:
            added = _dt.datetime.fromisoformat(str(i.get("added")))
        except ValueError:
            added = now
        if now - added > max_age:
            continue
        t = _nfc(i["text"])
        if t.lower() in seen:
            continue
        seen.add(t.lower())
        out.append({"text": t, "url": str(i.get("url") or "")})
    if cfg.get("auto"):
        nr = _newsroom_latest()
        hidden = set(cfg.get("hidden") or [])
        for tpc in sorted(nr.get("topics", []), key=lambda t: -int(t.get("score") or 0)):
            t = _nfc(tpc.get("headline", ""))
            if not t or t.lower() in seen or t.lower() in hidden:
                continue
            seen.add(t.lower())
            srcs = tpc.get("sources") or []
            url = (srcs[0].get("url") if isinstance(srcs[0], dict) else str(srcs[0])) if srcs else ""
            out.append({"text": t, "url": url})
    return out[:_TICKER_MAX]


def _ticker_html(items: list) -> str:
    if not items:
        return ""
    esc = _htmlmod.escape

    def one(i):
        t = esc(i["text"])
        return (f'<a href="{esc(i["url"])}" target="_blank" rel="noopener">{t}</a>'
                if i.get("url", "").startswith("http") else f"<span>{t}</span>")
    row = "".join(f'<span class="ti">{one(i)}</span>' for i in items)
    return (f'<div class="ticker"><span class="tlabel">{_t("ticker")}</span><div class="ttrack">'
            f'<div class="titems">{row}</div><div class="titems" aria-hidden="true">{row}</div></div></div>')


class TickerReq(BaseModel):
    auto: bool = False
    max_age_hours: int = 24
    items: list[dict] = []
    hidden: list[str] = []


class TickerAddReq(BaseModel):
    text: str
    url: str = ""


class TickerHideReq(BaseModel):
    text: str
    hide: bool = True


@app.get("/api/ticker")
def ticker_get(request: Request, lang: str = "de", ausgabe: str = ""):
    """ausgabe (oder lang): Schluessel der Ausgabe, Standard de (Ticker in data/ticker_<key>.json)."""
    require_user(request)
    with _lang(_ausgabe_key(ausgabe or lang)):
        cfg = _ticker_cfg()
        return {"auto": cfg["auto"], "max_age_hours": cfg["max_age_hours"], "items": cfg["items"],
                "hidden": cfg["hidden"], "visible": _ticker_items(),
                "scan_date": _newsroom_latest().get("date")}


@app.post("/api/ticker/hide")
def ticker_hide(req: TickerHideReq, request: Request):
    """Eine Scan-Schlagzeile im Auto-Modus ausblenden (oder wieder zeigen), ohne Auto abzuschalten."""
    require_user(request)
    t = _nfc(req.text).strip().lower()
    if not t:
        raise HTTPException(400, "Text is required.")
    cfg = _ticker_cfg()
    hidden = [h for h in cfg["hidden"] if h != t]
    if req.hide:
        hidden.insert(0, t)
    cfg["hidden"] = hidden[:60]
    _ticker_save(cfg)
    return {"ok": True, "hidden": cfg["hidden"], "visible": _ticker_items()}


@app.post("/api/ticker")
def ticker_set(req: TickerReq, request: Request):
    require_user(request)
    items = []
    for i in req.items[:_TICKER_MAX]:
        t = _nfc(str(i.get("text", "")))[:140]
        if not t:
            continue
        url = str(i.get("url") or "").strip()[:300]
        items.append({"id": str(i.get("id") or uuid.uuid4().hex[:8]), "text": t,
                      "url": url if url.startswith("http") else "",
                      "added": str(i.get("added") or _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"))})
    cfg = {"auto": bool(req.auto), "max_age_hours": max(1, min(24 * 14, int(req.max_age_hours))), "items": items,
           "hidden": [_nfc(h).strip().lower() for h in req.hidden if _nfc(h).strip()][:60]}
    _ticker_save(cfg)
    return {"ok": True, "visible": _ticker_items()}


@app.post("/api/ticker/add")
def ticker_add(req: TickerAddReq, request: Request):
    require_user(request)
    t = _nfc(req.text)[:140]
    if not t:
        raise HTTPException(400, "Text is required.")
    cfg = _ticker_cfg()
    if any(_nfc(i["text"]).lower() == t.lower() for i in cfg["items"]):
        return {"ok": True, "duplicate": True}
    url = req.url.strip()[:300]
    cfg["items"].insert(0, {"id": uuid.uuid4().hex[:8], "text": t, "url": url if url.startswith("http") else "",
                            "added": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")})
    cfg["items"] = cfg["items"][:_TICKER_MAX]
    _ticker_save(cfg)
    return {"ok": True, "count": len(cfg["items"])}


# ------------------------------ Konstruktiv-Score + Medien-Check ------
# Eigene Beitraege bekommen beim Veroeffentlichen einen Score (Hintergrund-Thread, ~1 ct).
# Medien-Check: fremde Artikel per Link pruefen -> Score -> Redaktion gibt frei -> Website.
# Artikeltexte fremder Seiten werden NIE gespeichert, nur Titel, Link, Domain und Bewertung.
REVIEWS_DIR = DATA / "reviews"


def _score_post_async(pid: str):
    def go():
        try:
            import konstruktiv
            e = _blog_entry(pid)
            if not e:
                return
            k = konstruktiv.score_text(str(e.get("text") or e.get("skript") or ""),
                                       lang=(e.get("sprache") or "de"))
            e2 = _blog_entry(pid)
            if k and e2:
                e2["konstruktiv"] = k
                _save_entry(pid, e2)
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=go, daemon=True).start()


def _score_cls(score: int) -> str:
    return "s-hi" if score >= 60 else ("s-mid" if score >= 40 else "s-lo")


def _score_badge(k: dict | None, P: str) -> str:
    if not isinstance(k, dict) or "score" not in k or not _site().get("show_scores", True):
        return ""
    esc = _htmlmod.escape
    return (f'<a class="score {_score_cls(int(k["score"]))}" href="{P}/{_t("p_method")}" '
            f'title="{esc(_t("score_expl"))} {esc(k.get("kurz", ""))}">{_t("score_word")} {int(k["score"])}</a>')


def _score_box(k: dict | None, P: str) -> str:
    if not isinstance(k, dict) or "score" not in k or not _site().get("show_scores", True):
        return ""
    esc = _htmlmod.escape
    import konstruktiv
    crit = "".join(f'<span class="{"ok" if k.get(c) else "no"}">{"✓" if k.get(c) else "✗"} {lbl}</span>'
                   for c, lbl in (("loesung", _t("crit_loesung")), ("belege", _t("crit_belege")), ("grenzen", _t("crit_grenzen"))))
    ton = (_t("ton_" + str(k.get("ton", ""))) if _LANG.get() != "de" and _t("ton_" + str(k.get("ton", ""))) != "ton_" + str(k.get("ton", ""))
           else konstruktiv.TON_LABEL.get(k.get("ton", ""), k.get("ton", "")))
    return (f'<div class="sbox"><div class="shead"><span class="score {_score_cls(int(k["score"]))}">{_t("score_word")} {int(k["score"])}/100</span>'
            f'<span class="scrit">{crit}<span>{_t("ton")}: {esc(ton)}</span></span></div>'
            f'<p><b>{esc(k.get("kurz", ""))}</b></p>'
            + (f'<p>{esc(k["erlaeuterung"])}</p>' if k.get("erlaeuterung") else "")
            + f'<a href="{P}/{_t("p_method")}">{_t("score_link")}</a></div>')


class ScoreReq(BaseModel):
    id: str


@app.post("/api/blog/score")
def blog_score(req: ScoreReq, request: Request):
    """Score fuer einen Beitrag (neu) berechnen, synchron, damit die Redaktion das Ergebnis sieht."""
    u = require_user(request)
    e = _blog_entry(req.id)
    if not e:
        raise HTTPException(404, "Post not found.")
    if e.get("uid") != u["id"] and not _is_editor(u):
        raise HTTPException(403, "Only the author or an admin.")
    import konstruktiv
    k = konstruktiv.score_text(str(e.get("text") or e.get("skript") or ""))
    if not k:
        raise HTTPException(502, "Scoring failed (text too short or model unavailable).")
    e["konstruktiv"] = k
    _save_entry(req.id, e)
    return {"ok": True, "konstruktiv": k}


class ScoreTextReq(BaseModel):
    text: str
    titel: str = ""
    sprache: str = "de"     # en: Begruendungen auf Englisch (englische Ausgabe)


@app.post("/api/score/text")
def score_text_ep(req: ScoreTextReq, request: Request):
    """Konstruktiv-Score fuer einen beliebigen Text, ohne Veroeffentlichung (fuer die Kette).
    Exakt derselbe Prompt wie fuer die Beitraege auf der Website."""
    require_user(request)
    import konstruktiv
    text = (req.titel.strip() + chr(10) + chr(10) if req.titel.strip() else "") + req.text
    k = konstruktiv.score_text(text, lang=(req.sprache or "de")[:2])
    if not k:
        raise HTTPException(502, "Scoring failed (text too short or model unavailable).")
    return {"ok": True, "konstruktiv": k}


@app.get("/api/score/prompt")
def score_prompt_ep(request: Request):
    """Der Bewertungs-Prompt im Wortlaut, damit Vorab-Bewertungen anderswo identisch sind."""
    require_user(request)
    import konstruktiv
    return {"model": konstruktiv.MODELL, "effort": "low", "system": konstruktiv.KRITERIEN}


def _reviews() -> list:
    out = []
    if REVIEWS_DIR.is_dir():
        for j in REVIEWS_DIR.glob("*.json"):
            try:
                rec = json.loads(j.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if isinstance(rec, dict) and rec.get("url"):
                rec["id"] = j.stem
                out.append(rec)
    out.sort(key=lambda r: str(r.get("added", "")), reverse=True)
    return out


def _review(rid: str) -> dict | None:
    if not rid or any(c in rid for c in ("..", "/", "\\")):
        return None
    j = REVIEWS_DIR / (rid + ".json")
    if not j.is_file():
        return None
    try:
        rec = json.loads(j.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    rec["id"] = rid
    return rec


def _save_review(rid: str, rec: dict):
    rec.pop("id", None)
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    (REVIEWS_DIR / (rid + ".json")).write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")


class ReviewCheckReq(BaseModel):
    url: str
    note: str = ""


@app.post("/api/reviews/check")
def review_check(req: ReviewCheckReq, request: Request):
    u = require_user(request)
    import konstruktiv
    url = req.url.strip()[:500]
    art = konstruktiv.fetch_article(url)
    if not art.get("ok"):
        raise HTTPException(400, art.get("error", "Artikel nicht lesbar."))
    k = konstruktiv.score_text(art["text"])
    if not k:
        raise HTTPException(502, "Bewertung fehlgeschlagen (Modell nicht erreichbar).")
    existing = next((r for r in _reviews() if r.get("url") == url), None)
    rid = existing["id"] if existing else "r_" + uuid.uuid4().hex[:10]
    rec = {"url": url, "domain": art["domain"], "titel": _nfc(art["title"])[:200], "konstruktiv": k,
           "note": _nfc(req.note)[:300], "approved": bool(existing and existing.get("approved")),
           "added": (existing or {}).get("added") or _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
           "checked": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
           "uid": (existing or {}).get("uid", u["id"]), "autor": (existing or {}).get("autor", u["username"])}
    _save_review(rid, rec)
    rec["id"] = rid
    return {"ok": True, "review": rec}


@app.get("/api/reviews")
def reviews_list(request: Request):
    require_user(request)
    return {"reviews": _reviews()}


class ReviewSetReq(BaseModel):
    approved: bool


@app.post("/api/reviews/{rid}")
def review_set(rid: str, req: ReviewSetReq, request: Request):
    require_user(request)
    rec = _review(rid)
    if not rec:
        raise HTTPException(404, "Review not found.")
    rec["approved"] = bool(req.approved)
    _save_review(rid, rec)
    return {"ok": True}


@app.delete("/api/reviews/{rid}")
def review_delete(rid: str, request: Request):
    u = require_user(request)
    rec = _review(rid)
    if not rec:
        raise HTTPException(404, "Review not found.")
    if rec.get("uid") != u["id"] and not _is_editor(u):
        raise HTTPException(403, "Only the creator or an admin.")
    (REVIEWS_DIR / (rid + ".json")).unlink(missing_ok=True)
    return {"ok": True}


def _review_item_html(r: dict, P: str, full: bool = False) -> str:
    esc = _htmlmod.escape
    k = r.get("konstruktiv") or {}
    more = (f'<p class="rvk">{esc(k["erlaeuterung"])}</p>' if full and k.get("erlaeuterung") else "")
    return (f'<div class="rv"><div class="rvtop"><span class="rvdom">{esc(r.get("domain", ""))}</span>'
            f'<span class="score {_score_cls(int(k.get("score", 0)))}">{int(k.get("score", 0))}</span></div>'
            f'<a class="rvt" href="{esc(r["url"])}" target="_blank" rel="noopener">{esc(r.get("titel") or r["url"])}</a>'
            f'<p class="rvk"><b>{esc(k.get("kurz", ""))}</b></p>{more}</div>')


def _reviews_aside_html(P: str, limit: int = 6) -> str:
    if not _site().get("show_scores", True) or _LANG.get() != "de":   # Medien-Check nur in der deutschen Ausgabe
        return ""
    rs = [r for r in _reviews() if r.get("approved")][:limit]
    if not rs:
        return ""
    return (f'<div class="rvhead"><h2>Medien-Check</h2><a href="{P}/medien">Alle →</a></div>'
            f'<p class="rvsub">Wie konstruktiv berichten andere? KI-Einschätzung nach festen Kriterien, von der Redaktion freigegeben.</p>'
            + "".join(_review_item_html(r, P) for r in rs)
            + f'<a class="rvmore" href="{P}/so-bewerten-wir">So bewerten wir →</a>')


@app.get("/news/medien")
def news_medien(request: Request):
    site, P = _site(), _prefix(request)
    rs = [r for r in _reviews() if r.get("approved")]
    items = "".join(_review_item_html(r, P, full=True) for r in rs) or '<div class="empty">Noch keine Bewertungen.</div>'
    inner = ("<h1>Medien-Check</h1><p class=\"sub\">Wie konstruktiv berichten andere Medien? Wir lassen einzelne Artikel "
             "nach festen Kriterien von einer KI einschätzen und veröffentlichen die Einschätzung erst nach Prüfung durch die Redaktion. "
             f"<a href=\"{P}/so-bewerten-wir\">So bewerten wir →</a></p><div class=\"rvgrid\">{items}</div>")
    return _news_shell(request, f"Medien-Check · {site['name']}", inner, active="medien",
                       desc="Konstruktiv-Score für Artikel anderer Medien, geprüft von der Redaktion.")


@app.get("/news/so-bewerten-wir")
def news_method(request: Request):
    if _sprache() == "en":
        return _news_method_en(request)
    site, P = _site(), _prefix(request)
    inner = f"""<h1>So bewerten wir</h1>
<div class="about"><p>Unter unseren Beiträgen und im Medien-Check steht ein <b>Konstruktiv-Score</b> von 0 bis 100. Er ist eine
Einschätzung durch ein KI-Modell nach festen, offengelegten Kriterien, keine Note für Wahrheit oder Qualität und kein Urteil über die
Autorinnen und Autoren. Er misst nur eins: Wie konstruktiv ist ein Text im Sinne des konstruktiven Journalismus?</p>
<h2>Die vier Kriterien</h2>
<p><b>Lösung benannt:</b> Nennt der Text eine Antwort, einen Lösungsansatz oder einen Fortschritt, statt nur ein Problem zu beschreiben?</p>
<p><b>Belege:</b> Stützt sich der Text auf konkrete Fakten, Zahlen oder Quellen?</p>
<p><b>Grenzen benannt:</b> Sagt der Text ehrlich, was noch offen ist oder wo die Lösung an ihre Grenzen stößt?</p>
<p><b>Ton:</b> Ist die Grundhaltung nüchtern oder lebendig (gut), alarmistisch oder werblich (weniger gut)?</p>
<h2>Die Skala</h2>
<p><b>60 bis 100:</b> lösungsorientiert, belegt, nüchtern bis ermutigend. <b>40 bis 59:</b> gemischt, oft Problembeschreibung mit einzelnen
konstruktiven Elementen. <b>Unter 40:</b> reine Problembeschreibung, Alarmismus oder Meinung ohne Belege.</p>
<h2>Was der Score nicht kann</h2>
<p>Er prüft keine Fakten und erkennt keine Falschmeldungen. Er bewertet nur den Text, nicht Bilder oder Videos. Bei fremden Artikeln
lesen wir nur den öffentlich zugänglichen Text; Bezahlartikel können wir nicht einschätzen. Jede Einschätzung im Medien-Check wird
von einem Menschen der Redaktion geprüft, bevor sie erscheint. Wir wenden dieselben Kriterien und dieselbe Skala auf unsere eigenen
Beiträge an. Wer eine Einschätzung für falsch hält, erreicht uns über das <a href="{P}/impressum">Impressum</a>.</p></div>"""
    return _news_shell(request, f"So bewerten wir · {site['name']}", inner, active="medien",
                       desc="Die Kriterien hinter dem Konstruktiv-Score von Persona AI News.")


# ------------------------------ Social-Posts zum Thema -----------------
# Redaktion verlinkt Social-Media-Beitraege zu einem Artikel. Metadaten (Titel, Autor,
# Vorschaubild) holt der SERVER per oEmbed; Bilder werden lokal gespiegelt. Keine fremden
# Skripte, keine Cookies beim Leser. YouTube per Klick ueber youtube-nocookie.
_SOCIAL_PLATFORMS = (("youtube.com", "YouTube"), ("youtu.be", "YouTube"), ("tiktok.com", "TikTok"),
                     ("instagram.com", "Instagram"), ("facebook.com", "Facebook"), ("fb.watch", "Facebook"),
                     ("x.com", "X"), ("twitter.com", "X"), ("bsky.app", "Bluesky"), ("threads.net", "Threads"),
                     ("linkedin.com", "LinkedIn"), ("mastodon.social", "Mastodon"))


def _social_platform(url: str) -> str:
    host = url.split("//", 1)[-1].split("/")[0].lower()
    for dom, name in _SOCIAL_PLATFORMS:
        if host == dom or host.endswith("." + dom):
            return name
    return host.replace("www.", "")[:30]


def _youtube_id(url: str) -> str:
    m = re.search(r"(?:youtu\.be/|v=|/shorts/|/embed/)([A-Za-z0-9_-]{6,})", url)
    return m.group(1) if m else ""


def _oembed(url: str, platform: str) -> dict:
    """Titel/Autor/Thumbnail ueber oeffentliche oEmbed-Endpunkte (ohne Schluessel)."""
    ep = {"YouTube": "https://www.youtube.com/oembed?format=json&url=",
          "TikTok": "https://www.tiktok.com/oembed?url=",
          "X": "https://publish.twitter.com/oembed?omit_script=true&url=",
          "Bluesky": "https://embed.bsky.app/oembed?url="}.get(platform)
    if not ep:
        return {}
    try:
        req = urllib.request.Request(ep + urllib.parse.quote(url, safe=""),
                                     headers={"User-Agent": "Mozilla/5.0 (compatible; PersonaAINews/1.0)"})
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    title = _nfc(d.get("title", ""))
    if not title and d.get("html"):   # X/Bluesky: Text aus dem Blockquote
        title = _nfc(re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<a[^>]*>.*?</a>", "", d["html"])))
        title = re.sub(r"\s+", " ", title)[:160]
    return {"title": title[:200], "author": _nfc(d.get("author_name", ""))[:80],
            "thumb_url": d.get("thumbnail_url", "")}


def _social_enrich(pid: str, idx: int, url: str, label: str, old: dict | None) -> dict:
    platform = _social_platform(url)
    rec = {"url": url, "platform": platform, "label": label, "yt": _youtube_id(url) if platform == "YouTube" else ""}
    if old and old.get("url") == url and old.get("fetched"):
        rec.update({k: old.get(k, "") for k in ("title", "author", "thumb", "fetched")})
        return rec
    meta = _oembed(url, platform)
    rec.update(title=meta.get("title", ""), author=meta.get("author", ""), thumb="", fetched=True)
    tu = meta.get("thumb_url", "")
    if tu:
        try:
            from PIL import Image
            req = urllib.request.Request(tu, headers={"User-Agent": "Mozilla/5.0 (compatible; PersonaAINews/1.0)"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = r.read(3_000_000)
            tmp = BLOG_DIR / f"tmp_{uuid.uuid4().hex[:6]}"
            tmp.write_bytes(data)
            with Image.open(tmp) as im:
                im = im.convert("RGB")
                im.thumbnail((480, 480))
                out = BLOG_DIR / f"{pid}_s{idx}.jpg"
                im.save(out, "JPEG", quality=82)
            tmp.unlink(missing_ok=True)
            rec["thumb"] = out.name
        except Exception:  # noqa: BLE001
            pass
    return rec


def _parse_social_lines(lines: list) -> list:
    out = []
    for ln in lines[:8]:
        ln = _nfc(ln)
        if not ln:
            continue
        label, url = "", ln
        if "|" in ln:
            a, b = ln.split("|", 1)
            if b.strip().startswith("http"):
                label, url = a.strip()[:120], b.strip()
            elif a.strip().startswith("http"):
                url, label = a.strip(), b.strip()[:120]
        if re.match(r"https?://", url):
            out.append((label, url[:500]))
    return out


def _social_html(e: dict, P: str) -> str:
    items = [s for s in (e.get("social") or []) if isinstance(s, dict) and s.get("url")]
    if not items:
        return ""
    esc = _htmlmod.escape
    cards = []
    for s in items:
        title = s.get("label") or s.get("title") or f"{_t('post_on')} {s.get('platform', '')}"
        thumb = (f'<img src="{P}/media/{esc(s["thumb"])}" alt="" loading="lazy">' if s.get("thumb") else "")
        yt = f' data-yt="{esc(s["yt"])}"' if s.get("yt") else ""
        cards.append(
            f'<a class="sc" href="{esc(s["url"])}" target="_blank" rel="noopener nofollow"{yt}>'
            f'<div class="scth">{thumb}{"<span class=\"play\">▶</span>" if s.get("yt") else ""}</div>'
            f'<div class="scb"><span class="scp">{esc(s.get("platform", ""))}</span><b>{esc(title)}</b>'
            f'{f"<span class=\"sca\">{esc(s['author'])}</span>" if s.get("author") else ""}</div></a>')
    return (f'<div class="social"><div class="rowhead"><h2>{_t("social_head")}</h2>'
            f'<span class="hint2">{_t("social_hint")}</span></div>'
            f'<div class="scgrid">{"".join(cards)}</div></div>')


# ------------------------------ Screenshot-Statistiken per Mail --------
# Registrierte Absender, Pruefliste der ausgelesenen Screenshots, Freigabe -> social_weekly.
_stats_lock = threading.Lock()


class SenderReq(BaseModel):
    email: str
    name: str = ""
    platform: str = ""     # leer = aus dem Bild erkennen
    konto: str = ""
    rhythm: str = "wöchentlich"


@app.get("/api/stats/senders")
def stats_senders(request: Request):
    require_user(request)
    import mail_stats
    return {"senders": [dict(v, email=k) for k, v in mail_stats.load_senders().items()],
            "address": _mail_address(), "status": mail_stats.status()}


def _mail_address() -> str:
    try:
        return json.loads((DATA / "mail.json").read_text(encoding="utf-8")).get("user", "")
    except (OSError, ValueError):
        return ""


@app.post("/api/stats/senders")
def stats_sender_save(req: SenderReq, request: Request):
    u = require_user(request)
    if not _is_editor(u):
        raise HTTPException(403, "Editors only.")
    import mail_stats
    em = req.email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", em):
        raise HTTPException(400, "Invalid e-mail address.")
    plat = req.platform.strip().lower()
    if plat and plat not in mail_stats.PLATFORMS:
        raise HTTPException(400, "Unknown platform.")
    d = mail_stats.load_senders()
    d[em] = {"name": _nfc(req.name)[:60], "platform": plat, "konto": _nfc(req.konto)[:60],
             "rhythm": _nfc(req.rhythm)[:30], "since": d.get(em, {}).get("since") or _dt.date.today().isoformat(),
             "added_by": u["username"]}
    mail_stats.save_senders(d)
    return {"ok": True}


@app.delete("/api/stats/senders/{em}")
def stats_sender_delete(em: str, request: Request):
    u = require_user(request)
    if not _is_editor(u):
        raise HTTPException(403, "Editors only.")
    import mail_stats
    d = mail_stats.load_senders()
    d.pop(em.strip().lower(), None)
    mail_stats.save_senders(d)
    return {"ok": True}


@app.get("/api/stats/inbox")
def stats_inbox(request: Request, weeks: int = 8):
    require_user(request)
    import mail_stats
    return {"items": mail_stats.rows(max(1, min(26, int(weeks))))}


@app.get("/api/stats/image/{path:path}")
def stats_image(path: str, request: Request):
    require_user(request)
    if ".." in path or not path.startswith("stats_inbox/"):
        raise HTTPException(400, "Invalid.")
    p = DATA / path
    if not p.is_file():
        raise HTTPException(404, "Nicht gefunden.")
    mt = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}.get(p.suffix[1:], "application/octet-stream")
    return FileResponse(p, media_type=mt)


class StatsSetReq(BaseModel):
    status: str                       # "ok" (freigeben) | "rejected"
    week: str = ""
    platform: str = ""
    konto: str = ""
    values: dict = {}                 # metric -> value (korrigiert)


@app.post("/api/stats/{sid}")
def stats_set(sid: int, req: StatsSetReq, request: Request):
    u = require_user(request)
    if not _is_editor(u):
        raise HTTPException(403, "Editors only.")
    import mail_stats
    if req.status not in ("ok", "rejected"):
        raise HTTPException(400, "Invalid status.")
    con = mail_stats._db()
    try:
        row = con.execute("SELECT week, platform, konto FROM stats_msgs WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "Not found.")
        week = req.week.strip() or row[0]
        if not re.fullmatch(r"\d{4}-W\d{2}", week or ""):
            raise HTTPException(400, "Invalid week.")
        plat = (req.platform.strip().lower() or row[1] or "other")
        konto = _nfc(req.konto) or (row[2] or "")
        for m, v in (req.values or {}).items():
            if m in mail_stats.METRICS:
                try:
                    con.execute("INSERT OR REPLACE INTO stats_values(msg, metric, value, label, sure) VALUES (?,?,?,?,1)",
                                (sid, m, float(v), "korrigiert"))
                except (TypeError, ValueError):
                    pass
        con.execute("UPDATE stats_msgs SET status=?, week=?, platform=?, konto=? WHERE id=?", (req.status, week, plat, konto, sid))
        con.commit()
        if req.status == "ok":
            vals = {m: v for m, v in con.execute("SELECT metric, value FROM stats_values WHERE msg=?", (sid,)).fetchall()}
            pkey = plat + ("-" + _slug(konto) if konto else "")
            pos = sum(vals.get(k, 0) for k in ("likes", "comments", "saves")) or vals.get("engagement", 0)
            _mdb_upsert_social(week, pkey, int(vals.get("reach", 0)), int(vals.get("views", 0)), int(vals.get("followers", 0)),
                               int(vals.get("shares", 0)), int(pos), 0, f"Screenshot #{sid}", u["id"])
    finally:
        con.close()
    return {"ok": True}


def _mdb_upsert_social(week, platform, reach, views, followers, shares, pos, neg, notes, uid):
    con = _mdb()
    try:
        con.execute("INSERT OR REPLACE INTO social_weekly(week, platform, reach, views, followers, shares, pos, neg, notes, uid, at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (week, platform, reach, views, followers, shares, pos, neg, notes, uid,
                     _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")))
        con.commit()
    finally:
        con.close()


@app.post("/api/stats/pull")
def stats_pull(request: Request):
    u = require_user(request)
    if not _is_editor(u):
        raise HTTPException(403, "Editors only.")
    if not (DATA / "mail.json").is_file():
        raise HTTPException(400, "Mailbox not configured (data/mail.json).")
    if not _stats_lock.acquire(blocking=False):
        raise HTTPException(409, "Already running.")
    try:
        import mail_stats
        return mail_stats.pull()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Mail pull failed: {str(e)[:160]}")
    finally:
        _stats_lock.release()


# ------------------------------ Website: Persona AI News --------------
# Oeffentliche Seite. Laeuft unter /news (Persona-Host) und, per Caddy-Rewrite, an der
# Wurzel der eigenen Domain (site.json: "domain"). Alle Links gehen ueber _prefix().
SITE_FILE = DATA / "site.json"
_SITE_DEFAULT = {"name": "Persona AI News",
                 "claim": "Konstruktive Nachrichten, gesprochen von unseren Avataren.",
                 "ressorts": ["Politik", "Deutschland", "Wirtschaft", "Finanzen", "Kultur",
                              "Sport", "Tech", "Außenpolitik"],
                 "about": "", "domain": "persona-ainews.ch", "indexable": False, "per_page": 20,
                 "show_scores": True, "google_verification": ""}

# ---- Ausgaben (Mandanten): de = Stammseite, en = englische Ausgabe unter /en (Entscheidung Max,
# 18.10.2026), weitere Ausgaben seit 19.10.2026 ueber das Register data/ausgaben.json ("Ausgabe wird
# Mandant": eigener Schluessel, Sprache, Ressorts, Pfad oder eigene Domain; angelegt von der Kette
# per POST /api/ausgaben). Die aktuelle Ausgabe (ihr Schluessel) steht in einer ContextVar; _site(),
# _prefix(), _blog_entries(), _profiles() und die Oberflaechentexte (_t, nach Sprache der Ausgabe)
# richten sich danach. Beitraege und Profile tragen das Feld `ausgabe` (alt: `sprache` en = Ausgabe en).
# Ausgaben ausser de laufen unter /news/<key>/... ueber einen Verteiler (news_edition_get); eine
# eigene Domain schreibt Caddy auf /news/<key>/... um.
_LANG = contextvars.ContextVar("persona_lang", default="de")
SITE_FILE_EN = DATA / "site_en.json"
_SITE_EN_DEFAULT = {"name": "Persona AI News",
                    "claim": "International news, constructive and short, presented by our AI anchors.",
                    "ressorts": ["World", "US Politics", "Economy", "Technology", "Science and Health",
                                 "Culture and Sport"],
                    "about": "", "domain": "persona-ainews.ch", "indexable": True, "per_page": 20,
                    "show_scores": True, "google_verification": ""}
AUSGABEN_FILE = DATA / "ausgaben.json"
_AUSGABEN_FEST = {
    "de": {"sprache": "de", "prefix": "", "domain": "persona-ainews.ch", "site": "site.json", "label": "DE",
           "titel_zusatz": "", "sichtbar": True},
    "en": {"sprache": "en", "prefix": "/en", "domain": "persona-ainews.ch", "site": "site_en.json", "label": "EN",
           "titel_zusatz": "(English)", "sichtbar": True},
}
_AUSGABE_FELDER = ("sprache", "prefix", "domain", "site", "label", "titel_zusatz", "sichtbar", "name")
_AUSGABE_RE = re.compile(r"^[a-z][a-z0-9]{1,15}$")
_ausgaben_cache: tuple[float, dict] = (-1.0, {})


def _ausgaben() -> dict:
    """Register aller Ausgaben: die festen (de, en) plus data/ausgaben.json (Datei gewinnt je Feld)."""
    global _ausgaben_cache
    try:
        mtime = AUSGABEN_FILE.stat().st_mtime if AUSGABEN_FILE.is_file() else 0.0
    except OSError:
        mtime = 0.0
    if _ausgaben_cache[0] == mtime:
        return _ausgaben_cache[1]
    out = {k: dict(v) for k, v in _AUSGABEN_FEST.items()}
    try:
        d = json.loads(AUSGABEN_FILE.read_text(encoding="utf-8")) if mtime else {}
    except (ValueError, OSError):
        d = {}
    for k, v in (d.items() if isinstance(d, dict) else []):
        if not (_AUSGABE_RE.match(str(k)) and isinstance(v, dict)):
            continue
        rec = out.get(k) or {"sprache": "de", "prefix": "/" + k, "domain": _AUSGABEN_FEST["de"]["domain"],
                             "site": f"site_{k}.json", "label": k.upper(), "titel_zusatz": "", "sichtbar": True}
        rec.update({kk: vv for kk, vv in v.items() if kk in _AUSGABE_FELDER})
        rec["sprache"] = "en" if str(rec.get("sprache", "de")).lower().startswith("en") else "de"
        out[k] = rec
    _ausgaben_cache = (mtime, out)
    return out


def _ausgabe(key: str | None = None) -> dict:
    a = _ausgaben()
    return a.get(key or _LANG.get()) or a["de"]


def _sprache() -> str:
    """Sprache der aktuellen Ausgabe (de oder en); steuert Oberflaechentexte, hreflang, Schema-Texte."""
    return _ausgabe().get("sprache", "de")


def _ausgabe_key(s: str | None) -> str:
    """Schluessel einer Ausgabe aus einem API-Parameter; leer = de, unbekannt = 404."""
    s = (s or "").strip().lower()[:16]
    if not s or s == "de":
        return "de"
    if s not in _ausgaben():
        raise HTTPException(404, f"Unknown edition: {s}")
    return s


def _stamm_domain() -> str:
    return (_ausgaben()["de"].get("domain") or "persona-ainews.ch").lower().strip()


def _eintrag_ausgabe(rec: dict) -> str:
    """Ausgabe eines Beitrags oder Profils: Feld `ausgabe`; fehlt es, aus `sprache` (en = englische Ausgabe)."""
    a = rec.get("ausgabe")
    if a:
        return str(a)
    return "en" if (rec.get("sprache") or "de") == "en" else "de"


def _ausgabe_home_abs(key: str) -> str:
    """Oeffentliche Startadresse einer Ausgabe: eigene Domain an der Wurzel, sonst Stammdomain plus Pfad."""
    a = _ausgabe(key)
    dom = (a.get("domain") or _stamm_domain()).lower().strip()
    return f"https://{dom}/" if dom != _stamm_domain() else f"https://{dom}{a.get('prefix') or ''}/"


def _ausgaben_sichtbar() -> list[str]:
    return [k for k, v in _ausgaben().items() if v.get("sichtbar", True)]


class _lang:
    """with _lang("en"): ... fuehrt den Block in der Ausgabe mit diesem Schluessel aus."""

    def __init__(self, code: str):
        self.code = code

    def __enter__(self):
        self.tok = _LANG.set(self.code)
        return self.code

    def __exit__(self, *a):
        _LANG.reset(self.tok)


def _edition(code: str):
    """Dekorator fuer Routen einer Ausgabe (Signatur bleibt fuer FastAPI erhalten)."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            with _lang(code):
                return fn(*a, **kw)
        return wrapper
    return deco


_UI = {
    "de": {
        "p_all": "alle", "p_about": "ueber", "p_sec": "r", "p_method": "so-bewerten-wir",
        "start": "Start", "about": "Über uns", "all_posts": "Alle Beiträge", "all_posts_arrow": "Alle Beiträge →",
        "back_all": "← Alle Beiträge", "chrono": "Chronologisch, neueste zuerst.", "none_yet": "Noch keine Beiträge.",
        "news_from": "Neues aus {r}", "all_from": "Alle aus {r} →", "section_sub": "Alle Beiträge aus dem Ressort {name}.",
        "newer": "← Neuere Beiträge", "older": "Ältere Beiträge →", "read_more": "Weiterlesen →", "sources": "Quellen",
        "avatar": "Avatar", "ticker": "Ticker", "speakers": "Für dieses Ressort sprechen:", "all_profiles": "Alle Profile →",
        "to_section": "Zum Ressort →", "others": "Weitere", "our_avatars": "Unsere Avatare", "posts_by": "Beiträge von",
        "fictional": "Fiktive Figur · KI-Avatar",
        "fiknote": "Die hier gezeigten Figuren sind erfunden. Namen, Biografien, Titel, Einrichtungen und Gesichter "
                   "stammen aus der KI und stehen für keine realen Personen. In jedem Video sind sie als KI-Avatar gekennzeichnet.",
        "fiknote_profile": "Fiktive Figur. Name, Biografie, Titel und Bild sind KI-erzeugt und stehen für keine reale Person. "
                           "In jedem Video ist die Figur als KI-Avatar gekennzeichnet.",
        "about_default": "{name} ist das Testgelände einer kleinen Redaktion: kurze, konstruktive Meldungen, "
                         "gesprochen von KI-Avataren.",
        "react_q": "Was löst der Beitrag bei dir aus?",
        "react_labels": (("neu", "🤔 Wusste ich nicht"), ("mut", "💛 Macht mir Mut"), ("teilen", "📣 Will ich weitergeben")),
        "score_word": "Konstruktiv", "crit_loesung": "Lösung benannt", "crit_belege": "Belege", "crit_grenzen": "Grenzen benannt",
        "ton": "Ton", "score_link": "KI-Einschätzung nach festen Kriterien. So bewerten wir →",
        "aktuell_word": "Aktuell", "aktuell_title": "Nachricht des Tages, ausgewählt nach Aktualität und Relevanz; dafür zeigen wir keinen Konstruktiv-Score.",
        "today_ed": "Tagesausgabe", "ed_of": "Ausgabe vom {d}", "stories_one": "ein Beitrag", "stories_n": "{n} Beiträge",
        "earlier": "Gestern und davor", "nature": "Natur", "nature_sub": "täglich ein Thema aus dem Natur-Scan: Landschaft, Klima, Landnutzung",
        "cards": "Die Karten zum Beitrag", "cards_sub": "aus dem Video, zum Vergrößern anklicken", "card_alt": "Karte {n}",
        "related": "Verwandte Beiträge",
        "dossiers": "Dossiers", "dossiers_sub": "Was wir zu einem Thema wissen: alle Beiträge dazu, neueste zuerst.",
        "dossier_from": "Dossier: {t}", "dossier_n": "{n} Beiträge", "as_of": "Stand", "all_dossiers": "Alle Dossiers →",
        "p_korr": "korrekturen", "corrections": "Korrekturen", "corrections_sub": "Was wir nach der Veröffentlichung berichtigt haben, mit Datum. Fehler melden: redaktion@persona-ainews.ch.",
        "corrections_none": "Bisher keine Korrekturen.", "correction_of": "Korrektur vom {d}:",
        "register": "Herkunftsregister", "register_sub": "Jedes Video und jede Karte dieser Seite steht hier mit Fingerabdruck, Zeit, Deklaration und Signatur. Die Videos tragen Content Credentials (C2PA), die Einträge jedes Tages werden in der Bitcoin-Blockchain verankert (OpenTimestamps).",
        "register_how": "Prüfen: Datei hochladen, auch nach Weiterleitung, Neukomprimierung oder als Screenshot. Einträge signiert mit dem Schlüssel der Redaktion:", "register_key": "öffentlicher Schlüssel",
        "check_h": "Ist dieses Video von uns?", "check_sub": "Video oder Standbild auswählen; die Datei wird geprüft und nicht gespeichert.", "check_btn": "Prüfen",
        "col_time": "Zeit (UTC)", "col_post": "Beitrag", "col_type": "Art", "col_decl": "Deklaration", "col_id": "Eintrag", "col_anchor": "Anker",
        "art_video": "Video", "art_karte": "Karte", "origin": "Herkunft", "origin_note": "Eintrag im Herkunftsregister mit Fingerabdruck, Signatur und Content Credentials",
        "score_expl": "Konstruktiv-Score 0 bis 100: Einschätzung eines Sprachmodells nach festen Kriterien (Lösung benannt, Belege, Grenzen). Klick: So bewerten wir.",
        "pitch": "Täglich fünf geprüfte Nachrichten als kurze Videos: konstruktiv erzählt, jede Zahl mit Quelle, gesprochen von fiktiven KI-Sprechern, kostenlos.",
        "pitch_nl": "Die Beiträge des Tages einmal täglich per E-Mail, kein Tracking:", "nl_consent_kurz": "Einverstanden, dass die Adresse für den Versand gespeichert wird (Datenschutz).",
        "nf_title": "Seite nicht gefunden", "nf_sub": "Die Adresse gibt es nicht oder nicht mehr. Hier die neuesten Beiträge:",
        "code_word": "Kennzeichen", "code_h": "Kennzeichen im Video prüfen", "code_how": "Jedes neue Video trägt unten in der Zeile „KI-erzeugt“ ein fünfstelliges Kennzeichen. Eintippen genügt, auch von einem Screenshot oder einem abgefilmten Bildschirm.",
        "social_head": "Dazu in den sozialen Medien", "social_hint": "Von der Redaktion ausgewählt. Links führen zu externen Plattformen.",
        "post_on": "Beitrag auf",
        "footer1": "Alle Videos sind KI-erzeugt und im Bild entsprechend gekennzeichnet. Reichweite zählen wir ohne Cookies und ohne Personendaten.",
        "legal_impressum": "Impressum", "legal_datenschutz": "Datenschutz", "legal_nutzung": "Nutzungsbedingungen", "legal_note": "",
        "medien": "Medien-Check", "switch_label": "EN", "switch_title": "English edition",
        "err_post": "Beitrag nicht gefunden.", "err_section": "Ressort nicht gefunden.", "err_profile": "Profil nicht gefunden.",
        "newsletter": "Newsletter", "podcast": "Podcast",
        "nl_title": "Newsletter", "nl_sub": "Die Beiträge des Tages einmal täglich per E-Mail, gegen 18:30 Uhr. Kein Tracking, jederzeit abbestellbar.",
        "nl_mail": "E-Mail-Adresse", "nl_consent": "Ich bin einverstanden, dass meine E-Mail-Adresse für den Versand des Newsletters gespeichert wird (Datenschutz).",
        "nl_button": "Anmelden", "nl_sent": "Danke. Bitte bestätige die Anmeldung über den Link in der E-Mail, die wir dir gerade geschickt haben.",
        "nl_error": "Das hat nicht geklappt. Bitte die Adresse prüfen und noch einmal versuchen.",
        "nl_confirmed": "Die Anmeldung ist bestätigt. Der nächste Newsletter kommt am Abend des nächsten Tages mit Beiträgen.",
        "nl_unsubscribed": "Du bist abgemeldet. Es kommen keine weiteren Mails.", "nl_badtoken": "Dieser Link ist ungültig oder schon verbraucht.",
        "pc_title": "Podcast", "pc_sub": "Jeder Beitrag als Tonspur, gesprochen von unseren Avataren. Abonnieren in jeder Podcast-App über den Feed:",
        "pc_hint": "Den Feed-Link in die Podcast-App kopieren (Apple Podcasts, Spotify, Pocket Casts oder eine andere); sobald der Podcast in den Verzeichnissen gelistet ist, findest du ihn auch per Suche.",
        "pc_episodes": "Letzte Folgen",
        "p_work": "so-arbeiten-wir", "work": "So arbeiten wir",
        "p_search": "suche", "search": "Suche", "search_sub": "Volltextsuche über Titel, Teaser und Text aller Beiträge dieser Ausgabe.",
        "search_ph": "Thema, Ort, Name …", "search_n": "{n} Treffer für „{q}“, nach Relevanz.", "search_none": "Kein Beitrag passt zu „{q}“. Andere Schreibweise oder ein kürzeres Wort versuchen.",
    },
    "en": {
        "p_all": "all", "p_about": "about", "p_sec": "section", "p_method": "how-we-score",
        "start": "Home", "about": "About", "all_posts": "All stories", "all_posts_arrow": "All stories →",
        "back_all": "← All stories", "chrono": "In chronological order, newest first.", "none_yet": "No stories yet.",
        "news_from": "Latest in {r}", "all_from": "All in {r} →", "section_sub": "All stories from the {name} desk.",
        "newer": "← Newer stories", "older": "Older stories →", "read_more": "Read more →", "sources": "Sources",
        "avatar": "Anchor", "ticker": "Ticker", "speakers": "Anchors for this desk:", "all_profiles": "All anchors →",
        "to_section": "To the desk →", "others": "More", "our_avatars": "Our anchors", "posts_by": "Stories presented by",
        "fictional": "Fictional character · AI avatar",
        "fiknote": "The anchors shown here are fictional. Names, biographies, titles, institutions and faces are AI-generated "
                   "and do not represent real people. Every video is labelled as an AI avatar.",
        "fiknote_profile": "Fictional character. Name, biography, title and image are AI-generated and do not represent a real person. "
                           "Every video labels the character as an AI avatar.",
        "about_default": "{name} in English is the international edition of a small newsroom experiment: two short, constructive "
                         "stories a day from around the world, including the United States, presented by AI anchors.",
        "react_q": "What does this story do for you?",
        "react_labels": (("neu", "🤔 Didn't know that"), ("mut", "💛 Gives me hope"), ("teilen", "📣 Want to share this")),
        "score_word": "Constructive", "crit_loesung": "Solution named", "crit_belege": "Evidence", "crit_grenzen": "Limits named",
        "ton": "Tone", "score_link": "AI assessment by fixed criteria. How we score →",
        "aktuell_word": "Latest", "aktuell_title": "News of the day, picked for timeliness and relevance; no constructive score is shown for it.",
        "today_ed": "Today's edition", "ed_of": "Edition of {d}", "stories_one": "one story", "stories_n": "{n} stories",
        "earlier": "Earlier", "nature": "Nature", "nature_sub": "one story a day from the nature scan",
        "cards": "The cards of this story", "cards_sub": "from the video, click to enlarge", "card_alt": "Card {n}",
        "related": "Related stories",
        "dossiers": "Dossiers", "dossiers_sub": "What we know about a topic: every story on it, newest first.",
        "dossier_from": "Dossier: {t}", "dossier_n": "{n} stories", "as_of": "As of", "all_dossiers": "All dossiers →",
        "p_korr": "corrections", "corrections": "Corrections", "corrections_sub": "What we corrected after publication, with the date. Report an error: redaktion@persona-ainews.ch.",
        "corrections_none": "No corrections so far.", "correction_of": "Correction of {d}:",
        "register": "Provenance register", "register_sub": "Every video and card on this site is listed here with its fingerprint, time, declaration and signature. The videos carry Content Credentials (C2PA); each day's entries are anchored in the Bitcoin blockchain (OpenTimestamps).",
        "register_how": "Check: upload a file, even after forwarding, re-encoding or as a screenshot. Entries are signed with the newsroom's key:", "register_key": "public key",
        "check_h": "Is this video ours?", "check_sub": "Pick a video or a still image; the file is checked and not stored.", "check_btn": "Check",
        "col_time": "Time (UTC)", "col_post": "Story", "col_type": "Type", "col_decl": "Declaration", "col_id": "Entry", "col_anchor": "Anchor",
        "art_video": "Video", "art_karte": "Card", "origin": "Provenance", "origin_note": "entry in the provenance register with fingerprint, signature and Content Credentials",
        "score_expl": "Constructive score 0 to 100: a language model's assessment by fixed criteria (solution named, evidence, limits). Click: how we score.",
        "pitch": "Five checked news stories a day as short videos: constructive, every number sourced, presented by fictional AI anchors, free.",
        "pitch_nl": "The stories of the day once a day by e-mail, no tracking:", "nl_consent_kurz": "I agree that the address is stored for sending (privacy policy, in German).",
        "nf_title": "Page not found", "nf_sub": "This address does not exist or no longer exists. The latest stories:",
        "code_word": "Code", "code_h": "Check the code in the video", "code_how": "Every new video carries a five-character code in the “AI-generated” line at the bottom. Type it in, even from a screenshot or a filmed screen.",
        "ton_nuechtern": "sober", "ton_lebendig": "lively", "ton_alarmistisch": "alarmist", "ton_werblich": "promotional",
        "social_head": "On social media", "social_hint": "Selected by the newsroom. Links lead to external platforms.",
        "post_on": "Post on",
        "footer1": "All videos are AI-generated and labelled as such in the picture. We count reach without cookies and without personal data.",
        "legal_impressum": "Legal notice", "legal_datenschutz": "Privacy", "legal_nutzung": "Terms", "legal_note": " (in German)",
        "medien": "", "switch_label": "DE", "switch_title": "Deutsche Ausgabe",
        "err_post": "Story not found.", "err_section": "Desk not found.", "err_profile": "Anchor not found.",
        "newsletter": "Newsletter", "podcast": "Podcast",
        "nl_title": "Newsletter", "nl_sub": "The stories of the day once a day by e-mail, around 18:30 CET. No tracking, unsubscribe any time.",
        "nl_mail": "E-mail address", "nl_consent": "I agree that my e-mail address is stored for sending the newsletter (privacy policy, in German).",
        "nl_button": "Subscribe", "nl_sent": "Thank you. Please confirm your subscription with the link in the e-mail we have just sent you.",
        "nl_error": "That did not work. Please check the address and try again.",
        "nl_confirmed": "Your subscription is confirmed. The next newsletter arrives on the evening of the next day with stories.",
        "nl_unsubscribed": "You are unsubscribed. No more mails will come.", "nl_badtoken": "This link is invalid or already used.",
        "pc_title": "Podcast", "pc_sub": "Every story as an audio track, presented by our AI anchors. Subscribe in any podcast app with the feed:",
        "pc_hint": "Copy the feed link into your podcast app (Apple Podcasts, Spotify, Pocket Casts or another); once the podcast is listed in the directories you will also find it by search.",
        "pc_episodes": "Latest episodes",
        "p_work": "how-we-work", "work": "How we work",
        "p_search": "search", "search": "Search", "search_sub": "Full-text search across title, teaser and text of every story in this edition.",
        "search_ph": "Topic, place, name …", "search_n": "{n} results for “{q}”, by relevance.", "search_none": "No story matches “{q}”. Try another spelling or a shorter word.",
    },
}


def _t(key: str):
    """Oberflaechentext der aktuellen Ausgabe nach ihrer Sprache (Rueckfall: deutsch, dann der Schluessel selbst)."""
    d = _UI.get(_sprache(), _UI["de"])
    v = d.get(key)
    if v is None:
        v = _UI["de"].get(key, key)
    return v


def _site() -> dict:
    a = _ausgabe()
    default = _SITE_EN_DEFAULT if a.get("sprache") == "en" else _SITE_DEFAULT
    f = DATA / (a.get("site") or "site.json")
    s = dict(default)
    if a.get("name"):
        s["name"] = a["name"]
    if _LANG.get() != "de":
        s["indexable"] = False   # neue Ausgaben starten unsichtbar fuer Suchmaschinen; site_<key>.json schaltet frei
    try:
        if f.is_file():
            d = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                s.update({k: v for k, v in d.items() if k in default})
    except (ValueError, OSError):
        pass
    s["ressorts"] = [_nfc(r) for r in s.get("ressorts", []) if _nfc(r)][:20]
    return s


def _slug(s: str) -> str:
    s = _nfc(s).lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"), ("&", "und")):
        s = s.replace(a, b)
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s)).strip("-")[:40]


def _host(request: Request) -> str:
    h = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    return h.split(",")[0].strip().split(":")[0].lower()


def _eigene_domain(request: Request) -> str:
    """Die eigene Domain der aktuellen Ausgabe, wenn die Anfrage darueber kommt (nur Ausgaben mit
    einer Domain, die nicht die Stammdomain ist); sonst ''."""
    own = (_ausgabe().get("domain") or "").lower().strip()
    if _LANG.get() == "de" or not own or own == _stamm_domain():
        return ""
    h = _host(request)
    return own if h in (own, "www." + own) else ""


def _prefix_base(request: Request) -> str:
    """'' auf der Stammdomain oder der eigenen Domain der Ausgabe (Caddy schreibt /x -> /news/... um),
    sonst '/news' (Persona-Host)."""
    dom = _stamm_domain()
    h = _host(request)
    return "" if (dom and h in (dom, "www." + dom)) or _eigene_domain(request) else "/news"


def _prefix(request: Request) -> str:
    """Linkpraefix der aktuellen Ausgabe: Stammseite wie _prefix_base, andere Ausgaben mit ihrem Pfad
    dahinter (/en, /<key>); auf ihrer eigenen Domain laeuft eine Ausgabe an der Wurzel."""
    if _eigene_domain(request):
        return ""
    return _prefix_base(request) + (_ausgabe().get("prefix") or "")


def _abs_base(request: Request) -> str:
    h = _host(request) or "persona.clipfactory.ch"
    for dom in (_stamm_domain(), (_ausgabe().get("domain") or "").lower().strip()):
        if dom and h == "www." + dom:
            h = dom   # kanonisch ohne www (Caddy leitet www dorthin um)
    return f"https://{h}{_prefix(request)}"


@app.get("/api/site")
def site_get(request: Request):
    require_user(request)
    return _site()


class SiteReq(BaseModel):
    name: str = ""
    claim: str = ""
    ressorts: list[str] = []
    about: str = ""
    domain: str = ""
    indexable: bool = False
    show_scores: bool = True
    google_verification: str = ""


@app.post("/api/site")
def site_set(req: SiteReq, request: Request):
    u = require_user(request)
    if u["role"] != "admin":
        raise HTTPException(403, "Admin only.")
    s = _site()
    s.update(name=_nfc(req.name)[:60] or _SITE_DEFAULT["name"], claim=_nfc(req.claim)[:200],
             ressorts=[_nfc(r)[:40] for r in req.ressorts if _nfc(r)][:20] or _SITE_DEFAULT["ressorts"],
             about=_nfc(req.about)[:8000],
             domain=re.sub(r"[^a-z0-9.-]", "", req.domain.lower().strip())[:80],
             indexable=bool(req.indexable), show_scores=bool(req.show_scores),
             google_verification=re.sub(r"[^A-Za-z0-9_-]", "", req.google_verification)[:100])
    SITE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SITE_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "site": s}


_NEWS_CSS = """
:root{--bg:#f6f5f1;--surface:#ffffff;--line:#e6e2d9;--line2:#d5d0c5;--ink:#1c1b19;--muted:#5d5a54;
  --faint:#8b877e;--accent:#0f766e;--accent-dark:#0b5b55;--accent-soft:#e3f1ef;--gold:var(--accent);--gold-bright:var(--accent-dark)}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:"Inter",system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  font-size:15.5px;line-height:1.6}
a{color:var(--accent-dark)}
.wrap{max-width:1120px;margin:0 auto;padding:18px 22px 60px}
header{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:8px 20px;
  padding-bottom:12px;border-bottom:2px solid var(--ink);margin-bottom:4px}
.brand{font-family:"Source Serif 4",Georgia,serif;font-size:32px;font-weight:700;letter-spacing:-.02em;color:var(--ink);text-decoration:none}
.brand .dot{color:var(--accent)}
.claim{color:var(--muted);font-size:13.5px;margin:0}
nav{display:flex;flex-wrap:wrap;gap:2px 16px;padding:10px 0 20px;font-size:14px;border-bottom:1px solid var(--line);margin-bottom:22px}
nav a{color:var(--muted);text-decoration:none;padding:4px 0;border-bottom:2px solid transparent;font-weight:500}
nav a:hover{color:var(--ink)}
nav a.on{color:var(--accent-dark);border-bottom-color:var(--accent)}
h1{font-family:"Source Serif 4",Georgia,serif;font-size:30px;margin:0 0 4px;letter-spacing:-.02em}
.sub{color:var(--muted);margin:0 0 22px;font-size:14px}
.lead{display:grid;grid-template-columns:1fr 160px;grid-template-areas:"hd vid" "bd vid" "re vid";gap:6px 22px;align-items:start;background:var(--surface);border:1px solid var(--line);
  border-radius:14px;padding:20px 22px;margin-bottom:22px}
.lead .hd{grid-area:hd}.lead .bd{grid-area:bd}.lead .vid{grid-area:vid;align-self:center}.lead .react{grid-area:re;margin-top:4px}
.lead.wide{grid-template-columns:1fr;grid-template-areas:"vid" "hd" "bd" "re"}
.lead.novid{grid-template-columns:1fr;grid-template-areas:"hd" "bd" "re"}
.lead h2{font-family:"Source Serif 4",Georgia,serif;font-size:25px;line-height:1.25;margin:0 0 8px}
.lead h2 a,.card h3 a{color:inherit;text-decoration:none}
.lead h2 a:hover,.card h3 a:hover{color:var(--accent-dark)}
.lead .teaser{font-size:15.5px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:14px 15px;display:flex;gap:13px;min-width:0}
.card .th{flex:0 0 72px}
.card .th img{width:72px;height:118px;object-fit:cover;border-radius:8px;background:#ddd;display:block}
.card .th.w img{width:72px;height:48px}
.card h3{font-family:"Source Serif 4",Georgia,serif;font-size:17px;line-height:1.3;margin:0 0 6px}
.card p{margin:0 0 6px;color:var(--muted);font-size:13.5px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.card .meta{margin-bottom:0}
.meta{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px;font-size:12px;color:var(--faint)}
.meta .r{color:var(--accent-dark);background:var(--accent-soft);border-radius:999px;padding:2px 10px;text-decoration:none;font-weight:600}
.rowhead{display:flex;align-items:baseline;justify-content:space-between;margin:30px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.rowhead h2{font-family:"Source Serif 4",Georgia,serif;font-size:21px;margin:0}
.rowhead a{color:var(--muted);text-decoration:none;font-size:13.5px}
.rowhead a:hover{color:var(--accent-dark)}
.today{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 14px;margin:2px 0 14px}
.today h2{font-family:"Source Serif 4",Georgia,serif;font-size:22px;margin:0}
.today span{color:var(--muted);font-size:13.5px}
.rowhead.natur{border-bottom-color:#cfe0d2}
.rowhead h2 small{display:block;font-family:Inter,sans-serif;font-weight:400;font-size:12.5px;color:var(--muted);margin-top:2px}
.grid.natur .card{background:#f0f6f1;border-color:#cfe0d2}
.meta .narr{color:#2d6a3e;font-weight:600}
.karten{margin-top:18px}.karten .rowhead{margin:0 0 10px;align-items:baseline}.karten .rowhead h2{font-size:17px}
.kstrip{display:flex;gap:10px;overflow-x:auto;padding:2px 0 8px;scroll-snap-type:x proximity}
.kstrip a{flex:0 0 auto;scroll-snap-align:start}
.kstrip img{height:230px;width:auto;border-radius:10px;border:1px solid var(--line);display:block;background:var(--bg)}
.dlinks{margin:0 0 18px;display:flex;flex-wrap:wrap;gap:8px}
.dlinks a{background:var(--accent-soft);color:var(--accent-dark);border-radius:999px;padding:5px 13px;text-decoration:none;font-size:13.5px;font-weight:600}
.dcard h3{font-family:"Source Serif 4",Georgia,serif;font-size:19px;margin:0 0 6px}.dcard h3 a{color:inherit;text-decoration:none}
.korr{background:#fff7e6;border:1px solid #f0d9a8;border-radius:10px;padding:10px 14px;margin:0 0 14px;font-size:14px;line-height:1.5}
.korr a{color:var(--accent-dark)}
.herk{margin-top:8px}.herk a{color:var(--accent-dark)}
.pitch{margin:22px 0 6px;padding:16px 18px;border:1px solid var(--line);border-radius:14px;background:var(--surface)}
.pitch p{margin:0 0 8px;font-size:15px}.pitch p a{color:var(--accent-dark);text-decoration:none;font-weight:600}.pitch .pitch2{color:var(--muted);font-size:13.5px}
.nlmini{display:flex;flex-wrap:wrap;gap:8px;align-items:center}.nlmini input[type=email]{flex:1 1 220px;padding:9px 12px;border:1px solid var(--line2);border-radius:8px;font-size:15px}
.nlmini button{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:10px 16px;font-size:15px;cursor:pointer}
.nlmini .nlc{flex-basis:100%;font-size:12.5px;color:var(--muted)}.nlmini .nlmsg{flex-basis:100%;font-size:13px;color:var(--accent-dark)}
.hk{width:100%;border-collapse:collapse;font-size:13px;margin-top:14px}.hk th,.hk td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
.hk th{color:var(--muted);font-weight:600}.hk code{font-size:12px}.hk tr:target td{background:var(--accent-soft)}
@media(max-width:620px){.hk{display:block;overflow-x:auto}}
.all{display:block;text-align:right;margin-top:12px;font-size:13.5px;color:var(--muted);text-decoration:none}
article{display:grid;grid-template-columns:1fr 240px;grid-template-areas:"hd vid" "bd vid";gap:6px 26px;align-items:start;background:var(--surface);
  border:1px solid var(--line);border-radius:14px;padding:28px 30px;margin-bottom:18px}
article .hd{grid-area:hd}article .bd{grid-area:bd}article .vid{grid-area:vid}
article.wide{grid-template-columns:1fr;grid-template-areas:"vid" "hd" "bd"}
article.novid{grid-template-columns:1fr;grid-template-areas:"hd" "bd"}
article h2{font-family:"Source Serif 4",Georgia,serif;font-size:32px;line-height:1.2;margin:0 0 10px}
article p{margin:0 0 10px;color:var(--ink)}
.teaser{font-size:17px;color:var(--ink);margin:0 0 12px}
article .bd .teaser{font-weight:600;font-style:italic;font-size:17.5px;margin:2px 0 16px}
video{width:100%;border-radius:12px;background:#000;display:block}
article.wide video,.lead.wide video{max-height:70vh;width:auto;max-width:100%;margin:0 auto}
a.more{color:var(--accent-dark);text-decoration:none;font-weight:600;font-size:14px}
a.back{color:var(--muted);text-decoration:none;font-size:13.5px;display:inline-block;margin-bottom:16px}
.src{margin-top:16px;font-size:13px;color:var(--faint)}
.src a{color:var(--muted);word-break:break-all}
.react{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
.react button{background:var(--surface);border:1px solid var(--line2);color:var(--muted);border-radius:999px;
  padding:6px 12px;font-size:13px;cursor:pointer;font-family:inherit}
.react button:hover{border-color:var(--accent)}
.react button.on{color:#fff;background:var(--accent);border-color:var(--accent);font-weight:600}
.react button:disabled{cursor:default;opacity:.9}
.react span{margin-left:6px;color:var(--faint)}
.react button.on span{color:#e3f1ef}
.react .q{width:100%;font-size:12px;color:var(--faint);margin-bottom:-2px}
.pager{display:flex;justify-content:space-between;margin-top:20px;font-size:14px}
.such{display:flex;gap:8px;margin:0 0 22px;max-width:560px}
.such input{flex:1;min-width:0;padding:10px 13px;border:1px solid var(--line2);border-radius:9px;font:inherit;font-size:15px;background:var(--surface);color:var(--ink)}
.such input:focus{outline:2px solid var(--accent);outline-offset:1px;border-color:var(--accent)}
.such button{padding:10px 16px;border:0;border-radius:9px;background:var(--accent);color:#fff;font:inherit;font-weight:600;cursor:pointer}
.such button:hover{background:var(--accent-dark)}
.empty{color:var(--muted);padding:18px 0}
.pager a{color:var(--muted);text-decoration:none}
.about p{max-width:70ch}
footer{margin-top:40px;padding-top:18px;border-top:1px solid var(--line);
  color:var(--faint);font-size:12.5px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px}
footer a{color:var(--muted)}
.empty{color:var(--muted);border:1px dashed var(--line2);border-radius:14px;padding:36px;text-align:center}
.ticker{display:flex;align-items:center;gap:0;background:var(--ink);color:#fff;border-radius:8px;margin:0 0 14px;overflow:hidden;font-size:13.5px}
.tlabel{flex:0 0 auto;background:var(--accent);color:#fff;font-weight:700;letter-spacing:.06em;text-transform:uppercase;font-size:11px;padding:8px 12px}
.ttrack{flex:1;overflow:hidden;white-space:nowrap;position:relative;display:flex}
.titems{display:inline-flex;flex:0 0 auto;padding:7px 0;animation:tick 60s linear infinite}
.ticker:hover .titems{animation-play-state:paused}
.ti{padding:0 22px;border-right:1px solid rgba(255,255,255,.25)}
.ti a{color:#fff;text-decoration:none}
.ti a:hover{text-decoration:underline}
@keyframes tick{from{transform:translateX(0)}to{transform:translateX(-100%)}}
@media(prefers-reduced-motion:reduce){.titems{animation:none}.ttrack{white-space:normal;flex-wrap:wrap}.titems[aria-hidden]{display:none}}
.score{display:inline-block;border-radius:999px;padding:2px 10px;font-size:12px;font-weight:700;text-decoration:none;color:#fff;background:#8b877e}
.score.s-hi{background:var(--accent)}.score.s-mid{background:#c27c1a}.score.s-lo{background:#9b4a4a}
.score.s-akt{background:#2f5d8a;cursor:default}
nav a .nk{display:none}
.sbox{margin:18px 0 4px;padding:14px 16px;border:1px solid var(--line);border-radius:12px;background:var(--bg)}
.sbox .shead{display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center;margin-bottom:8px}
.sbox .shead .score{font-size:13px;padding:4px 12px}
.scrit{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12.5px;color:var(--muted)}
.scrit .ok{color:var(--accent-dark);font-weight:600}.scrit .no{color:var(--faint)}
.sbox p{margin:0 0 6px;font-size:14px;color:var(--ink)}
.sbox a{font-size:12.5px;color:var(--muted)}
.home{display:grid;grid-template-columns:280px 1fr;gap:26px;align-items:start}
.home .main{min-width:0}
.side{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:16px 18px;position:sticky;top:14px}
.rvhead{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:4px}
.rvhead h2{font-family:"Source Serif 4",Georgia,serif;font-size:19px;margin:0}
.rvhead a,.rvmore{font-size:13px;color:var(--muted);text-decoration:none}
.rvsub{font-size:12.5px;color:var(--faint);margin:0 0 12px}
.rv{padding:10px 0;border-top:1px solid var(--line)}
.rvtop{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}
.rvdom{font-size:11.5px;letter-spacing:.05em;text-transform:uppercase;color:var(--faint)}
.rvt{display:block;font-family:"Source Serif 4",Georgia,serif;font-size:15px;line-height:1.35;color:var(--ink);text-decoration:none;margin-bottom:4px}
.rvt:hover{color:var(--accent-dark)}
.rvk{font-size:12.5px;color:var(--muted);margin:0}
.rvmore{display:block;margin-top:10px}
.rvgrid{display:grid;grid-template-columns:repeat(2,1fr);gap:0 26px}
.rvgrid .rv{padding:14px 0}
.social{margin-top:20px}
.social .rowhead{margin-top:0}
.hint2{font-size:12px;color:var(--faint)}
.scgrid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}
.sc{display:flex;gap:12px;background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:10px;text-decoration:none;color:var(--ink);min-width:0}
.sc:hover{border-color:var(--accent)}
.scth{position:relative;flex:0 0 96px;min-height:64px;background:var(--accent-soft);border-radius:8px;overflow:hidden}
.scth img{width:96px;height:100%;object-fit:cover;display:block;min-height:64px}
.scth .play{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#fff;font-size:26px;text-shadow:0 1px 6px rgba(0,0,0,.6)}
.scb{display:flex;flex-direction:column;gap:3px;min-width:0}
.scb b{font-size:14px;line-height:1.35;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.scp{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--accent-dark);font-weight:700}
.sca{font-size:12.5px;color:var(--muted)}
.ytwrap{position:relative;padding-top:56.25%;border-radius:12px;overflow:hidden;background:#000;grid-column:1/-1}
.ytwrap iframe{position:absolute;inset:0;width:100%;height:100%;border:0}
@media(max-width:620px){.scgrid{grid-template-columns:1fr}}
@media(max-width:900px){.grid{grid-template-columns:repeat(2,1fr)}.home{grid-template-columns:1fr}.side{position:static;order:2}.rvgrid{grid-template-columns:1fr}}
@media(max-width:620px){.grid{grid-template-columns:1fr}
  .lead,article{grid-template-columns:1fr;grid-template-areas:"hd" "vid" "bd";gap:10px}
  .lead.novid,article.novid{grid-template-areas:"hd" "bd"}
  .lead .vid,article .vid{margin:2px 0 6px;text-align:center}
  .lead .vid video{max-height:50vh;width:auto;max-width:100%;margin:0 auto}
  article .vid video{max-height:66vh;width:auto;max-width:100%;margin:0 auto}
  .lead:not(.wide) video,article:not(.wide) video{max-width:220px;margin:0 auto}
  .wrap{padding:12px 14px 40px}.brand{font-size:24px}.claim{display:none}
  header{padding-bottom:8px;margin-bottom:0}
  nav{gap:0 14px;padding:6px 0 8px;margin-bottom:14px;font-size:13.5px;flex-wrap:wrap}
  nav a{white-space:nowrap;padding:5px 0}nav a.lang{margin-left:0!important}
  nav a .nl{display:none}nav a .nk{display:inline}
  .lead{grid-template-columns:38% 1fr;grid-template-areas:"hd hd" "vid bd" "re re";gap:8px 12px;align-items:start}
  .lead.novid{grid-template-columns:1fr;grid-template-areas:"hd" "bd" "re"}
  .react{gap:6px;margin-top:6px}.react button{font-size:12px;padding:5px 10px}.react .q{font-size:11.5px}.react span{margin-left:4px}
  .lead .vid video{max-height:none;width:100%}.lead .vid{margin:0;align-self:start}.lead .bd .teaser{font-size:14.5px;margin-top:0}
  .ticker{font-size:12.5px;margin-bottom:10px}.tlabel{padding:6px 9px}}
"""

_NEWS_JS = r"""
(function(){
  var src=(new URLSearchParams(location.search).get('utm_source')||'').toLowerCase();
  try{ if(src) sessionStorage.setItem('src',src); }catch(e){}
  var S=''; try{ S=sessionStorage.getItem('src')||''; }catch(e){}
  function ev(kind,post,value){
    try{
      var body=JSON.stringify({kind:kind,post:post||'index',value:value||'',source:S});
      if(navigator.sendBeacon){ navigator.sendBeacon('/api/metrics/event',new Blob([body],{type:'application/json'})); }
      else{ fetch('/api/metrics/event',{method:'POST',headers:{'Content-Type':'application/json'},body:body,keepalive:true}); }
    }catch(e){}
  }
  ev('view',window.__post||'index');
  var seen={};
  if('IntersectionObserver' in window){
    var io=new IntersectionObserver(function(es){ es.forEach(function(e){
      var id=e.target.dataset.post||e.target.id;
      if(e.isIntersecting&&id&&!seen[id]){ seen[id]=1; ev('seen',id); } }); },{threshold:.5});
    document.querySelectorAll('[data-post]').forEach(function(a){ io.observe(a); });
  }
  document.querySelectorAll('video').forEach(function(v){
    var host=v.closest('[data-post]'); if(!host) return; var id=host.dataset.post, played=false, marks={};
    v.addEventListener('play',function(){ if(!played){ played=true; ev('play',id); } });
    v.addEventListener('timeupdate',function(){ if(!v.duration) return; var p=v.currentTime/v.duration*100;
      [25,50,75,100].forEach(function(m){ if(p>=m-0.5&&!marks[m]){ marks[m]=1; ev('progress',id,String(m)); } }); });
    v.addEventListener('ended',function(){ if(!marks[100]){ marks[100]=1; ev('progress',id,'100'); } });
  });
  document.querySelectorAll('.sc[data-yt]').forEach(function(a){ a.addEventListener('click',function(ev){
    ev.preventDefault(); var w=document.createElement('div'); w.className='ytwrap';
    w.innerHTML='<iframe src="https://www.youtube-nocookie.com/embed/'+encodeURIComponent(a.dataset.yt)+'?autoplay=1" allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen loading="lazy"></iframe>';
    a.replaceWith(w); }); });
  document.querySelectorAll('.react').forEach(function(r){
    var post=r.dataset.rpost, done=null; try{ done=localStorage.getItem('r_'+post); }catch(e){}
    var bs=r.querySelectorAll('button');
    function lock(v){ bs.forEach(function(x){ x.disabled=true; if(x.dataset.v===v) x.classList.add('on'); }); }
    if(done) lock(done);
    bs.forEach(function(b){ b.onclick=function(){
      var d=null; try{ d=localStorage.getItem('r_'+post); }catch(e){} if(d) return;
      try{ localStorage.setItem('r_'+post,b.dataset.v); }catch(e){}
      var n=b.querySelector('span'); n.textContent=(parseInt(n.textContent,10)||0)+1;
      lock(b.dataset.v); ev('react',post,b.dataset.v); }; });
  });
})();
"""

_REACT_LABELS = (("neu", "🤔 Wusste ich nicht"), ("mut", "💛 Macht mir Mut"),
                 ("teilen", "📣 Will ich weitergeben"))


def _teaser_of(e: dict) -> str:
    t = _nfc(e.get("teaser", ""))
    if t:
        return t
    first = str(e.get("text") or e.get("skript") or "").strip().split("\n")[0]
    return first if len(first) <= 220 else first[:217].rsplit(" ", 1)[0] + " …"


def _meta_html(e: dict, P: str) -> str:
    esc = _htmlmod.escape
    r = _nfc(e.get("ressort", ""))
    parts = []
    if r:
        parts.append(f'<a class="r" href="{P}/{_t("p_sec")}/{_slug(r)}">{esc(r)}</a>')
    parts.append(f"<span>{esc(_datum_kurz(_tag_berlin(e.get('published_at', '')), _sprache()))}</span>")
    if e.get("avatar"):
        prof = _profile_for_avatar(e["avatar"])
        # Link nur zu sichtbaren Profilen; die 60 Ensemble-Figuren sind seit 18.10.2026 unsichtbar
        # (Entscheidung Max: nur die zehn Nachrichtensprecher), ihre alten Beitraege behalten den Namen
        parts.append(f'<span>{_t("avatar")}: <a href="{P}/team/{esc(prof["slug"])}" style="color:inherit">{esc(prof["name"])}</a></span>'
                     if prof and prof.get("sichtbar", True) else f"<span>{_t('avatar')}: {esc(prof['name'] if prof else e['avatar'])}</span>")
    if e.get("narrativ"):
        parts.append(f'<span class="narr">🌿 {esc(str(e["narrativ"]))}</span>')   # Natur-Scan: das Narrativ des Beitrags
    badge = _aktuell_badge(e) or _score_badge(e.get("konstruktiv"), P)
    if badge:
        parts.append(badge)
    return '<div class="meta">' + "".join(parts) + "</div>"


def _ist_aktuell(e: dict) -> bool:
    """Spur Aktuell (26.10.2026): Beitraege der Nachmittagsausgabe nach Aktualitaet; der Konstruktiv-Score
    wird berechnet und gespeichert, aber nicht gezeigt, die Karte traegt stattdessen die Marke "Aktuell"."""
    return e.get("spur") == "aktuell"


def _aktuell_badge(e: dict) -> str:
    if not _ist_aktuell(e):
        return ""
    return f'<span class="score s-akt" title="{_htmlmod.escape(_t("aktuell_title"))}">{_t("aktuell_word")}</span>'


def _react_html(e: dict, counts: dict) -> str:
    pc = counts.get(e["id"], {})
    return (f'<div class="react" data-rpost="{_htmlmod.escape(e["id"])}"><div class="q">{_t("react_q")}</div>'
            + "".join(f'<button type="button" data-v="{k}">{lbl}<span>{pc.get(k, 0)}</span></button>'
                      for k, lbl in _t("react_labels")) + "</div>")


def _video_html(e: dict, P: str, preload: str = "metadata") -> str:
    """preload "none" fuer den Aufmacher (30.10.2026): nur das Standbild laedt, das Video erst beim Tippen."""
    if not e.get("video"):
        return ""
    pid = _htmlmod.escape(e["id"])
    poster = f' poster="{P}/media/{pid}.jpg"' if e.get("poster") else ""
    return f'<div class="vid"><video src="{P}/media/{pid}.mp4"{poster} controls preload="{preload}" playsinline></video></div>'


def _layout_cls(e: dict) -> str:
    if not e.get("video"):
        return "novid"
    return "wide" if int(e.get("w") or 0) > int(e.get("h") or 0) else ""


def _lead_html(e: dict, counts: dict, P: str) -> str:
    esc = _htmlmod.escape
    pid = esc(e["id"])
    body = str(e.get("text") or e.get("skript") or "").strip()
    teaser = _teaser_of(e)
    more = f'<a class="more" href="{P}/{pid}">{_t("read_more")}</a>' if (len(body) > len(teaser) + 20 or e.get("quellen")) else ""
    return (f'<div class="lead {_layout_cls(e)}" data-post="{pid}">'
            f'<div class="hd"><h2><a href="{P}/{pid}">{esc(e["titel"])}</a></h2>{_meta_html(e, P)}</div>'
            f'{_video_html(e, P, preload="none")}'
            f'<div class="bd"><p class="teaser">{esc(teaser)}</p>{more}</div>{_react_html(e, counts)}</div>')


def _card_html(e: dict, P: str) -> str:
    esc = _htmlmod.escape
    pid = esc(e["id"])
    th = ""
    if e.get("poster"):
        wide = int(e.get("w") or 0) > int(e.get("h") or 0)
        th = f'<a class="th{" w" if wide else ""}" href="{P}/{pid}"><img src="{P}/media/{pid}.jpg" alt="" loading="lazy"></a>'
    return (f'<div class="card" data-post="{pid}">{th}<div><h3><a href="{P}/{pid}">{esc(e["titel"])}</a></h3>'
            f'<p>{esc(_teaser_of(e))}</p>{_meta_html(e, P)}</div></div>')


def _karten_html(e: dict, P: str) -> str:
    """Die Karten der Grafikstrecke als Bildleiste unter dem Text (26.10.2026): <pid>_k<n>.png liegen
    seit dem Anhaengen des Videos oeffentlich neben dem Beitrag (Feld `karten` = Anzahl)."""
    n = int(e.get("karten") or 0)
    if n <= 0:
        return ""
    esc = _htmlmod.escape
    pid = esc(e["id"])
    items = "".join(f'<a href="{P}/media/{pid}_k{i}.png" target="_blank" rel="noopener">'
                    f'<img src="{P}/media/{pid}_k{i}.png" alt="{esc(_t("card_alt").format(n=i))}" loading="lazy"></a>'
                    for i in range(1, n + 1))
    return (f'<div class="karten"><div class="rowhead"><h2>{_t("cards")}</h2><span class="hint2">{esc(_t("cards_sub"))}</span></div>'
            f'<div class="kstrip">{items}</div></div>')


_FUELL = {"de": {"nach", "beim", "über", "für", "mit", "von", "vor", "auf", "aus", "bei", "soll", "will", "wird", "mehr", "noch",
                 "nicht", "sich", "sind", "ist", "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einem", "eines",
                 "und", "oder", "als", "wie", "auch", "sein", "seine", "ihre", "diese", "dieser", "einer", "zwischen", "gegen",
                 "unter", "durch", "wegen", "trotz", "seit", "bis", "ohne", "zum", "zur", "vom", "ins", "ans", "dass", "wenn",
                 "aber", "doch", "nur", "schon", "jetzt", "heute", "neue", "neuen", "neuer", "erste", "ersten", "jahr", "jahre",
                 "prozent", "millionen", "milliarden", "euro", "franken", "sagt", "laut", "mehrere", "viele", "wieder", "bisher"},
          "en": {"the", "and", "for", "with", "from", "after", "over", "into", "that", "this", "says", "said", "new", "than",
                 "about", "against", "between", "under", "through", "while", "their", "there", "these", "those", "will", "would",
                 "could", "should", "have", "has", "had", "been", "being", "more", "most", "some", "year", "years", "percent",
                 "million", "billion", "first", "last", "next", "week", "amid", "after", "before", "since", "until", "what", "when"}}


def _inhaltswoerter(e: dict) -> set[str]:
    stop = _FUELL.get(_sprache(), _FUELL["en"])
    text = f"{e.get('titel', '')} {e.get('teaser', '')}".lower()
    return {w for w in re.findall(r"[a-zäöüßéèàâ0-9]+", text) if len(w) > 3 and w not in stop}


def _verwandte(e: dict, ents: list, n: int = 3) -> list:
    """Verwandte Beitraege (26.10.2026): gemeinsame Inhaltswoerter in Titel und Teaser, dasselbe Ressort
    und dasselbe Narrativ zaehlen dazu; aufgefuellt mit den neuesten Beitraegen des Ressorts."""
    w = _inhaltswoerter(e)
    scored = []
    for x in ents:
        if x["id"] == e["id"]:
            continue
        g = len(w & _inhaltswoerter(x))
        s = g * 2 + (1 if x.get("ressort") == e.get("ressort") else 0) + (2 if e.get("narrativ") and x.get("narrativ") == e.get("narrativ") else 0)
        if g or (e.get("narrativ") and x.get("narrativ") == e.get("narrativ")):
            scored.append((s, x))
    scored.sort(key=lambda t: (-t[0], str(t[1].get("published_at", ""))), reverse=False)
    out = [x for s, x in scored if s >= 2][:n]
    if len(out) < n:
        for x in ents:
            if len(out) >= n:
                break
            if x["id"] != e["id"] and x not in out and x.get("ressort") == e.get("ressort"):
                out.append(x)
    return out


def _verwandte_html(e: dict, ents: list, P: str) -> str:
    vs = _verwandte(e, ents)
    if not vs:
        return ""
    return (f'<div class="rowhead"><h2>{_t("related")}</h2></div>'
            '<div class="grid">' + "".join(_card_html(x, P) for x in vs) + "</div>")


def _article_html(e: dict, counts: dict, P: str) -> str:
    esc = _htmlmod.escape
    pid = esc(e["id"])
    body = str(e.get("text") or e.get("skript") or "")
    content = "".join(f"<p>{esc(p.strip())}</p>" for p in body.split("\n") if p.strip())
    if _nfc(e.get("teaser", "")):
        content = f'<p class="teaser">{esc(_teaser_of(e))}</p>' + content
    content = _korrektur_html(e) + content
    q = e.get("quellen") or []
    if q:
        content += (f'<div class="src">{_t("sources")}: ' + " · ".join(
            f'<a href="{esc(u)}" target="_blank" rel="noopener">{esc(u.split("//", 1)[-1].split("/")[0])}</a>'
            for u in q) + "</div>")
    content += _herkunft_html(e, P)
    return (f'<article class="{_layout_cls(e)}" data-post="{pid}"><div class="hd"><h2>{esc(e["titel"])}</h2>{_meta_html(e, P)}</div>'
            f"{_video_html(e, P)}<div class=\"bd\">{content}{_karten_html(e, P)}{'' if _ist_aktuell(e) else _score_box(e.get('konstruktiv'), P)}{_social_html(e, P)}{_react_html(e, counts)}</div></article>")


# ---- Strukturierte Daten (JSON-LD), damit Suchmaschinen und KI-Assistenten die Seite lesen
# koennen (19.10.2026, Entscheidung Max: Lesbarkeit fuer KI-Systeme statt eines eigenen Produkts).
# Die Sprecher sind fiktiv und erscheinen deshalb nirgends als schema.org/Person.
def _jsonld(obj: dict) -> str:
    return '<script type="application/ld+json">' + json.dumps(obj, ensure_ascii=False).replace("</", "<\\/") + "</script>"


def _jsonld_org(site: dict, base: str, lang: str) -> dict:
    return {"@type": "NewsMediaOrganization", "@id": f"{base}/#organisation", "name": site["name"],
            "url": f"{base}/", "logo": {"@type": "ImageObject", "url": f"{base}/podcast/cover.png"},
            "description": site.get("claim", ""),
            "ethicsPolicy": f"{base}/{_t('p_method')}", "masthead": f"{base}/{_t('p_about')}",
            "publishingPrinciples": f"{base}/{_t('p_work')}",
            "correctionsPolicy": f"{base}/{_t('p_work')}",
            "knowsAbout": site.get("ressorts", []),
            "sameAs": [_ausgabe_home_abs(k) for k in _ausgaben_sichtbar()]}


def _jsonld_site(site: dict, base: str, lang: str) -> dict:
    return {"@context": "https://schema.org", "@graph": [
        {"@type": "WebSite", "@id": f"{base}/#website", "name": site["name"], "url": f"{base}/", "inLanguage": lang,
         "description": site.get("claim", ""), "publisher": {"@id": f"{base}/#organisation"}},
        _jsonld_org(site, base, lang)]}


def _jsonld_article(e: dict, site: dict, base: str, lang: str) -> dict:
    pid = e["id"]
    url = f"{base}/{pid}"
    art = {"@context": "https://schema.org", "@type": "NewsArticle", "@id": url, "mainEntityOfPage": url,
           "headline": e.get("titel", "")[:110], "description": _teaser_of(e), "inLanguage": lang,
           "datePublished": e.get("published_at", ""), "dateModified": e.get("updated_at") or e.get("published_at", ""),
           "articleSection": e.get("ressort", ""), "isAccessibleForFree": True,
           "author": {"@type": "Organization", "name": site["name"], "url": f"{base}/{_t('p_about')}"},
           "publisher": {"@id": f"{base}/#organisation"},
           "citation": [{"@type": "CreativeWork", "url": u} for u in (e.get("quellen") or [])[:10]]}
    body = str(e.get("text") or "")
    if body:
        art["articleBody"] = body[:5000]
    if e.get("poster"):
        art["image"] = [f"{base}/media/{pid}.jpg"]
    if int(e.get("karten") or 0):   # die Karten der Grafikstrecke (26.10.2026)
        art["image"] = (art.get("image") or []) + [f"{base}/media/{pid}_k{i}.png" for i in range(1, int(e["karten"]) + 1)]
    if e.get("video"):
        vid = {"@type": "VideoObject", "name": e.get("titel", ""), "description": _teaser_of(e),
               "contentUrl": f"{base}/media/{pid}.mp4", "uploadDate": e.get("published_at", ""),
               "thumbnailUrl": f"{base}/media/{pid}.jpg" if e.get("poster") else f"{base}/podcast/cover.png",
               "isFamilyFriendly": True}
        if e.get("audio_s"):
            vid["duration"] = f"PT{int(e['audio_s'])}S"
        art["video"] = vid
    if e.get("audio"):
        art["audio"] = {"@type": "AudioObject", "contentUrl": f"{base}/media/{pid}.mp3", "encodingFormat": "audio/mpeg"}
    ks = [k for k in (e.get("korrekturen") or []) if isinstance(k, dict) and k.get("text")]
    if ks:   # schema.org/correction: Korrekturen sichtbar fuer Suchmaschinen (26.10.2026)
        art["correction"] = [{"@type": "CorrectionComment", "text": k["text"][:500], "datePublished": str(k.get("datum", ""))[:10]} for k in ks]
    props = []
    hk = (e.get("register") or {}).get("video") if isinstance(e.get("register"), dict) else None
    if hk:   # Herkunftsregister (30.10.2026): Eintrag mit Hashes, Signatur und Content Credentials
        props.append({"@type": "PropertyValue", "name": "Provenance record" if lang == "en" else "Herkunftseintrag",
                      "value": hk, "url": f"{base}/register#{hk}"})
    if e.get("avatar"):
        # fiktiver Sprecher: als Rolle benannt, nicht als Person
        props.append({"@type": "PropertyValue", "name": "AI anchor (fictional)" if lang == "en" else "KI-Sprecher (fiktiv)",
                      "value": e["avatar"]})
    k = e.get("konstruktiv") or {}
    if _ist_aktuell(e):
        # Spur Aktuell: ausgewaehlt nach Aktualitaet, ohne Score auf der Seite, also auch nicht in den Daten
        props.append({"@type": "PropertyValue", "name": "Track" if lang == "en" else "Spur",
                      "value": "Latest" if lang == "en" else "Aktuell"})
    elif isinstance(k, dict) and "score" in k:
        # Der Score ist keine Rezension im Sinne von schema.org/Review (Google erlaubt Rezensions-Snippets nicht fuer
        # Nachrichtenartikel und nicht als Selbstbewertung; Search-Console-Meldung 20.10.2026). Er steht als
        # neutrale Zusatzeigenschaft mit Skala und Kurzbegruendung.
        props.append({"@type": "PropertyValue", "name": "Constructive score" if lang == "en" else "Konstruktiv-Score",
                      "value": int(k["score"]), "minValue": 0, "maxValue": 100,
                      "description": (k.get("kurz") or "")[:300]})
    if props:
        art["additionalProperty"] = props if len(props) > 1 else props[0]
    return art


_WORK = {
    "de": ("So arbeiten wir", [
        ("Was Persona AI News ist", "Persona AI News ist das Testgelände einer kleinen Redaktion in Winterthur: kurze, konstruktive Nachrichten, "
         "täglich geschrieben mit Hilfe von Sprachmodellen, geprüft von Maschinen und Menschen, gesprochen von fiktiven KI-Sprechern. "
         "Es gibt eine deutsche Ausgabe mit Schwerpunkt deutschsprachiger Raum und eine englische Ausgabe mit internationalen Nachrichten einschließlich USA."),
        ("Woher die Themen kommen", "Jeden Morgen liest ein Scan die Nachrichtenfeeds von rund 25 deutschsprachigen beziehungsweise 35 internationalen "
         "Medien, Behörden und Forschungseinrichtungen. Ein Sprachmodell schlägt daraus die Themen des Tages vor, nach Aktualität, Relevanz und konstruktivem Gehalt. "
         "Für jedes Thema holt die Redaktionssoftware den vollständigen Quelltext; Themen ohne vollständige Quelle werden nicht geschrieben."),
        ("Wie ein Beitrag entsteht", "Ein Sprachmodell (Claude von Anthropic) schreibt aus dem Quelltext einen Beitrag mit fester Form: Anlass, Substanz, Grenzen, Ausblick. "
         "Jede Zahl im Beitrag muss wörtlich im Quelltext belegt sein oder einen offengelegten Rechenweg haben. Vierzehn automatische Prüfungen laufen über jeden Entwurf, "
         "darunter die Belegprüfung aller Zahlen, die Erreichbarkeit der Quellen und ein Abgleich gegen die Beiträge der letzten sieben Tage. Fällt eine Prüfung, geht der Entwurf zurück, höchstens zweimal."),
        ("Wer freigibt", "Kein Beitrag erscheint ohne Freigabe durch einen Menschen. Die Redaktion sieht Entwurf, Prüfbefunde, Redaktionsnotiz und Konstruktiv-Score und entscheidet. "
         "Der Konstruktiv-Score ist eine Einschätzung nach festen Kriterien (Lösung benannt, Belege, Grenzen benannt, Ton), erklärt auf der Seite „So bewerten wir“."),
        ("Die Sprecher", "Die Sprecherinnen und Sprecher sind erfundene Figuren. Name, Biografie, Gesicht und Stimme sind KI-erzeugt und stehen für keine reale Person. "
         "Die Gesichter entstehen mit einem Bildgenerator, die Videos mit einem Fotoavatar-Dienst (HeyGen), die Stimme ist eine synthetische Katalogstimme. "
         "Jedes Video trägt sichtbar das Label „KI-erzeugt“ und in den Dateidaten eine entsprechende Kennzeichnung."),
        ("Quellen und Korrekturen", "Unter jedem Beitrag stehen die Quellen als Links. Wer einen Fehler findet, erreicht uns über das Impressum; wir prüfen den Hinweis, "
         "korrigieren den Beitrag auf der Seite und ziehen fehlerhafte Videos zurück. Wir nehmen kein Geld für Beiträge und keine Werbung an."),
        ("Daten", "Die Seite setzt keine Cookies und zählt Reichweite ohne Personendaten. Newsletter nur mit Bestätigung, ohne Zählpixel. Mehr dazu in der Datenschutzerklärung."),
    ]),
    "en": ("How we work", [
        ("What Persona AI News is", "Persona AI News is the testing ground of a small newsroom in Winterthur, Switzerland: short, constructive news, "
         "written daily with the help of language models, checked by machines and people, presented by fictional AI anchors. "
         "There is a German edition focused on the German-speaking countries and an English edition with international news including the United States."),
        ("Where the topics come from", "Every morning a scan reads the news feeds of around 35 international media, public bodies and research institutions. "
         "A language model proposes the topics of the day by timeliness, international relevance and constructive content. For every topic the newsroom software "
         "fetches the full source text; topics without a complete source are not written."),
        ("How a story is made", "A language model (Claude by Anthropic) writes a story from the source text in a fixed form: the news, the substance, the limits, the outlook. "
         "Every number in the story must be supported verbatim by the source text or have a disclosed calculation. Fourteen automatic checks run over every draft, "
         "among them the evidence check for all numbers, the reachability of the sources and a comparison against the stories of the last seven days. If a check fails, the draft goes back, at most twice."),
        ("Who approves", "No story is published without approval by a person. The editors see the draft, the findings of the checks, an editorial note and the Constructive score, and decide. "
         "The Constructive score is an assessment by fixed criteria (solution named, evidence, limits named, tone), explained on the page \"How we score\"."),
        ("The anchors", "The anchors are invented characters. Name, biography, face and voice are AI-generated and represent no real person. "
         "The faces come from an image generator, the videos from a photo avatar service (HeyGen), the voice is a synthetic catalogue voice. "
         "Every video carries the visible label \"AI-generated\" and a matching marker in its file metadata."),
        ("Sources and corrections", "Below every story the sources are linked. If you find an error, reach us through the legal notice; we check the report, "
         "correct the story on the site and withdraw faulty videos. We take no money for stories and no advertising."),
        ("Data", "The site sets no cookies and counts reach without personal data. The newsletter works with confirmation only and without tracking pixels. More in the privacy policy (in German)."),
    ]),
}


def news_work(request: Request):
    site, esc, P, B = _site(), _htmlmod.escape, _prefix(request), _prefix_base(request)
    titel, abschnitte = _WORK[_sprache()]
    body = "".join(f"<h2>{esc(h)}</h2><p>{esc(t)}</p>" for h, t in abschnitte)
    inner = (f"<h1>{esc(titel)}</h1><div class=\"about legal\">{body}"
             f'<p><a href="{P}/{_t("p_method")}">{_t("score_link").split(".")[-1].strip()}</a> · '
             f'<a href="{P}/{_t("p_about")}">{_t("about")}</a> · <a href="{B}/impressum">{_t("legal_impressum")}</a></p></div>')
    return _news_shell(request, f"{titel} · {site['name']}", inner, head_extra=f"<style>{_LEGAL_CSS}</style>",
                       active="work", desc=abschnitte[0][1][:200])


# ---- IndexNow (19.10.2026): neue und geaenderte Seiten sofort an Bing, Yandex, Seznam, Naver melden.
# Schluesseldatei liegt unter /<key>.txt, der Schluessel in data/indexnow.key. Google nimmt IndexNow
# nicht an; dort gilt die Sitemap in der Search Console.
INDEXNOW_FILE = DATA / "indexnow.key"


def _indexnow_key() -> str:
    try:
        k = INDEXNOW_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        k = ""
    if not re.fullmatch(r"[a-f0-9]{32}", k):
        k = uuid.uuid4().hex
        INDEXNOW_FILE.parent.mkdir(parents=True, exist_ok=True)
        INDEXNOW_FILE.write_text(k, encoding="utf-8")
    return k


def indexnow_submit(urls: list[str]) -> None:
    """Im Hintergrund melden; Fehler werden nur protokolliert."""
    urls = [u for u in urls if u.startswith("https://")][:10000]
    if not urls:
        return
    host = urllib.parse.urlparse(urls[0]).netloc
    key = _indexnow_key()
    body = json.dumps({"host": host, "key": key, "keyLocation": f"https://{host}/{key}.txt", "urlList": urls}).encode("utf-8")

    def go():
        try:
            req = urllib.request.Request("https://api.indexnow.org/indexnow", data=body, method="POST",
                                         headers={"Content-Type": "application/json; charset=utf-8"})
            with urllib.request.urlopen(req, timeout=20) as r:
                print(f"[indexnow] {len(urls)} URL(s): HTTP {r.status}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[indexnow] fehlgeschlagen: {type(e).__name__}: {e}", flush=True)
    threading.Thread(target=go, daemon=True).start()


def _public_url(pid: str, lang: str) -> str:
    """Oeffentliche Adresse eines Beitrags in der Ausgabe `lang` (Schluessel): eigene Domain an der
    Wurzel, sonst Stammdomain plus Pfad der Ausgabe."""
    return _ausgabe_home_abs(lang) + pid


def news_llms(request: Request):
    """llms.txt: ein kurzer, maschinenlesbarer Steckbrief der Seite fuer KI-Assistenten (alle sichtbaren Ausgaben)."""
    with _lang("de"):
        de = _site()
        stamm = _abs_base(request)
    teile = []
    for key in _ausgaben_sichtbar():
        with _lang(key):
            s, sp = _site(), _sprache()
            b = _ausgabe_home_abs(key).rstrip("/")
            if sp == "en":
                kopf = "English edition" if key == "en" else f"{s['name']} ({key}, English)"
                teile.append(f"""## {kopf}
- Home: {b}/
- Desks: {", ".join(s['ressorts'])}
- RSS: {b}/feed.xml
- Podcast: {b}/podcast.xml
- How we work: {b}/{_t('p_work')}
- How we score: {b}/{_t('p_method')}
- About and anchors: {b}/{_t('p_about')}
- Sitemap: {b}/sitemap.xml
- Newsletter: {b}/newsletter""")
            else:
                kopf = "Deutsche Ausgabe" if key == "de" else f"{s['name']} ({key})"
                teile.append(f"""## {kopf}
- Startseite: {b}/
- Ressorts: {", ".join(s['ressorts'])}
- RSS: {b}/feed.xml
- Podcast: {b}/podcast.xml
- So arbeiten wir: {b}/{_t('p_work')}
- So bewerten wir (Konstruktiv-Score): {b}/{_t('p_method')}
- Ueber uns und Sprecher: {b}/{_t('p_about')}
- Sitemap: {b}/sitemap.xml
- Newsletter: {b}/newsletter""")
    n = len(teile)
    txt = f"""# Persona AI News

> {de['claim']} Kurze, konstruktive Nachrichten aus einer kleinen Redaktion in Winterthur (Schweiz), geschrieben mit Sprachmodellen, von Menschen freigegeben, gesprochen von fiktiven KI-Sprechern. {n} Ausgaben, darunter Deutsch (deutschsprachiger Raum) und Englisch (international, einschliesslich USA).

Betreiber: Let's Talk, Max Stadler, Winterthur. Impressum: {stamm}/impressum. Kontakt: max@letstalk.team.
Alle Sprecherinnen und Sprecher sind erfundene Figuren; jedes Video ist als KI-erzeugt gekennzeichnet. Jeder Beitrag nennt seine Quellen und traegt einen Konstruktiv-Score (0 bis 100) nach festen Kriterien.

""" + "\n\n".join(teile) + f"""

## Optional
- Datenschutz: {stamm}/datenschutz
- Nutzungsbedingungen: {stamm}/nutzungsbedingungen
"""
    return Response(txt, media_type="text/plain; charset=utf-8")


_RESSORT_KURZ = {"Umwelt und Natur": "Natur", "Kultur und Sport": "Kultur", "Science and Health": "Science",
                 "Culture and Sport": "Culture", "US Politics": "US"}


def _ressort_kurz(r: str) -> str:
    """Kurzform fuer die Handy-Navigation (30.10.2026): das erste Wort vor "und"/"and", Ausnahmen oben."""
    if r in _RESSORT_KURZ:
        return _RESSORT_KURZ[r]
    return re.split(r"\s+(?:und|and)\s+", r)[0]


def _news_shell(request: Request, title: str, inner: str, head_extra: str = "",
                active: str = "", desc: str = "") -> HTMLResponse:
    site, P, esc, lang = _site(), _prefix(request), _htmlmod.escape, _sprache()
    key = _LANG.get()
    B = _prefix_base(request)   # Rechtstexte der Stammseite (auf eigener Domain ueber den Verteiler)
    nav = f'<a href="{P}/" class="{"on" if active == "index" else ""}">{_t("start")}</a>' + "".join(
        f'<a href="{P}/{_t("p_sec")}/{_slug(r)}" class="{"on" if active == _slug(r) else ""}">'
        f'<span class="nl">{esc(r)}</span><span class="nk">{esc(_ressort_kurz(r))}</span></a>'
        for r in site["ressorts"]) + (f'<a href="{P}/dossiers" class="{"on" if active == "dossiers" else ""}">{_t("dossiers")}</a>' if _dossiers() else "") \
        + f'<a href="{P}/{_t("p_about")}" class="{"on" if active == "ueber" else ""}">{_t("about")}</a>' \
        + f'<a href="{P}/{_t("p_search")}" class="{"on" if active == "suche" else ""}" title="{esc(_t("search"))}">{_t("search")}</a>'
    # Wechsel zu den anderen sichtbaren Ausgaben (DE, EN, weitere Mandanten)
    andere = [k for k in _ausgaben_sichtbar() if k != key]
    for i, k in enumerate(andere):
        a = _ausgabe(k)
        titel = {"de": "Deutsche Ausgabe", "en": "English edition"}.get(k, a.get("name") or k)
        nav += (f'<a href="{_ausgabe_home_abs(k)}" class="lang" title="{esc(titel)}" '
                f'style="{"margin-left:auto;" if i == 0 else ""}font-weight:600">{esc(a.get("label") or k.upper())}</a>')
    alt_links = "".join(f'<link rel="alternate" hreflang="{_ausgabe(k)["sprache"]}" href="{_ausgabe_home_abs(k)}">'
                        for k in ("de", "en") if k in _ausgaben_sichtbar())
    robots = "" if site.get("indexable") else '<meta name="robots" content="noindex,nofollow">'
    if site.get("google_verification"):
        robots += f'<meta name="google-site-verification" content="{esc(site["google_verification"])}">'
    legal = (f'<a href="{P}/{_t("p_work")}">{_t("work")}</a> · <a href="{P}/{_t("p_korr")}">{_t("corrections")}</a> · '
             f'<a href="{P}/register">{_t("register")}</a> · '
             f'<a href="{P}/newsletter">{_t("newsletter")}</a> · <a href="{P}/podcast">{_t("podcast")}</a> · '
             f'<a href="{B}/impressum">{_t("legal_impressum")}</a>'
             + (f' · <a href="{P}/medien">{_t("medien")}</a>' if _t("medien") else "")
             + f' · <a href="{B}/datenschutz">{_t("legal_datenschutz")}</a> · <a href="{B}/nutzungsbedingungen">{_t("legal_nutzung")}</a>'
             + esc(_t("legal_note")))
    base = _abs_base(request)
    page = (f"<!doctype html><html lang=\"{lang}\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">" + robots
            + f"<title>{esc(title)}</title>"
            + (f'<meta name="description" content="{esc(desc)}">' if desc else "")
            + f'<link rel="alternate" type="application/rss+xml" title="{esc(site["name"])}" href="{P}/feed.xml">'
            + f'<link rel="icon" type="image/svg+xml" href="{P}/favicon.svg">'
            # Vorschaubild und Titel fuer geteilte Links, wenn die Seite keine eigenen setzt (30.10.2026)
            + ("" if "og:image" in head_extra else
               f'<meta property="og:title" content="{esc(title)}"><meta property="og:description" content="{esc(desc or site.get("claim", ""))}">'
               f'<meta property="og:image" content="{base}/podcast/cover.png"><meta name="twitter:card" content="summary">')
            + alt_links
            + f'<meta property="og:locale" content="{"en_US" if lang == "en" else "de_CH"}">'
            + _jsonld(_jsonld_site(site, base, lang))
            + head_extra + '<link rel="preconnect" href="https://fonts.googleapis.com"><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Source+Serif+4:wght@600;700&display=swap" rel="stylesheet">' + f"<style>{_NEWS_CSS}</style></head><body><div class=\"wrap\">"
            + _ticker_html(_ticker_items())
            + f'<header><a class="brand" href="{P}/">{esc(site["name"])}<span class="dot">.</span></a>'
            f'<p class="claim">{esc(site.get("claim", ""))}</p></header><nav>{nav}</nav>{inner}'
            f"<footer><div>{_t('footer1')}</div>"
            f'<div><a href="{P}/feed.xml">RSS</a> · {legal}</div></footer></div>'
            f"<script>{_NEWS_JS}</script></body></html>")
    headers = {} if site.get("indexable") else {"X-Robots-Tag": "noindex, nofollow"}
    return HTMLResponse(page, headers=headers)


def _listing(request: Request, ents: list, title: str, active: str, page: int, sub: str = "",
             extra: str = "", head_extra: str = "", base: str = "", leer: str = "", lead_erster: bool = True) -> HTMLResponse:
    """Liste von Beitraegen mit Aufmacher und Seitenzahlen. `base` ersetzt die Adresse der Seitenzahlen
    (Suche: mit Anfrage), `leer` den Text ohne Treffer, `lead_erster` = der erste Beitrag als Aufmacher."""
    site, P = _site(), _prefix(request)
    per = max(5, int(site.get("per_page") or 20))
    try:
        counts = _reaction_counts()
    except sqlite3.Error:
        counts = {}
    total = len(ents)
    page = max(1, page)
    start = (page - 1) * per
    chunk = ents[start:start + per]
    if not chunk:
        inner = f'<div class="empty">{_htmlmod.escape(leer) if leer else _t("none_yet")}</div>'
    else:
        lead = _lead_html(chunk[0], counts, P) if page == 1 and lead_erster else ""
        rest = chunk[1:] if (page == 1 and lead_erster) else chunk
        inner = lead + ('<div class="grid">' + "".join(_card_html(e, P) for e in rest) + "</div>" if rest else "")
    head = "" if active == "index" else f"<h1>{_htmlmod.escape(title)}</h1>" + (f'<p class="sub">{_htmlmod.escape(sub)}</p>' if sub else "") + extra
    if not base:
        base = f"{P}/" if active == "index" else (f"{P}/{_t('p_all')}" if active == "alle" else f"{P}/{_t('p_sec')}/{active}")
    trenner = "&" if "?" in base else "?"
    pager = ""
    if total > per:
        prev = f'<a href="{base}{trenner}page={page - 1}">{_t("newer")}</a>' if page > 1 else "<span></span>"
        nxt = f'<a href="{base}{trenner}page={page + 1}">{_t("older")}</a>' if start + per < total else "<span></span>"
        pager = f'<div class="pager">{prev}{nxt}</div>'
    full_title = site["name"] if active == "index" else f"{title} · {site['name']}"
    return _news_shell(request, full_title, head + inner + pager, head_extra=head_extra, active=active,
                       desc=site.get("claim", "") if active == "index" else sub)


_WOCHENTAGE = {"de": ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"],
               "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]}
_MONATE = {"de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"],
           "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]}
_NATUR_RESSORT = "Umwelt und Natur"


def _tag_berlin(iso: str) -> str:
    """Kalendertag (JJJJ-MM-TT) eines Zeitstempels in Europe/Berlin; die Beitraege tragen UTC."""
    try:
        d = _dt.datetime.fromisoformat(str(iso or "").replace("Z", "+00:00"))
    except ValueError:
        return str(iso or "")[:10]
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    from zoneinfo import ZoneInfo
    return d.astimezone(ZoneInfo("Europe/Berlin")).date().isoformat()


def _datum_kurz(tag: str, lang: str) -> str:
    """"30. Oktober 2026" bzw. "October 30, 2026" (Karten und Metazeilen, 30.10.2026 statt ISO)."""
    try:
        d = _dt.date.fromisoformat(tag)
    except ValueError:
        return tag
    mon = _MONATE.get(lang, _MONATE["en"])
    return f"{d.day}. {mon[d.month - 1]} {d.year}" if lang == "de" else f"{mon[d.month - 1]} {d.day}, {d.year}"


def _datum_lang(tag: str, lang: str) -> str:
    """"Montag, 26. Oktober 2026" bzw. "Monday, October 26, 2026"."""
    try:
        d = _dt.date.fromisoformat(tag)
    except ValueError:
        return tag
    wt, mon = _WOCHENTAGE.get(lang, _WOCHENTAGE["en"]), _MONATE.get(lang, _MONATE["en"])
    if lang == "de":
        return f"{wt[d.weekday()]}, {d.day}. {mon[d.month - 1]} {d.year}"
    return f"{wt[d.weekday()]}, {mon[d.month - 1]} {d.day}, {d.year}"


@app.get("/news")
@app.get("/news/")
def news_page(request: Request):
    """Startseite als Tagesausgabe (26.10.2026, Entscheidung Max): Kopf mit dem Datum der juengsten Ausgabe,
    Aufmacher und alle Beitraege dieses Tages, dann der Natur-Streifen (Natur-Scan, Narrative), "Gestern und davor"
    und die Ressort-Zeilen ("Neues aus …")."""
    site, P, esc, lang = _site(), _prefix(request), _htmlmod.escape, _sprache()
    ents = _blog_entries()
    try:
        counts = _reaction_counts()
    except sqlite3.Error:
        counts = {}
    if not ents:
        return _news_shell(request, site["name"], f'<div class="empty">{_t("none_yet")}</div>',
                           active="index", desc=site.get("claim", ""))
    # the edition of the day: every post of the latest calendar day that has one (early in the morning
    # that is still yesterday's edition)
    tag = _tag_berlin(ents[0].get("published_at", ""))
    heute = [e for e in ents if _tag_berlin(e.get("published_at", "")) == tag]
    frueher = [e for e in ents if e not in heute]
    inner = (f'<div class="today"><h2>{_t("today_ed")}</h2>'
             f'<span>{esc(_t("ed_of").format(d=_datum_lang(tag, lang)))} · '
             f'{esc((_t("stories_one") if len(heute) == 1 else _t("stories_n")).format(n=len(heute)))}</span></div>')
    inner += _lead_html(heute[0], counts, P)
    if heute[1:]:
        inner += '<div class="grid">' + "".join(_card_html(e, P) for e in heute[1:7]) + "</div>"
    # das Versprechen in einem Satz und die Anmeldung, dort wo der Erstbesucher ist (30.10.2026)
    inner += _pitch_html(P)
    # Natur-Streifen: die Beitraege des Natur-Scans (Narrativ) bzw. des Ressorts Umwelt und Natur
    natur = [e for e in ents if e.get("narrativ") or _nfc(e.get("ressort", "")) == _NATUR_RESSORT][:3]
    if natur and lang == "de":
        link = (f'<a href="{P}/{_t("p_sec")}/{_slug(_NATUR_RESSORT)}">{esc(_t("all_from").format(r=_NATUR_RESSORT))}</a>'
                if _NATUR_RESSORT in site["ressorts"] else "")
        inner += (f'<div class="rowhead natur"><h2>🌿 {_t("nature")}<small>{esc(_t("nature_sub"))}</small></h2>{link}</div>'
                  '<div class="grid natur">' + "".join(_card_html(e, P) for e in natur) + "</div>")
    if frueher:
        inner += (f'<div class="rowhead"><h2>{_t("earlier")}</h2><a href="{P}/{_t("p_all")}">{_t("all_posts_arrow")}</a></div>'
                  '<div class="grid">' + "".join(_card_html(e, P) for e in frueher[:6]) + "</div>")
    for r in site["ressorts"]:
        if r == _NATUR_RESSORT and natur and lang == "de":
            continue   # schon im Natur-Streifen
        rs = [e for e in ents if _nfc(e.get("ressort", "")) == r][:3]
        if not rs:
            continue
        inner += (f'<div class="rowhead"><h2>{esc(_t("news_from").format(r=r))}</h2>'
                  f'<a href="{P}/{_t("p_sec")}/{_slug(r)}">{esc(_t("all_from").format(r=r))}</a></div>'
                  '<div class="grid">' + "".join(_card_html(e, P) for e in rs) + "</div>")
    aside = _reviews_aside_html(P)
    if aside:
        inner = f'<div class="home"><aside class="side">{aside}</aside><div class="main">{inner}</div></div>'
    return _news_shell(request, site["name"], inner, active="index", desc=site.get("claim", ""))


@app.get("/news/alle")
def news_all(request: Request, page: int = 1):
    return _listing(request, _blog_entries(), _t("all_posts"), "alle", page, sub=_t("chrono"))


# ------------------------------ Suche (31.10.2026, Bestandsaufnahme Punkt 7) -------------------
def _such_form(P: str, q: str = "") -> str:
    esc = _htmlmod.escape
    return (f'<form class="such" action="{P}/{_t("p_search")}" method="get" role="search">'
            f'<input type="search" name="q" value="{esc(q)}" placeholder="{esc(_t("search_ph"))}" maxlength="120" required '
            f'aria-label="{esc(_t("search"))}"><button type="submit">{esc(_t("search"))}</button></form>')


@app.get("/news/suche")
def news_search(request: Request, q: str = "", page: int = 1):
    """Volltextsuche ueber den Index (suche.py): Titel, Teaser, Text der aktuellen Ausgabe, nach Relevanz."""
    q = re.sub(r"\s+", " ", _nfc(q))[:120]
    P = _prefix(request)
    form = _such_form(P, q)
    titel = _t("search")
    if not q:
        inner = f"<h1>{titel}</h1><p class=\"sub\">{_htmlmod.escape(_t('search_sub'))}</p>{form}"
        return _news_shell(request, f"{titel} · {_site()['name']}", inner, active="suche", desc=_t("search_sub"))
    meta = {e["id"]: e for e in _blog_entries()}       # liest den Ordner und fuehrt den Index nach
    treffer = _SUCHE.suchen(q, _LANG.get(), limit=200)
    ents = [meta[pid] for pid, _ in treffer if pid in meta]
    sub = _t("search_n").format(n=len(ents), q=q) if ents else ""
    return _listing(request, ents, f"{titel}: {q}", "suche", page, sub=sub, extra=form,
                    base=f"{P}/{_t('p_search')}?q={urllib.parse.quote(q)}", leer=_t("search_none").format(q=q),
                    lead_erster=False, head_extra='<meta name="robots" content="noindex,follow">')


@app.get("/api/suche")
def api_search(request: Request, q: str = "", ausgabe: str = "de", limit: int = 20):
    """Treffer als JSON: id, titel, teaser, ressort, published_at, ausschnitt (mit <b>)."""
    key = _ausgabe_key(ausgabe)
    if key not in _ausgaben():
        raise HTTPException(404, "Ausgabe unbekannt.")
    q = re.sub(r"\s+", " ", _nfc(q))[:120]
    with _lang(key):
        meta = {e["id"]: e for e in _blog_entries()}
        out = []
        for pid, schnipsel in _SUCHE.suchen(q, key, limit=max(1, min(int(limit), 100))):
            e = meta.get(pid)
            if e:
                out.append({"id": pid, "titel": e.get("titel"), "teaser": e.get("teaser"), "ressort": e.get("ressort"),
                            "published_at": e.get("published_at"), "ausschnitt": schnipsel})
    return {"q": q, "ausgabe": key, "treffer": out}


# ------------------------------ Dossiers: "Was wir zu X wissen" (26.10.2026) -------------
# Ein Dossier buendelt die Beitraege zu einem Thema: feste Dossiers aus data/dossiers[_<key>].json
# (slug, titel, intro, woerter; ein Beitrag gehoert dazu, wenn eines der Woerter in Titel oder Teaser
# steht) und je ein Dossier je Narrativ des Natur-Scans (Feld `narrativ` am Beitrag).
def _dossiers_file() -> Path:
    key = _LANG.get()
    return DATA / ("dossiers.json" if key == "de" else f"dossiers_{key}.json")


def _dossiers_fest() -> list:
    try:
        d = json.loads(_dossiers_file().read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    out = []
    for x in d if isinstance(d, list) else []:
        if isinstance(x, dict) and x.get("slug") and x.get("titel") and x.get("sichtbar", True):
            out.append({"slug": _slug(str(x["slug"])), "titel": _nfc(x["titel"])[:80], "intro": _nfc(x.get("intro", ""))[:600],
                        "woerter": [_nfc(w).lower() for w in (x.get("woerter") or []) if _nfc(w)], "narrativ": ""})
    return out


def _narrativ_kern(name: str) -> str:
    try:
        import newsroom
        return dict(newsroom.NARRATIVE).get(name, "")
    except Exception:  # noqa: BLE001
        return ""


def _dossiers(ents: list | None = None) -> list:
    """Alle Dossiers der Ausgabe mit ihren Beitraegen (neueste zuerst), nur solche mit Beitraegen."""
    ents = _blog_entries() if ents is None else ents
    out = []
    for d in _dossiers_fest():
        posts = [e for e in ents if _dossier_passt(d, e)]
        if posts:
            out.append({**d, "posts": posts})
    narrative = sorted({_nfc(e.get("narrativ", "")) for e in ents if e.get("narrativ")})
    for n in narrative:
        d = {"slug": "natur-" + _slug(n), "titel": f"{_t('nature')}: {n}", "intro": _narrativ_kern(n), "woerter": [], "narrativ": n}
        posts = [e for e in ents if _nfc(e.get("narrativ", "")) == n]
        if posts and not any(x["slug"] == d["slug"] for x in out):
            out.append({**d, "posts": posts})
    out.sort(key=lambda d: str(d["posts"][0].get("published_at", "")), reverse=True)
    return out


def _dossier_passt(d: dict, e: dict) -> bool:
    if d.get("narrativ"):
        return _nfc(e.get("narrativ", "")) == d["narrativ"]
    # Titel und Teaser, nicht der ganze Text: "Bundesregierung" im dritten Absatz macht keinen Beitrag zum Dossier
    text = f"{e.get('titel', '')} {e.get('teaser', '')}".lower()
    return any(w in text for w in d.get("woerter") or [])


def _dossier_links_html(e: dict, P: str, ents: list | None = None) -> str:
    ds = [d for d in _dossiers(ents) if any(x["id"] == e["id"] for x in d["posts"])][:2]
    if not ds:
        return ""
    esc = _htmlmod.escape
    return ('<div class="dlinks">' + "".join(
        f'<a href="{P}/dossier/{esc(d["slug"])}">📚 {esc(_t("dossier_from").format(t=d["titel"]))}</a>' for d in ds) + "</div>")


@app.get("/news/dossiers")
def news_dossiers(request: Request):
    site, P, esc = _site(), _prefix(request), _htmlmod.escape
    ds = _dossiers()
    inner = f"<h1>{_t('dossiers')}</h1><p class=\"sub\">{esc(_t('dossiers_sub'))}</p>"
    if not ds:
        inner += f'<div class="empty">{_t("none_yet")}</div>'
    else:
        inner += '<div class="grid">' + "".join(
            f'<div class="card dcard"><div><h3><a href="{P}/dossier/{esc(d["slug"])}">{esc(d["titel"])}</a></h3>'
            f'<p>{esc(d["intro"] or _teaser_of(d["posts"][0]))}</p>'
            f'<div class="meta"><span>{esc(_t("dossier_n").format(n=len(d["posts"])))}</span>'
            f'<span>{_t("as_of")} {esc(str(d["posts"][0].get("published_at", ""))[:10])}</span></div></div></div>'
            for d in ds) + "</div>"
    return _news_shell(request, f"{_t('dossiers')} · {site['name']}", inner, active="dossiers", desc=_t("dossiers_sub"))


@app.get("/news/dossier/{slug}")
def news_dossier(slug: str, request: Request):
    site, P, esc = _site(), _prefix(request), _htmlmod.escape
    d = next((x for x in _dossiers() if x["slug"] == slug), None)
    if not d:
        raise HTTPException(404, _t("err_post"))
    try:
        counts = _reaction_counts()
    except sqlite3.Error:
        counts = {}
    posts = d["posts"]
    inner = (f"<h1>{esc(d['titel'])}</h1><p class=\"sub\">{esc(d['intro'])}" + (" " if d["intro"] else "")
             + f"{esc(_t('dossier_n').format(n=len(posts)))} · {_t('as_of')} {esc(str(posts[0].get('published_at', ''))[:10])}</p>")
    inner += _lead_html(posts[0], counts, P)
    if posts[1:]:
        inner += '<div class="grid">' + "".join(_card_html(e, P) for e in posts[1:]) + "</div>"
    inner += f'<a class="all" href="{P}/dossiers">{_t("all_dossiers")}</a>'
    return _news_shell(request, f"{d['titel']} · {site['name']}", inner, active="dossiers", desc=d["intro"] or _teaser_of(posts[0]))


class DossierReq(BaseModel):
    slug: str
    titel: str
    intro: str = ""
    woerter: list[str] = []
    sichtbar: bool = True
    ausgabe: str = "de"


@app.get("/api/dossiers")
def dossiers_list(request: Request, ausgabe: str = "de"):
    require_user(request)
    with _lang(_ausgabe_key(ausgabe)):
        return {"dossiers": [{k: v for k, v in d.items() if k != "posts"} | {"beitraege": len(d["posts"])} for d in _dossiers()],
                "fest": _dossiers_fest()}


@app.post("/api/dossiers")
def dossiers_save(req: DossierReq, request: Request):
    """Dossier anlegen oder ersetzen (Rolle editor/admin); die Narrativ-Dossiers entstehen von selbst."""
    _require_editor(request)
    with _lang(_ausgabe_key(req.ausgabe)):
        slug = _slug(req.slug)
        if not slug or not _nfc(req.titel):
            raise HTTPException(400, "slug and titel are required.")
        fest = [d for d in _dossiers_fest() if d["slug"] != slug]
        fest.append({"slug": slug, "titel": _nfc(req.titel)[:80], "intro": _nfc(req.intro)[:600],
                     "woerter": [_nfc(w)[:40] for w in req.woerter if _nfc(w)][:40], "sichtbar": bool(req.sichtbar)})
        _dossiers_file().write_text(json.dumps(fest, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"ok": True, "slug": slug, "beitraege": len([e for e in _blog_entries() if _dossier_passt(fest[-1], e)])}


@app.delete("/api/dossiers/{slug}")
def dossiers_delete(slug: str, request: Request, ausgabe: str = "de"):
    _require_editor(request)
    with _lang(_ausgabe_key(ausgabe)):
        fest = [d for d in _dossiers_fest() if d["slug"] != _slug(slug)]
        _dossiers_file().write_text(json.dumps(fest, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"ok": True}


# ------------------------------ Herkunftsregister (30.10.2026) ------------------------------
# Jedes veroeffentlichte Video und jede Karte steht mit Hashes, Zeit, Deklaration und Signatur in
# data/herkunft/register.jsonl (herkunft.py); die Seite /register zeigt es und prueft hochgeladene Dateien.
def _herkunft_html(e: dict, P: str) -> str:
    h = e.get("register") if isinstance(e.get("register"), dict) else {}
    if not h.get("video"):
        return ""
    esc = _htmlmod.escape
    code = f' · {_t("code_word")} <a href="{P}/k/{esc(h["code"])}"><code>{esc(h["code"])}</code></a>' if h.get("code") else ""
    return (f'<div class="src herk">{_t("origin")}: <a href="{P}/register#{esc(h["video"])}">{esc(h["video"])}</a>{code} · '
            f'{esc(_t("origin_note"))}</div>')


def _kennzeichen_text(r: dict, lang: str) -> str:
    e = r.get("eintrag") or {}
    d = e.get("deklaration") or {}
    if r.get("status") == "eingetragen":
        wann = str(e.get("zeit", ""))[:16].replace("T", " ")
        link = f"{e.get('url', '').rsplit('/media/', 1)[0]}/{e.get('beitrag')}" if e.get("beitrag") else ""
        if lang == "en":
            return f"Genuine: a video of {e.get('absender', '')}, published {wann} UTC, declared as {d.get('inhalt', '')}" + (f"; anchor {d.get('sprecher')}" if d.get("sprecher") else "") + (f". Story: {link}" if link else "")
        return f"Echt: ein Video von {e.get('absender', '')}, veröffentlicht {wann} UTC, deklariert als {d.get('inhalt', '')}" + (f"; Sprecher {d.get('sprecher')}" if d.get("sprecher") else "") + (f". Beitrag: {link}" if link else "")
    if r.get("status") == "gerendert":
        wann = str((r.get("reserviert") or {}).get("zeit", ""))[:16].replace("T", " ")
        return (f"Known code: rendered by Persona AI News on {wann} UTC, the story is not published yet." if lang == "en"
                else f"Bekanntes Kennzeichen: am {wann} UTC bei Persona AI News gerendert, der Beitrag ist noch nicht veröffentlicht.")
    if r.get("status") == "ungueltig":
        return ("Not a valid code: five characters, letters and digits, without 0, O, 1, I, L." if lang == "en"
                else "Kein gültiges Kennzeichen: fünf Zeichen, Buchstaben und Ziffern, ohne 0, O, 1, I, L.")
    return ("Unknown: no video of Persona AI News carries this code. A video with one of our anchors and an unknown or missing code is not from us." if lang == "en"
            else "Unbekannt: Kein Video von Persona AI News trägt dieses Kennzeichen. Ein Video mit einem unserer Sprecher und ohne gültiges Kennzeichen stammt nicht von uns.")


@app.get("/news/k/{code}")
def news_kennzeichen(code: str, request: Request):
    import herkunft
    site, P, esc, lang = _site(), _prefix(request), _htmlmod.escape, _sprache()
    r = herkunft.nach_code(code)
    text = _kennzeichen_text(r, lang)
    icon = {"eingetragen": "✅", "gerendert": "🕓", "unbekannt": "❌", "ungueltig": "❓"}.get(r.get("status"), "")
    e = r.get("eintrag") or {}
    details = ""
    if e:
        details = (f'<p><a href="{P}/{esc(e.get("beitrag", ""))}">{esc((e.get("deklaration") or {}).get("titel") or e.get("beitrag", ""))}</a> · '
                   f'{esc(_t("register"))}: <a href="{P}/register#{esc(e["id"])}">{esc(e["id"])}</a> · C2PA {"✓" if (e.get("c2pa") or {}).get("eingebettet") else "·"}</p>')
    inner = (f"<h1>{_t('code_word')} <code>{esc(herkunft.normalisiere_code(code) or code[:12])}</code></h1>"
             f'<div class="korr"><b>{icon} {esc(text)}</b></div>{details}'
             f'<p class="hint2">{esc(_t("code_how"))}</p>'
             f'<form method="get" onsubmit="location.href=\'{P}/k/\'+this.c.value.trim().toUpperCase();return false">'
             f'<input name="c" maxlength="8" placeholder="7K2QM" style="font-size:18px;padding:6px 10px;width:9em"> <button type="submit">{esc(_t("check_btn"))}</button></form>'
             f'<p class="hint2"><a href="{P}/register">{esc(_t("register"))}</a></p>')
    return _news_shell(request, f"{_t('code_word')} {esc(herkunft.normalisiere_code(code) or '')} · {site['name']}", inner, active="register", desc=text[:160])


@app.get("/api/herkunft/code/{code}")
def herkunft_code(code: str, lang: str = "de"):
    import herkunft
    r = herkunft.nach_code(code)
    e = r.get("eintrag")
    return {"status": r.get("status"), "text": _kennzeichen_text(r, "en" if lang == "en" else "de"),
            "eintrag": ({k: e.get(k) for k in ("id", "beitrag", "art", "zeit", "absender", "deklaration", "url", "c2pa", "kennzeichen")} if e else None)}


@app.get("/news/register")
def news_register(request: Request):
    import herkunft
    site, P, esc, lang = _site(), _prefix(request), _htmlmod.escape, _sprache()
    key = _LANG.get()
    eintraege = [e for e in reversed(herkunft.alle()) if (e.get("deklaration") or {}).get("ausgabe", "de") == key][:300]
    tage = sorted({str(e.get("zeit", ""))[:10] for e in eintraege}, reverse=True)[:14]
    anker = {t: herkunft.anker_stand(t) for t in tage}
    rows = "".join(
        f'<tr id="{esc(e["id"])}"><td>{esc(str(e.get("zeit", ""))[:16].replace("T", " "))}</td>'
        f'<td><a href="{P}/{esc(e.get("beitrag", ""))}">{esc((e.get("deklaration") or {}).get("titel") or e.get("beitrag", ""))}</a></td>'
        f'<td>{esc(_t("art_" + e.get("art", "")))}</td>'
        f'<td>{esc((e.get("deklaration") or {}).get("sprecher") or (e.get("deklaration") or {}).get("inhalt", ""))}</td>'
        f'<td><code>{esc(e["id"])}</code></td>'
        f'<td>{f"<a href=\"{P}/k/{esc(e["kennzeichen"])}\"><code>{esc(e["kennzeichen"])}</code></a>" if e.get("kennzeichen") else "·"}</td>'
        f'<td>{"✓" if (e.get("c2pa") or {}).get("eingebettet") else "·"}</td>'
        f'<td>{esc(anker.get(str(e.get("zeit", ""))[:10], ""))}</td></tr>'
        for e in eintraege)
    inner = (f"<h1>{_t('register')}</h1><p class=\"sub\">{esc(_t('register_sub'))}</p>"
             f'<div class="korr"><b>{esc(_t("code_h"))}</b><br>{esc(_t("code_how"))}<br>'
             f'<form method="get" onsubmit="location.href=\'{P}/k/\'+this.c.value.trim().toUpperCase();return false">'
             f'<input name="c" maxlength="8" placeholder="7K2QM" style="font-size:18px;padding:6px 10px;width:9em"> <button type="submit">{esc(_t("check_btn"))}</button></form></div>'
             f'<div class="korr" id="pruefen"><b>{esc(_t("check_h"))}</b><br>{esc(_t("check_sub"))}<br>'
             f'<input type="file" id="hk_datei" accept="video/*,image/*"> <button type="button" id="hk_knopf">{esc(_t("check_btn"))}</button>'
             f'<div id="hk_ergebnis" style="margin-top:8px"></div></div>'
             f'<p class="hint2">{esc(_t("register_how"))} <a href="{P}/register/schluessel.pem">{esc(_t("register_key"))}</a>'
             + "".join(f' · <a href="{P}/register/anker/{esc(t)}.ots">{esc(t)}</a>' for t in tage if anker.get(t, "").startswith(("gestempelt", "bestätigt")))
             + "</p>"
             f'<table class="hk"><thead><tr><th>{_t("col_time")}</th><th>{_t("col_post")}</th><th>{_t("col_type")}</th><th>{_t("col_decl")}</th>'
             f'<th>{_t("col_id")}</th><th>{_t("code_word")}</th><th>C2PA</th><th>{_t("col_anchor")}</th></tr></thead><tbody>{rows or f"<tr><td colspan=8>{_t('none_yet')}</td></tr>"}</tbody></table>'
             f"<script>(function(){{var b=document.getElementById('hk_knopf'),f=document.getElementById('hk_datei'),o=document.getElementById('hk_ergebnis');"
             f"b.onclick=async function(){{if(!f.files[0])return;o.textContent='…';var fd=new FormData();fd.append('file',f.files[0]);"
             f"try{{var r=await fetch('{P}/api/herkunft/pruefen',{{method:'POST',body:fd}});var j=await r.json();o.textContent=j.text||JSON.stringify(j);}}catch(e){{o.textContent='Fehler: '+e;}}}};}})();</script>")
    return _news_shell(request, f"{_t('register')} · {site['name']}", inner, active="register", desc=_t("register_sub"))


_FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#1c1b19"/>'
            '<text x="12" y="46" font-family="Georgia,serif" font-weight="700" font-size="40" fill="#ffffff">P</text>'
            '<circle cx="50" cy="42" r="6" fill="#0f766e"/></svg>')


@app.get("/news/favicon.svg")
def news_favicon():
    return Response(_FAVICON, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=604800"})


@app.get("/news/register/schluessel.pem")
def news_register_key():
    import herkunft
    return Response(herkunft.oeffentlicher_schluessel(), media_type="application/x-pem-file")


@app.get("/news/register/anker/{tag}")
def news_register_anker(tag: str):
    import herkunft
    tag = tag[:-4] if tag.endswith(".ots") else tag
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", tag) or not (herkunft.ANKER / f"{tag}.root.ots").is_file():
        raise HTTPException(404, "Not found.")
    return FileResponse(herkunft.ANKER / f"{tag}.root.ots", media_type="application/octet-stream", filename=f"persona-ainews-{tag}.root.ots")


@app.get("/api/herkunft")
def herkunft_list(beitrag: str = ""):
    import herkunft
    es = herkunft.alle()
    if beitrag:
        es = [e for e in es if e.get("beitrag") == beitrag]
    return {"absender": herkunft.ABSENDER, "schluessel": "/register/schluessel.pem", "eintraege": es}


@app.get("/api/herkunft/{eid}")
def herkunft_one(eid: str):
    import herkunft
    e = herkunft.eintrag(eid)
    if not e:
        raise HTTPException(404, "Unknown entry.")
    return {**e, "signatur_gueltig": herkunft.signatur_gueltig(e)}


_HERKUNFT_PRUEF_MAX = 120 * 1024 * 1024


def _herkunft_text(r: dict, lang: str) -> str:
    """One sentence for the bot and the page."""
    e = r.get("eintrag") or {}
    d = e.get("deklaration") or {}
    if r.get("status") in ("exakt", "aehnlich"):
        wann = str(e.get("zeit", ""))[:16].replace("T", " ")
        if r.get("mehrdeutig"):
            kand = ", ".join([e.get("beitrag", "")] + list(r["mehrdeutig"]))
            if lang == "en":
                return (f"Known: a video of {e.get('absender', '')}, declared as {d.get('inhalt', '')}"
                        + (f"; anchor {d.get('sprecher')}" if d.get("sprecher") else "")
                        + f". The exact story is not unambiguous from this excerpt (the anchor's frames are the same in several videos); candidates: {kand}. A longer excerpt or a frame with a card decides.")
            return (f"Bekannt: ein Video von {e.get('absender', '')}, deklariert als {d.get('inhalt', '')}"
                    + (f"; Sprecher {d.get('sprecher')}" if d.get("sprecher") else "")
                    + f". Der genaue Beitrag ist aus diesem Ausschnitt nicht eindeutig (die Sprecherbilder sind in mehreren Videos gleich); Kandidaten: {kand}. Ein längerer Ausschnitt oder ein Bild mit Karte entscheidet.")
        if lang == "en":
            return (f"Known since {wann} UTC, registered by {e.get('absender', '')}, declared as: {d.get('inhalt', '')}"
                    + (f"; anchor {d.get('sprecher')}" if d.get("sprecher") else "") + f". Entry {e.get('id')} ({'exact file' if r['status'] == 'exakt' else f'similar, {int(r.get('uebereinstimmung', 0) * 100)} % of frames'})"
                    + (f". Story: {e.get('url', '').rsplit('/media/', 1)[0]}/{e.get('beitrag')}" if e.get("beitrag") else ""))
        return (f"Bekannt seit {wann} UTC, eingetragen von {e.get('absender', '')}, deklariert als: {d.get('inhalt', '')}"
                + (f"; Sprecher {d.get('sprecher')}" if d.get("sprecher") else "") + f". Eintrag {e.get('id')} ({'exakte Datei' if r['status'] == 'exakt' else f'ähnlich, {int(r.get('uebereinstimmung', 0) * 100)} % der Standbilder'})"
                + (f". Beitrag: {e.get('url', '').rsplit('/media/', 1)[0]}/{e.get('beitrag')}" if e.get("beitrag") else ""))
    return ("Unknown: this file is not in the register of Persona AI News." if lang == "en"
            else "Unbekannt: Diese Datei steht nicht im Register von Persona AI News.")


@app.post("/api/herkunft/pruefen")
def herkunft_pruefen(file: UploadFile = File(...), lang: str = "de"):
    """Oeffentlich: Datei hochladen, Antwort bekannt/unbekannt. Kein Login, keine Speicherung der Datei."""
    import herkunft
    suffix = Path(file.filename or "x").suffix.lower()
    if suffix not in (".mp4", ".mov", ".webm", ".mkv", ".m4v", ".jpg", ".jpeg", ".png", ".webp"):
        raise HTTPException(400, "Video (mp4, mov, webm) or image (jpg, png, webp) only.")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        size = 0
        while True:
            chunk = file.file.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > _HERKUNFT_PRUEF_MAX:
                tmp.close(); Path(tmp.name).unlink(missing_ok=True)
                raise HTTPException(413, "File too large (max 120 MB).")
            tmp.write(chunk)
        pfad = Path(tmp.name)
    try:
        r = herkunft.pruefen_datei(pfad)
    finally:
        pfad.unlink(missing_ok=True)
    e = r.get("eintrag")
    return {"status": r.get("status"), "uebereinstimmung": r.get("uebereinstimmung"), "distanz": r.get("distanz"),
            "eintrag": ({k: e.get(k) for k in ("id", "beitrag", "art", "zeit", "absender", "deklaration", "url", "c2pa")} if e else None),
            "text": _herkunft_text(r, "en" if lang == "en" else "de")}


# ------------------------------ 404 als Seite (30.10.2026) ------------------------------
from starlette.exceptions import HTTPException as _StarletteHTTPException


@app.exception_handler(_StarletteHTTPException)
async def _http_fehler(request: Request, exc: _StarletteHTTPException):
    """Eine nicht gefundene Seite der Website bekommt eine Seite mit den neuesten Beitraegen statt
    einer JSON-Zeile; API, Medien und alles andere behalten die JSON-Antwort."""
    path = request.url.path
    if (exc.status_code == 404 and path.startswith("/news") and "/api/" not in path and "/media/" not in path
            and "text/html" in request.headers.get("accept", "")):
        parts = [p for p in path.split("/") if p]
        key = parts[1] if len(parts) > 1 and parts[1] in _ausgaben() and parts[1] != "de" else "de"
        try:
            with _lang(key):
                P, esc = _prefix(request), _htmlmod.escape
                ents = _blog_entries()[:6]
                inner = (f"<h1>{_t('nf_title')}</h1><p class=\"sub\">{esc(_t('nf_sub'))}</p>" + _such_form(P)
                         + '<div class="grid">' + "".join(_card_html(e, P) for e in ents) + "</div>"
                         f'<a class="all" href="{P}/">{_t("start")} →</a>')
                resp = _news_shell(request, f"{_t('nf_title')} · {_site()['name']}", inner, desc=_t("nf_sub"))
                resp.status_code = 404
                return resp
        except Exception:  # noqa: BLE001  (die Fehlerseite darf selbst nie fehlschlagen)
            pass
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))


# ------------------------------ Korrekturen (26.10.2026) -------------------------------
# Jede Korrektur steht am Beitrag (Feld `korrekturen`: Datum, Text), erscheint im Beitrag, in den
# strukturierten Daten (schema.org correction) und auf der Seite /korrekturen, neueste zuerst.
def _korrektur_html(e: dict) -> str:
    ks = [k for k in (e.get("korrekturen") or []) if isinstance(k, dict) and k.get("text")]
    if not ks:
        return ""
    esc = _htmlmod.escape
    return "".join(f'<div class="korr"><b>{esc(_t("correction_of").format(d=_datum_lang(str(k.get("datum", ""))[:10], _sprache())))}</b> {esc(k["text"])}</div>'
                   for k in reversed(ks))


@app.get("/news/korrekturen")
def news_korrekturen(request: Request):
    site, P, esc = _site(), _prefix(request), _htmlmod.escape
    rows = []
    for e in _blog_entries():
        for k in e.get("korrekturen") or []:
            if isinstance(k, dict) and k.get("text"):
                rows.append((str(k.get("datum", ""))[:10], e, k["text"]))
    rows.sort(key=lambda r: r[0], reverse=True)
    inner = f"<h1>{_t('corrections')}</h1><p class=\"sub\">{esc(_t('corrections_sub'))}</p>"
    if not rows:
        inner += f'<div class="empty">{esc(_t("corrections_none"))}</div>'
    else:
        inner += "".join(f'<div class="korr"><b>{esc(_datum_lang(d, _sprache()))}</b> · <a href="{P}/{esc(e["id"])}">{esc(e["titel"])}</a><br>{esc(t)}</div>'
                         for d, e, t in rows)
    return _news_shell(request, f"{_t('corrections')} · {site['name']}", inner, active="korrekturen", desc=_t("corrections_sub"))


@app.get("/news/r/{slug}")
def news_ressort(slug: str, request: Request, page: int = 1):
    site = _site()
    name = next((r for r in site["ressorts"] if _slug(r) == slug), None)
    if not name:
        raise HTTPException(404, _t("err_section"))
    ents = [e for e in _blog_entries() if _slug(_nfc(e.get("ressort", ""))) == slug]
    speakers = [p for p in _profiles() if p.get("sichtbar", True) and name in (p.get("ressorts") or [])]
    return _listing(request, ents, name, slug, page, sub=_t("section_sub").format(name=name),
                    extra=_speakers_html(speakers, _prefix(request)), head_extra=f"<style>{_PROFILE_CSS}</style>")


@app.get("/news/ueber")
def news_about(request: Request):
    site, esc = _site(), _htmlmod.escape
    about = site.get("about") or _t("about_default").format(name=site["name"])
    paras = "".join(f"<p>{esc(p.strip())}</p>" for p in about.split("\n") if p.strip())
    team = _team_grid_html(_prefix(request))
    return _news_shell(request, f"{_t('about')} · {site['name']}",
                       f'<h1>{_t("about")}</h1><div class="about">{paras}</div>'
                       + (f'<h1 style="margin-top:34px">{_t("our_avatars")}</h1>{team}' if team else ""),
                       head_extra=f"<style>{_PROFILE_CSS}</style>", active="ueber",
                       desc=about.split("\n")[0][:200])


_LEGAL_CSS = """
.legal h2{font-family:"Source Serif 4",Georgia,serif;font-size:19px;margin:24px 0 6px}
.legal p,.legal li{max-width:74ch;color:var(--ink)}
.legal a{color:var(--accent-dark)}
"""


def _legal_body(name: str) -> str:
    """Inhalt (ab <h1> bis <footer>) aus static/<name>.html, damit Impressum/Datenschutz
    auf der Website im hellen Rahmen mit dem Seitennamen erscheinen (eine Quelle, zwei Looks)."""
    try:
        html = (STATIC / f"{name}.html").read_text(encoding="utf-8")
    except OSError:
        return "<p>Text folgt.</p>"
    a, b = html.find("<h1"), html.find("<footer")
    body = html[a:b] if a >= 0 and b > a else html
    # Verweise untereinander bleiben relativ zum Website-Prefix (werden in der Route ersetzt)
    return body


@app.get("/news/impressum")
def news_impressum(request: Request):
    site, P = _site(), _prefix(request)
    body = _legal_body("impressum").replace('href="/datenschutz"', f'href="{P}/datenschutz"')
    return _news_shell(request, f"Impressum · {site['name']}", f'<div class="legal">{body}</div>',
                       head_extra=f"<style>{_LEGAL_CSS}</style>")


@app.get("/news/datenschutz")
def news_datenschutz(request: Request):
    site, P = _site(), _prefix(request)
    body = _legal_body("datenschutz").replace('href="/impressum"', f'href="{P}/impressum"')
    return _news_shell(request, f"Datenschutz · {site['name']}", f'<div class="legal">{body}</div>',
                       head_extra=f"<style>{_LEGAL_CSS}</style>")


@app.get("/news/nutzungsbedingungen")
def news_nutzungsbedingungen(request: Request):
    site, P = _site(), _prefix(request)
    body = (_legal_body("nutzungsbedingungen").replace('href="/impressum"', f'href="{P}/impressum"')
            .replace('href="/datenschutz"', f'href="{P}/datenschutz"'))
    return _news_shell(request, f"Nutzungsbedingungen · {site['name']}", f'<div class="legal">{body}</div>',
                       head_extra=f"<style>{_LEGAL_CSS}</style>")


@app.get("/news/feed.xml")
def news_feed(request: Request):
    site, esc, base = _site(), _htmlmod.escape, _abs_base(request)
    items = []
    for e in _blog_entries()[:50]:
        try:
            pub = _dt.datetime.fromisoformat(str(e.get("published_at"))).strftime("%a, %d %b %Y %H:%M:%S +0000")
        except ValueError:
            pub = ""
        enc = (f'<enclosure url="{base}/media/{esc(e["id"])}.mp4" type="video/mp4" length="0"/>'
               if e.get("video") else "")
        items.append(f"<item><title>{esc(e['titel'])}</title><link>{base}/{esc(e['id'])}</link>"
                     f"<guid isPermaLink=\"true\">{base}/{esc(e['id'])}</guid>"
                     f"<description>{esc(_teaser_of(e))}</description>"
                     + (f"<category>{esc(e['ressort'])}</category>" if e.get("ressort") else "")
                     + f"<pubDate>{pub}</pubDate>{enc}</item>")
    xml = ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
           f"<title>{esc(site['name'])}</title><link>{base}/</link><description>{esc(site.get('claim', ''))}</description>"
           f"<language>{_sprache()}</language>{''.join(items)}</channel></rss>")
    return Response(xml, media_type="application/rss+xml; charset=utf-8")


@app.get("/news/robots.txt")
def news_robots(request: Request):
    site = _site()
    # Sitemaps aller sichtbaren, indexierbaren Ausgaben, die auf dieser Domain liegen
    h = _host(request)
    maps = []
    for k in _ausgaben_sichtbar():
        with _lang(k):
            dom = (_ausgabe().get("domain") or _stamm_domain()).lower()
            if _site().get("indexable") and (h in (dom, "www." + dom) or not h):
                maps.append(f"Sitemap: {_ausgabe_home_abs(k)}sitemap.xml")
    body = ("User-agent: *\nAllow: /\n" + "\n".join(maps) + "\n") if site.get("indexable") and maps \
        else "User-agent: *\nDisallow: /\n"
    return Response(body, media_type="text/plain; charset=utf-8")


@app.get("/news/sitemap.xml")
def news_sitemap(request: Request):
    site, esc, base = _site(), _htmlmod.escape, _abs_base(request)
    ents = _blog_entries()
    # lastmod (26.10.2026, Search Console: 82 Seiten "gefunden, nicht indexiert"): Beitraege mit ihrem
    # Aenderungsdatum, Listen und feste Seiten mit dem Datum des juengsten Beitrags, Profile mit ihrem Stand
    def tag(s) -> str:
        return str(s or "")[:10]
    juengst = tag(ents[0].get("updated_at") or ents[0].get("published_at")) if ents else _dt.date.today().isoformat()
    urls = [(f"{base}/", juengst), (f"{base}/{_t('p_about')}", juengst), (f"{base}/{_t('p_method')}", ""),
            (f"{base}/{_t('p_work')}", ""), (f"{base}/podcast", juengst), (f"{base}/newsletter", "")] \
        + ([(f"{base}/medien", "")] if _LANG.get() == "de" else []) \
        + [(f"{base}/team/{p['slug']}", tag(p.get("updated_at"))) for p in _profiles() if p.get("sichtbar", True)] \
        + [(f"{base}/{_t('p_sec')}/{_slug(r)}", juengst) for r in site["ressorts"]] \
        + [(f"{base}/{esc(e['id'])}", tag(e.get("updated_at") or e.get("published_at"))) for e in ents]
    xml = ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           + "".join(f"<url><loc>{u}</loc>" + (f"<lastmod>{d}</lastmod>" if re.fullmatch(r"\d{4}-\d\d-\d\d", d or "") else "") + "</url>"
                     for u, d in urls) + "</urlset>")
    return Response(xml, media_type="application/xml; charset=utf-8")


@app.get("/news/{pid}")
def news_article(pid: str, request: Request):
    if _LANG.get() == "de" and pid != "de" and pid in _ausgaben():   # /<key> ohne Schraegstrich = Startseite der Ausgabe
        with _lang(pid):
            return news_page(request)
    if pid == "podcast.xml":                    # feste Seiten unter einem Segment (19.10.2026)
        return news_podcast_feed(request)
    if pid == "podcast":
        return news_podcast(request)
    if pid == "newsletter":
        return news_newsletter(request)
    if pid == _t("p_work"):
        return news_work(request)
    if pid == "llms.txt":
        return news_llms(request)
    if pid == "dossiers":
        return news_dossiers(request)
    if pid == "register":
        return news_register(request)
    if pid == "favicon.svg":
        return news_favicon()
    if pid == _t("p_korr"):
        return news_korrekturen(request)
    if pid == _indexnow_key() + ".txt":
        return Response(_indexnow_key(), media_type="text/plain; charset=utf-8")
    e = _blog_entry(pid)
    if not e:
        raise HTTPException(404, _t("err_post"))
    if _eintrag_ausgabe(e) != _LANG.get():   # Beitrag gehoert zu einer anderen Ausgabe: dorthin leiten
        from fastapi.responses import RedirectResponse
        ziel = _eintrag_ausgabe(e)
        if ziel not in _ausgaben():
            raise HTTPException(404, _t("err_post"))
        with _lang(ziel):
            return RedirectResponse(f"{_prefix(request)}/{pid}", status_code=301)
    site, esc, P, base = _site(), _htmlmod.escape, _prefix(request), _abs_base(request)
    try:
        counts = _reaction_counts()
    except sqlite3.Error:
        counts = {}
    desc = _teaser_of(e)
    og = (f'<meta property="og:type" content="article"><meta property="og:site_name" content="{esc(site["name"])}">'
          f'<meta property="og:title" content="{esc(e["titel"])}"><meta property="og:description" content="{esc(desc)}">'
          f'<meta property="og:url" content="{base}/{esc(pid)}">'
          + (f'<meta property="og:image" content="{base}/media/{esc(pid)}.jpg">' if e.get("poster") else "")
          + f'<meta name="twitter:card" content="{"summary_large_image" if e.get("poster") else "summary"}">'
          f'<link rel="canonical" href="{base}/{esc(pid)}"><script>window.__post={json.dumps(pid)};</script>'
          f'<meta property="article:published_time" content="{esc(str(e.get("published_at", "")))}">'
          + (f'<meta property="article:section" content="{esc(e["ressort"])}">' if e.get("ressort") else "")
          + _jsonld(_jsonld_article(e, site, base, _sprache())))
    ents = _blog_entries()
    inner = (f'<a class="back" href="{P}/">{_t("back_all")}</a>' + _article_html(e, counts, P)
             + _dossier_links_html(e, P, ents) + _pitch_html(P) + _verwandte_html(e, ents, P))
    return _news_shell(request, f"{e['titel']} · {site['name']}", inner, head_extra=og, desc=desc)


class NewsletterReq(BaseModel):
    email: str
    consent: bool = False
    website: str = ""      # Honeypot


# ------------------------------ Englische Fassung "How we score" ------------
# (die Routen der Ausgaben ausser de laufen ueber den Verteiler weiter unten)
def _news_method_en(request: Request):
    site, P, B = _site(), _prefix(request), _prefix_base(request)
    inner = f"""<h1>How we score</h1>
<div class="about"><p>Below our stories you will find a <b>Constructive score</b> from 0 to 100. It is an assessment by an AI model
against fixed, published criteria. It is not a grade for truth or quality and not a judgement about the authors. It measures one thing
only: how constructive is a text in the sense of constructive journalism?</p>
<h2>The four criteria</h2>
<p><b>Solution named:</b> Does the text name an answer, an approach or a step forward instead of only describing a problem?</p>
<p><b>Evidence:</b> Does the text rest on concrete facts, figures or sources?</p>
<p><b>Limits named:</b> Does the text say honestly what is still open or where the solution reaches its limits?</p>
<p><b>Tone:</b> Is the basic attitude sober or lively (good), alarmist or promotional (less good)?</p>
<h2>The scale</h2>
<p><b>60 to 100:</b> solution-oriented, evidenced, sober to encouraging. <b>40 to 59:</b> mixed, often a problem description with some
constructive elements. <b>Below 40:</b> pure problem description, alarmism or opinion without evidence.</p>
<h2>What the score cannot do</h2>
<p>It does not check facts and does not detect false reports. It assesses the text only, not pictures or videos. We apply the same
criteria and the same scale to the German edition. If you think an assessment is wrong, you can reach us through the
<a href="{B}/impressum">legal notice</a> (in German).</p></div>"""
    return _news_shell(request, f"How we score · {site['name']}", inner, active="medien",
                       desc="The criteria behind the Constructive score of Persona AI News.")


# ------------------------------ Podcast und Newsletter (19.10.2026) ------
# Podcast: jede Tonspur eines Beitrags (<pid>.mp3, _extract_audio) als Folge, ein RSS-Feed je Ausgabe.
# Newsletter: Modul newsletter.py (Double Opt-in, Tagesbrief 18:30 Europe/Berlin per Timer).
PODCAST_COVER = {"de": "podcast_cover.png", "en": "podcast_cover_en.png"}
PODCAST_OWNER = "max@letstalk.team"   # Impressum


def _podcast_items(limit: int = 100) -> list:
    return [e for e in _blog_entries() if e.get("audio")][:limit]


def news_podcast_cover():
    key = _LANG.get()
    p = STATIC / f"podcast_cover_{key}.png"          # eigenes Cover der Ausgabe, sonst das ihrer Sprache
    if not p.is_file():
        p = STATIC / PODCAST_COVER.get(_sprache(), PODCAST_COVER["de"])
    if not p.is_file():
        raise HTTPException(404, "Kein Cover.")
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


def news_podcast_feed(request: Request):
    site, esc, base, lang = _site(), _htmlmod.escape, _abs_base(request), _sprache()
    key = _LANG.get()
    items = []
    for e in _podcast_items():
        try:
            pub = _dt.datetime.fromisoformat(str(e.get("published_at"))).strftime("%a, %d %b %Y %H:%M:%S +0000")
        except ValueError:
            pub = ""
        mp3 = BLOG_DIR / (e["id"] + ".mp3")
        try:
            size = mp3.stat().st_size
        except OSError:
            continue
        dauer = int(e.get("audio_s") or 0)
        desc = _teaser_of(e) + f" {base}/{e['id']}"
        items.append(
            f"<item><title>{esc(e['titel'])}</title><link>{base}/{esc(e['id'])}</link>"
            f"<guid isPermaLink=\"false\">persona-{key}-{esc(e['id'])}</guid><pubDate>{pub}</pubDate>"
            f"<description>{esc(desc)}</description><itunes:summary>{esc(_teaser_of(e))}</itunes:summary>"
            f"<enclosure url=\"{base}/media/{esc(e['id'])}.mp3\" length=\"{size}\" type=\"audio/mpeg\"/>"
            + (f"<itunes:duration>{dauer}</itunes:duration>" if dauer else "")
            + (f"<itunes:image href=\"{base}/media/{esc(e['id'])}.jpg\"/>" if e.get("poster") else "")
            + "<itunes:explicit>false</itunes:explicit></item>")
    zusatz = _ausgabe().get("titel_zusatz") or ""
    titel = f"{site['name']}" + (f" {zusatz}" if zusatz else "")
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">'
           f"<channel><title>{esc(titel)}</title><link>{base}/</link><language>{lang}</language>"
           f"<description>{esc(site.get('claim', ''))}</description><itunes:summary>{esc(site.get('claim', ''))}</itunes:summary>"
           f"<itunes:author>{esc(site['name'])}</itunes:author><itunes:owner><itunes:name>{esc(site['name'])}</itunes:name>"
           f"<itunes:email>{PODCAST_OWNER}</itunes:email></itunes:owner>"
           f'<itunes:image href="{base}/podcast/cover.png"/><itunes:category text="News"/><itunes:explicit>false</itunes:explicit>'
           f'<itunes:type>episodic</itunes:type><atom:link href="{base}/podcast.xml" rel="self" type="application/rss+xml"/>'
           + "".join(items) + "</channel></rss>")
    return Response(xml, media_type="application/rss+xml; charset=utf-8")


def news_podcast(request: Request):
    site, esc, P, base = _site(), _htmlmod.escape, _prefix(request), _abs_base(request)
    eps = "".join(
        f'<div class="card" data-post="{esc(e["id"])}"><div><h3><a href="{P}/{esc(e["id"])}">{esc(e["titel"])}</a></h3>'
        f'<p>{esc(_teaser_of(e))}</p><audio controls preload="none" src="{P}/media/{esc(e["id"])}.mp3" style="width:100%"></audio>'
        f'{_meta_html(e, P)}</div></div>' for e in _podcast_items(20))
    inner = (f'<h1>{_t("pc_title")}</h1><p class="sub">{_t("pc_sub")}</p>'
             f'<p><code style="font-size:15px;background:var(--surface);border:1px solid var(--line);padding:8px 12px;border-radius:8px;display:inline-block">{base}/podcast.xml</code></p>'
             f'<p class="sub">{_t("pc_hint")}</p>'
             f'<div class="rowhead"><h2>{_t("pc_episodes")}</h2></div><div class="grid">{eps}</div>')
    return _news_shell(request, f"{_t('pc_title')} · {site['name']}", inner, active="podcast",
                       desc=_t("pc_sub")[:200],
                       head_extra=f'<link rel="alternate" type="application/rss+xml" title="{esc(site["name"])} Podcast" href="{base}/podcast.xml">')


@app.get("/news/podcast/cover.png")
def news_podcast_cover_de():
    return news_podcast_cover()


_nl_hits: dict[str, list[float]] = {}


def _nl_form_html(P: str, kompakt: bool = False) -> str:
    """Das Anmeldeformular; kompakt = eine Zeile fuer Start- und Artikelseite (30.10.2026)."""
    esc = _htmlmod.escape
    fid = "nlk" if kompakt else "nl"
    if kompakt:
        form = (f'<form id="{fid}" class="nlmini"><input type="email" name="email" required maxlength="200" placeholder="{esc(_t("nl_mail"))}">'
                f'<button type="submit">{_t("nl_button")}</button>'
                f'<span style="display:none"><input name="website" tabindex="-1" autocomplete="off"></span>'
                f'<label class="nlc"><input type="checkbox" name="consent" required> {esc(_t("nl_consent_kurz"))}</label>'
                f'<span id="{fid}msg" class="nlmsg"></span></form>')
    else:
        form = (f'<form id="{fid}" class="about" style="max-width:520px"><p><label>{_t("nl_mail")}<br>'
                f'<input type="email" name="email" required maxlength="200" style="width:100%;padding:10px;border:1px solid var(--line2);border-radius:8px;font-size:16px"></label></p>'
                f'<p style="display:none"><input name="website" tabindex="-1" autocomplete="off"></p>'
                f'<p><label><input type="checkbox" name="consent" required> {esc(_t("nl_consent"))}</label></p>'
                f'<p><button type="submit" style="background:var(--accent);color:#fff;border:0;border-radius:8px;padding:10px 18px;font-size:16px;cursor:pointer">{_t("nl_button")}</button></p>'
                f'<p id="{fid}msg" class="sub"></p></form>')
    return form + (f"<script>document.getElementById('{fid}').addEventListener('submit',async function(ev){{ev.preventDefault();var f=ev.target;var m=document.getElementById('{fid}msg');"
                   f"try{{var r=await fetch('{P}/newsletter/anmelden',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email:f.email.value,consent:f.consent.checked,website:f.website.value}})}});"
                   f"var d=await r.json();m.textContent=r.ok&&d.ok?{json.dumps(_t('nl_sent'))}:(d.detail||{json.dumps(_t('nl_error'))});if(r.ok&&d.ok)f.reset();}}catch(e){{m.textContent={json.dumps(_t('nl_error'))};}}}});</script>")


def _pitch_html(P: str) -> str:
    esc = _htmlmod.escape
    return (f'<div class="pitch"><p>{esc(_t("pitch"))} <a href="{P}/{_t("p_work")}">{_t("work")} →</a></p>'
            f'<p class="pitch2">{esc(_t("pitch_nl"))}</p>{_nl_form_html(P, kompakt=True)}</div>')


def news_newsletter(request: Request, meldung: str = "") -> HTMLResponse:
    site, esc, P = _site(), _htmlmod.escape, _prefix(request)
    form = _nl_form_html(P)
    inner = f'<h1>{_t("nl_title")}</h1><p class="sub">{_t("nl_sub")}</p>' + (f'<p class="about"><b>{esc(meldung)}</b></p>' if meldung else "") + form
    return _news_shell(request, f"{_t('nl_title')} · {site['name']}", inner, active="newsletter", desc=_t("nl_sub")[:200])


@app.post("/news/newsletter/anmelden")
def news_newsletter_signup(req: NewsletterReq, request: Request):
    import time as _t_
    if req.website.strip():
        return {"ok": True}
    email = req.email.strip().lower()
    if len(email) > 200 or not _EMAIL_RE.match(email):
        raise HTTPException(400, _t("nl_error"))
    if not req.consent:
        raise HTTPException(400, _t("nl_consent"))
    ip = _client_ip(request)
    now = _t_.time()
    with _lock:
        hits = [t for t in _nl_hits.get(ip, []) if now - t < _LEAD_WINDOW]
        if len(hits) >= _LEAD_MAX:
            raise HTTPException(429, "Zu viele Anfragen. Bitte später erneut versuchen.")
        hits.append(now)
        _nl_hits[ip] = hits
    lang = _LANG.get()
    import newsletter

    def go():
        try:
            newsletter.anmelden(email, lang, ip)
        except Exception as e:  # noqa: BLE001
            print(f"[newsletter] Anmeldung fehlgeschlagen: {type(e).__name__}: {e}", flush=True)
    threading.Thread(target=go, daemon=True).start()
    return {"ok": True}


@app.get("/news/newsletter/bestaetigen/{token}")
def news_newsletter_confirm(token: str, request: Request):
    import newsletter
    ok = newsletter.bestaetigen(token[:64])
    return news_newsletter(request, _t("nl_confirmed") if ok else _t("nl_badtoken"))


@app.get("/news/newsletter/abmelden/{token}")
def news_newsletter_unsub(token: str, request: Request):
    import newsletter
    ok = newsletter.abmelden(token[:64])
    return news_newsletter(request, _t("nl_unsubscribed") if ok else _t("nl_badtoken"))


# ------------------------------ Weitere Ausgaben: Verteiler (/news/<key>/...) ----
# Jede Ausgabe ausser de (en und alle Mandanten aus data/ausgaben.json) laeuft unter /news/<key>/...
# Der Verteiler bildet die Pfade auf dieselben Seitenfunktionen ab und setzt die Ausgabe in der
# ContextVar; die Pfadnamen (alle/all, r/section, ueber/about, ...) kommen aus _UI nach der Sprache
# der Ausgabe. Eine neue Ausgabe braucht keinen Neustart. Er steht hinter allen /news/...-Routen
# der Stammseite, damit deren feste Pfade (media, team, newsletter, podcast) Vorrang behalten.
def _ausgabe_oder_404(key: str) -> str:
    if key == "de" or key not in _ausgaben():
        raise HTTPException(404, "Not found.")
    return key


@app.get("/news/{key}/")
def news_edition_home(key: str, request: Request):
    _ausgabe_oder_404(key)
    with _lang(key):
        return news_page(request)


@app.get("/news/{key}/{rest:path}")
def news_edition_get(key: str, rest: str, request: Request, page: int = 1):
    _ausgabe_oder_404(key)
    parts = [p for p in rest.split("/") if p]
    if not parts or len(parts) > 3 or any(p in ("..", ".") for p in parts):
        raise HTTPException(404, "Not found.")
    with _lang(key):
        if len(parts) == 1:
            p0 = parts[0]
            if p0 == _t("p_all"):
                return news_all(request, page)
            if p0 == _t("p_search"):
                return news_search(request, request.query_params.get("q", ""), page)
            if p0 == _t("p_about"):
                return news_about(request)
            if p0 == "team":
                return news_team(request)
            if p0 == "feed.xml":
                return news_feed(request)
            if p0 == "sitemap.xml":
                return news_sitemap(request)
            if p0 == "robots.txt":
                return news_robots(request)
            if p0 == _t("p_method"):
                return news_method(request)
            if p0 == "impressum":
                return news_impressum(request)
            if p0 == "datenschutz":
                return news_datenschutz(request)
            if p0 == "nutzungsbedingungen":
                return news_nutzungsbedingungen(request)
            return news_article(p0, request)       # Beitrag, podcast, newsletter, so-arbeiten-wir, llms.txt, dossiers, korrekturen
        if len(parts) == 2:
            p0, p1 = parts
            if p0 == _t("p_sec"):
                return news_ressort(p1, request, page)
            if p0 == "dossier":
                return news_dossier(p1, request)
            if p0 == "register" and p1 == "schluessel.pem":
                return news_register_key()
            if p0 == "k":
                return news_kennzeichen(p1, request)
            if p0 == "team":
                return news_profile(p1, request)
            if p0 == "media":
                return news_media(p1)
            if p0 == "podcast" and p1 == "cover.png":
                return news_podcast_cover()
        if len(parts) == 3:
            p0, p1, p2 = parts
            if p0 == "register" and p1 == "anker":
                return news_register_anker(p2)
            if p0 == "media" and p1 == "avatar":
                return news_profile_photo(p2)
            if p0 == "newsletter" and p1 == "bestaetigen":
                return news_newsletter_confirm(p2, request)
            if p0 == "newsletter" and p1 == "abmelden":
                return news_newsletter_unsub(p2, request)
    raise HTTPException(404, "Not found.")


@app.post("/news/{key}/newsletter/anmelden")
def news_edition_newsletter_signup(key: str, req: NewsletterReq, request: Request):
    _ausgabe_oder_404(key)
    with _lang(key):
        return news_newsletter_signup(req, request)


# ------------------------------ Ausgaben-Register (Admin-API) ---------------
# Die Kette legt neue Ausgaben hierueber an (kette ausgabe neu <key>): Register, site_<key>.json,
# Feedliste fuer den Newsroom-Scan, Ticker. Profile und Beitraege kommen danach ueber die
# bestehenden Endpunkte mit `ausgabe: <key>`.
class AusgabeReq(BaseModel):
    key: str
    sprache: str = "de"
    name: str = ""
    claim: str = ""
    about: str = ""
    ressorts: list[str] = []
    domain: str = ""             # eigene Domain; leer = Stammdomain mit Pfad
    prefix: str = ""             # Pfad auf der Stammdomain, Standard /<key>
    label: str = ""              # Kuerzel im Sprachwechsel der Navigation
    titel_zusatz: str = ""       # Zusatz im Podcast-Titel
    sichtbar: bool = True        # in Navigation, hreflang, llms.txt, robots
    indexable: bool = False      # Suchmaschinen; neue Ausgaben starten unsichtbar
    fokus: str = ""              # Schwerpunkt fuer den Newsroom-Scan (z. B. "Region Winterthur und Zuerich")
    feeds: dict[str, list[str]] | None = None   # {Ressort: [RSS-URL, ...]} fuer newsroom_feeds_<key>.json
    ticker_auto: bool = True


def _feeds_file(key: str) -> Path:
    return _AVATAR_TWIN / (f"newsroom_feeds_{key}.json" if key != "de" else "newsroom_feeds.json")


@app.get("/api/ausgaben")
def ausgaben_list(request: Request):
    require_user(request)
    out = []
    for k, a in _ausgaben().items():
        with _lang(k):
            s = _site()
            out.append({"key": k, **{f: a.get(f) for f in _AUSGABE_FELDER}, "name": s["name"], "claim": s.get("claim", ""),
                        "ressorts": s["ressorts"], "indexable": bool(s.get("indexable")), "home": _ausgabe_home_abs(k),
                        "feeds": _feeds_file(k).is_file(), "beitraege": len(_blog_entries()),
                        "profile": len([p for p in _profiles() if p.get("sichtbar", True)])})
    return {"ausgaben": out}


def _require_editor(request: Request) -> dict:
    """Redaktion oder Admin (die Kette veroeffentlicht mit einem Redaktionskonto)."""
    u = require_user(request)
    if not _is_editor(u):
        raise HTTPException(403, "Editors only.")
    return u


@app.post("/api/ausgaben")
def ausgaben_save(req: AusgabeReq, request: Request):
    _require_editor(request)
    key = req.key.strip().lower()
    if not _AUSGABE_RE.match(key) or key in ("de", "news", "api", "media", "team", "static"):
        raise HTTPException(400, "Invalid key (a-z, 0-9, 2 to 16 characters).")
    if key == "en":
        raise HTTPException(400, "The English edition is built in; edit data/site_en.json.")
    sprache = "en" if req.sprache.strip().lower().startswith("en") else "de"
    ressorts = [_nfc(r)[:40] for r in req.ressorts if _nfc(r)][:20]
    if not ressorts:
        raise HTTPException(400, "At least one Ressort is required.")
    domain = re.sub(r"[^a-z0-9.-]", "", req.domain.strip().lower())[:80]
    prefix = "/" + re.sub(r"[^a-z0-9-]", "", (req.prefix or key).strip().lower().strip("/"))[:20]
    neu = key not in _ausgaben()
    # 1. Register
    try:
        reg = json.loads(AUSGABEN_FILE.read_text(encoding="utf-8")) if AUSGABEN_FILE.is_file() else {}
    except (ValueError, OSError):
        reg = {}
    if not isinstance(reg, dict):
        reg = {}
    reg[key] = {"sprache": sprache, "prefix": prefix, "domain": domain or _stamm_domain(), "site": f"site_{key}.json",
                "label": (req.label.strip() or key.upper())[:6], "titel_zusatz": req.titel_zusatz.strip()[:40],
                "sichtbar": bool(req.sichtbar), "angelegt": reg.get(key, {}).get("angelegt") or _dt.date.today().isoformat()}
    AUSGABEN_FILE.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
    # 2. site_<key>.json (vorhandene Werte bleiben, wenn das Feld leer kommt)
    sf = DATA / f"site_{key}.json"
    try:
        site = json.loads(sf.read_text(encoding="utf-8")) if sf.is_file() else {}
    except (ValueError, OSError):
        site = {}
    default = _SITE_EN_DEFAULT if sprache == "en" else _SITE_DEFAULT
    site = {**{k: v for k, v in default.items()}, **(site if isinstance(site, dict) else {})}
    site.update({"name": _nfc(req.name)[:80] or site.get("name") or default["name"],
                 "claim": _nfc(req.claim)[:200] or site.get("claim", ""), "about": _nfc(req.about)[:4000] or site.get("about", ""),
                 "ressorts": ressorts, "domain": domain or _stamm_domain(), "indexable": bool(req.indexable),
                 "fokus": _nfc(req.fokus)[:400], "sprache": sprache})
    sf.write_text(json.dumps(site, ensure_ascii=False, indent=2), encoding="utf-8")
    # 3. Feeds und Ticker
    if req.feeds:
        feeds = {_nfc(k)[:40]: [u.strip()[:300] for u in v if isinstance(u, str) and u.strip().startswith("http")][:40]
                 for k, v in req.feeds.items() if isinstance(v, list)}
        _feeds_file(key).write_text(json.dumps(feeds, ensure_ascii=False, indent=2), encoding="utf-8")
    tf = DATA / f"ticker_{key}.json"
    if not tf.is_file():
        tf.write_text(json.dumps({"auto": bool(req.ticker_auto), "max_age_hours": 24, "items": [], "hidden": []},
                                 ensure_ascii=False, indent=1), encoding="utf-8")
    (DATA / f"newsroom_{key}").mkdir(parents=True, exist_ok=True)
    print(f"[ausgaben] {'angelegt' if neu else 'aktualisiert'}: {key} ({sprache}, {reg[key]['domain']}{prefix})", flush=True)
    return {"ok": True, "key": key, "neu": neu, "home": _ausgabe_home_abs(key), "feeds": _feeds_file(key).is_file(),
            "site": sf.name}


@app.delete("/api/ausgaben/{key}")
def ausgaben_delete(key: str, request: Request):
    """Nimmt eine Ausgabe aus dem Register; ihre Dateien wandern nach data/ausgaben_alt/<key>/ (Beitraege
    und Profile bleiben, sind aber nicht mehr erreichbar, bis die Ausgabe wieder angelegt wird)."""
    _require_editor(request)
    key = key.strip().lower()
    if key in _AUSGABEN_FEST or key not in _ausgaben():
        raise HTTPException(404, "Unknown or built-in edition.")
    reg = json.loads(AUSGABEN_FILE.read_text(encoding="utf-8")) if AUSGABEN_FILE.is_file() else {}
    reg.pop(key, None)
    AUSGABEN_FILE.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
    alt = DATA / "ausgaben_alt" / f"{key}_{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%d%H%M')}"
    alt.mkdir(parents=True, exist_ok=True)
    import shutil
    for p in (DATA / f"site_{key}.json", DATA / f"ticker_{key}.json", _feeds_file(key), DATA / f"newsroom_{key}"):
        if p.exists():
            shutil.move(str(p), str(alt / p.name))
    print(f"[ausgaben] entfernt: {key} -> {alt}", flush=True)
    return {"ok": True, "key": key, "archiv": str(alt)}


# ------------------------------ Branding ----------------------------
_DEF_BRAND = {"name": "", "title": "", "accent": "e9b96a", "logo": "",
              "logo_pos": "tr", "logo_scale": 0.14, "logo_opacity": 0.9,
              "lowerthird_seconds": 4}


def _brand_file(uid: int) -> Path:
    return user_dir(uid) / "branding.json"


def _logo_file(uid: int) -> Path:
    return user_dir(uid) / "branding_logo.png"


def _load_brand(uid: int) -> dict:
    p = _brand_file(uid)
    cfg = dict(_DEF_BRAND)
    if p.is_file():
        try:
            cfg.update(json.loads(p.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            pass
    return cfg


class BrandingReq(BaseModel):
    name: str = ""
    title: str = ""
    accent: str = "e9b96a"
    logo_pos: str = "tr"
    lowerthird_seconds: float = 4


@app.get("/api/branding")
def branding_get(request: Request):
    u = require_user(request)
    cfg = _load_brand(u["id"])
    return {"name": cfg["name"], "title": cfg["title"], "accent": cfg["accent"],
            "logo_pos": cfg["logo_pos"], "lowerthird_seconds": cfg["lowerthird_seconds"],
            "has_logo": bool(cfg.get("logo")) and _logo_file(u["id"]).is_file()}


@app.post("/api/branding")
def branding_save(req: BrandingReq, request: Request):
    u = require_user(request)
    acc = req.accent.lstrip("#").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{6}", acc):
        raise HTTPException(400, "Accent color must be a 6-digit hex value (e.g. e9b96a).")
    if req.logo_pos not in ("tl", "tr", "bl", "br"):
        raise HTTPException(400, "Invalid logo position.")
    sec = max(0.0, min(15.0, float(req.lowerthird_seconds)))
    cfg = _load_brand(u["id"])
    cfg.update(name=req.name.strip()[:80], title=req.title.strip()[:80],
               accent=acc, logo_pos=req.logo_pos, lowerthird_seconds=sec)
    user_dir(u["id"]).mkdir(parents=True, exist_ok=True)
    _brand_file(u["id"]).write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": True}


@app.post("/api/branding/logo")
def branding_logo(request: Request, file: UploadFile = File(...)):
    u = require_user(request)
    raw = file.file.read()
    if len(raw) > 8 * 1024 * 1024:
        raise HTTPException(400, "Logo too large (max 8 MB).")
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "Not a valid image (PNG with transparency recommended).")
    img.save(_logo_file(u["id"]))                 # normalisiert auf PNG/RGBA
    cfg = _load_brand(u["id"])
    cfg["logo"] = "branding_logo.png"
    _brand_file(u["id"]).write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "has_logo": True}


@app.delete("/api/branding/logo")
def branding_logo_delete(request: Request):
    u = require_user(request)
    _logo_file(u["id"]).unlink(missing_ok=True)
    cfg = _load_brand(u["id"])
    cfg["logo"] = ""
    _brand_file(u["id"]).write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "has_logo": False}


@app.get("/api/branding/logo")
def branding_logo_get(request: Request):
    u = require_user(request)
    p = _logo_file(u["id"])
    if not p.is_file():
        raise HTTPException(404, "No logo.")
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "no-cache"})


# ------------------------------ Framing -----------------------------
def _load_json(p) -> dict:
    try:
        return json.loads(Path(p).read_text(encoding="utf-8")) if Path(p).exists() else {}
    except (ValueError, OSError):
        return {}


def _find_avatar(uid: int, preset: str):
    d = user_dir(uid) / "presets"
    if d.is_dir():
        for f in d.iterdir():
            if f.stem == preset and f.suffix.lower() in VIDEO_EXT:
                return f
    return None


@app.get("/api/framing")
def framing_get(preset: str, request: Request):
    u = require_user(request)
    if preset not in user_avatars(u["id"]):
        raise HTTPException(404, "Avatar not found.")
    s = _load_json(user_dir(u["id"]) / "presets" / "settings.json").get(preset, {})
    return {"zoom": s.get("zoom", 1.3), "y_frac": s.get("y_frac", 0.2),
            "tone": s.get("tone", ""), "brightness": s.get("brightness", 0.0)}


@app.get("/api/framing/preview")
def framing_preview(preset: str, request: Request,
                    zoom: float = 1.3, y_frac: float = 0.2, t: float = 2.0):
    u = require_user(request)
    vid = _find_avatar(u["id"], preset)
    if not vid:
        raise HTTPException(404, "Avatar not found.")
    zoom = max(1.0, min(4.0, zoom)); y_frac = max(0.0, min(1.0, y_frac)); t = max(0.0, t)
    pr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(vid)],
                        capture_output=True, text=True)
    try:
        w, h = (int(x) for x in pr.stdout.strip().split("x"))
    except ValueError:
        raise HTTPException(500, "Video not readable.")
    cw = int(round(w / zoom)); cw -= cw % 2
    ch = int(round(h / zoom)); ch -= ch % 2
    x = ((w - cw) // 2); x -= x % 2
    y = int(round((h - ch) * y_frac)); y -= y % 2
    out = user_dir(u["id"]) / "_framing.jpg"
    subprocess.run(["ffmpeg", "-y", "-ss", str(t), "-i", str(vid), "-frames:v", "1",
                    "-vf", f"crop={cw}:{ch}:{x}:{y},scale=-2:640", "-q:v", "3", str(out)],
                   capture_output=True, text=True)
    if not out.is_file():
        raise HTTPException(500, "Preview failed.")
    return FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


def _look_preview_bg(uid, vid, bg_img, cw, ch, x, y, w, h, zoom, y_frac, g, t, out):
    """Standbild-Vorschau MIT Hintergrund: Avatar-Frame freistellen (gecacht pro
    Avatar+Framing), hochzoomen + unten verankern, vor den Hintergrund legen, Look drueber."""
    import hashlib
    import grade
    import matte_image
    from PIL import Image
    presets = user_dir(uid) / "presets"
    frame = user_dir(uid) / "_lookframe.jpg"
    subprocess.run(["ffmpeg", "-y", "-ss", str(t), "-i", str(vid), "-frames:v", "1",
                    "-vf", f"crop={cw}:{ch}:{x}:{y},scale={w}:{h},setsar=1", "-q:v", "3", str(frame)],
                   capture_output=True, text=True)
    if not frame.is_file():
        return False
    key = hashlib.md5(f"{vid.name}|{zoom:.3f}|{y_frac:.3f}".encode()).hexdigest()[:12]
    cutout = presets / f"_cut_{key}.png"
    if not cutout.is_file():
        matte_image.cutout(str(frame), str(cutout))   # ~5 s, danach gecacht
    im = Image.open(cutout).convert("RGBA")
    bb = im.getchannel("A").getbbox()
    if not bb:
        return False
    sx, sy, sw, sh = bb[0], bb[1], bb[2] - bb[0], bb[3] - bb[1]
    hr = 0.07
    sc = max(1.0, min(2.2, h * (1 - hr) / sh)) if sh > 0 else 1.0
    w2 = int(round(w * sc)); w2 -= w2 % 2
    h2 = int(round(h * sc)); h2 -= h2 % 2
    tx = int(round(w / 2 - (sx + sw / 2) * sc))
    ty = int(round(hr * h - sy * sc))
    if not os.path.exists(bg_img):
        return False
    fc = (f"[0:v]{grade.bg_cover_filter(w, h)}[bg];[1:v]scale={w2}:{h2}[fg];"
          f"[bg][fg]overlay={tx}:{ty}[c];[c]" + (g + "," if g else "") + "scale=-2:640[v]")
    subprocess.run(["ffmpeg", "-y", "-i", bg_img, "-i", str(cutout), "-filter_complex", fc,
                    "-map", "[v]", "-frames:v", "1", "-q:v", "3", str(out)],
                   capture_output=True, text=True)
    return out.is_file()


@app.get("/api/look/preview")
def look_preview(preset: str, request: Request, look: str = "warm", green: str = "off",
                 vignette: float = 0.4, contrast: float = 0.0, warmth: float = 0.0,
                 brightness: float = 0.0, background: str = "none", t: float = 2.0):
    u = require_user(request)
    vid = _find_avatar(u["id"], preset)
    if not vid:
        raise HTTPException(404, "Avatar not found.")
    if look not in ("subtle", "warm", "strong", "none"):
        look = "warm"
    if green not in ("off", "mild", "strong"):
        green = "strong"
    # crop per the avatar's SAVED framing so the preview matches the real render
    s = _load_json(user_dir(u["id"]) / "presets" / "settings.json").get(preset, {})
    zoom = max(1.0, min(4.0, float(s.get("zoom", 1.3))))
    y_frac = max(0.0, min(1.0, float(s.get("y_frac", 0.2))))
    pr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(vid)],
                        capture_output=True, text=True)
    try:
        w, h = (int(x) for x in pr.stdout.strip().split("x"))
    except ValueError:
        raise HTTPException(500, "Video not readable.")
    cw = int(round(w / zoom)); cw -= cw % 2
    ch = int(round(h / zoom)); ch -= ch % 2
    x = ((w - cw) // 2); x -= x % 2
    y = int(round((h - ch) * y_frac)); y -= y % 2
    import grade
    g = grade.build_grade(look, green, vignette, contrast, warmth, brightness)
    out = user_dir(u["id"]) / "_look.jpg"

    # Mit Hintergrund: Avatar freistellen + verankert davor setzen (Fallback: normale Look-Vorschau)
    bg_img = None
    if background != "none":
        if str(background).startswith("u:"):
            p = _user_bg_dir(u["id"]) / (str(background)[2:] + ".jpg")
            bg_img = str(p) if p.is_file() else None
        elif background in grade.BACKGROUNDS:
            bg_img = grade.background_path(background, _AVATAR_TWIN)
    if bg_img:
        try:
            if _look_preview_bg(u["id"], vid, bg_img, cw, ch, x, y, w, h,
                                zoom, y_frac, g, float(max(0.0, t)), out):
                return FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
        except Exception:  # noqa: BLE001  (Matte-/Netzfehler -> ohne Hintergrund weiter)
            pass

    vf = f"crop={cw}:{ch}:{x}:{y}"
    if g:
        vf += "," + g
    vf += ",scale=-2:640"
    subprocess.run(["ffmpeg", "-y", "-ss", str(max(0.0, t)), "-i", str(vid), "-frames:v", "1",
                    "-vf", vf, "-q:v", "3", str(out)], capture_output=True, text=True)
    if not out.is_file():
        raise HTTPException(500, "Preview failed.")
    return FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


class FramingReq(BaseModel):
    preset: str
    zoom: float = 1.3
    y_frac: float = 0.2


@app.post("/api/framing")
def framing_save(req: FramingReq, request: Request):
    u = require_user(request)
    if req.preset not in user_avatars(u["id"]):
        raise HTTPException(404, "Avatar not found.")
    zoom = max(1.0, min(4.0, req.zoom)); yf = max(0.0, min(1.0, req.y_frac))
    sp = user_dir(u["id"]) / "presets" / "settings.json"
    settings = _load_json(sp)
    cur = settings.get(req.preset, {})
    cur.update(zoom=round(zoom, 2), y_frac=round(yf, 2))
    settings[req.preset] = cur
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": True}


# ------------------------------ Avatar-Upload -----------------------
_CONSENT_TEXT = ("Ich bestaetige, dass ich das Recht habe, Gesicht und Stimme dieser "
                 "Person fuer KI-generierte Videos zu verwenden.")


def _record_consent(presets: Path, name: str, by_user: str):
    rec = {"avatar": name, "by_user": by_user,
           "at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
           "statement": _CONSENT_TEXT}
    cf = presets / "consent.json"
    try:
        recs = json.loads(cf.read_text(encoding="utf-8")) if cf.exists() else []
        if not isinstance(recs, list):
            recs = []
    except (ValueError, OSError):
        recs = []
    recs.append(rec)
    cf.write_text(json.dumps(recs, indent=2, ensure_ascii=False), encoding="utf-8")


def _ingest_avatar(uid: int, name: str, upload: UploadFile,
                   by_user: str = "", consent: bool = False) -> dict:
    if not consent:
        raise HTTPException(400, "Consent required.")
    name = (name or "").strip()[:60]
    if not name or any(c in name for c in ("/", "\\", "..")):
        raise HTTPException(400, "Invalid avatar name.")
    ext = Path(upload.filename or "").suffix.lower()
    if ext not in VIDEO_EXT:
        raise HTTPException(400, "Not a video format (mp4/mov/m4v/webm/mkv).")
    raw = upload.file.read()
    if len(raw) > 300 * 1024 * 1024:
        raise HTTPException(400, "Video too large (max 300 MB).")
    presets = user_dir(uid) / "presets"
    presets.mkdir(parents=True, exist_ok=True)
    dst = presets / (name + ext)
    dst.write_bytes(raw)
    tmp = presets / "_clone.mp3"
    p = subprocess.run(["ffmpeg", "-y", "-i", str(dst), "-vn", "-ac", "1", "-ar", "44100",
                        str(tmp)], capture_output=True, text=True)
    if p.returncode != 0 or not tmp.is_file():
        dst.unlink(missing_ok=True)
        raise HTTPException(500, "Audio extraction failed (ffmpeg).")
    try:
        import elevenlabs_voice as ev
        voice_id = ev.clone_voice(str(tmp), f"persona:{name}")
    except Exception as e:  # noqa: BLE001
        dst.unlink(missing_ok=True)
        raise HTTPException(502, f"Voice cloning failed: {str(e)[:200]}")
    finally:
        tmp.unlink(missing_ok=True)
    vf = presets / "voices.json"
    voices = _load_json(vf)
    voices[name] = voice_id
    vf.write_text(json.dumps(voices, indent=2, ensure_ascii=False), encoding="utf-8")
    _record_consent(presets, name, by_user or "unknown")
    return {"name": name, "voice_id": voice_id}


@app.post("/api/avatar")
def avatar_self(request: Request, name: str = Form(...), file: UploadFile = File(...),
                consent: bool = Form(False)):
    u = require_user(request)                     # jeder in sein EIGENES Konto
    r = _ingest_avatar(u["id"], name, file, by_user=u["username"], consent=consent)
    return {"ok": True, "user": u["username"], **r}


def require_admin(request: Request):
    u = require_user(request)
    if u.get("role") != "admin":
        raise HTTPException(403, "Admin only.")
    return u


@app.get("/api/admin/users")
def admin_users(request: Request):
    require_admin(request)
    return {"users": [{"id": x["id"], "username": x["username"]} for x in auth.list_users()]}


@app.post("/api/admin/avatar")
def admin_avatar(request: Request, target: str = Form(...), name: str = Form(...),
                 file: UploadFile = File(...), consent: bool = Form(False)):
    adm = require_admin(request)                    # Admin in JEDES Konto
    tu = next((x for x in auth.list_users()
               if x["username"] == target.lower().strip() or str(x["id"]) == str(target)), None)
    if not tu:
        raise HTTPException(404, "Target account not found.")
    r = _ingest_avatar(tu["id"], name, file, by_user=adm["username"], consent=consent)
    return {"ok": True, "user": tu["username"], **r}


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


# ------------------------------ Newsroom -----------------------------
NEWSROOM_DIR = DATA / "newsroom"
_newsroom_lock = threading.Lock()


def _newsroom_dir() -> Path:
    key = _LANG.get()
    return DATA / f"newsroom_{key}" if key != "de" else NEWSROOM_DIR


def _newsroom_latest() -> dict:
    """Juengster Scan der aktuellen Ausgabe (de: data/newsroom, sonst data/newsroom_<key>)."""
    d = _newsroom_dir()
    d.mkdir(parents=True, exist_ok=True)
    files = sorted(d.glob("????-??-??.json"))
    if not files:
        return {"date": None, "topics": []}
    return _load_json(files[-1]) or {"date": None, "topics": []}


_SCANS = ("natur", "aktuell")   # weitere Scans neben den Ausgaben: data/newsroom_<scan> (Natur-Scan 21.10., Spur Aktuell 26.10.2026)


@app.get("/api/newsroom")
def newsroom_get(request: Request, lang: str = "de", ausgabe: str = "", scan: str = ""):
    """ausgabe (oder lang): Schluessel der Ausgabe, Standard de (Scan in data/newsroom_<key>).
    scan: ein weiterer Scan der Plattform, z. B. natur (data/newsroom_natur)."""
    require_user(request)
    if scan:
        if scan not in _SCANS:
            raise HTTPException(404, f"Unknown scan: {scan}")
        d = DATA / f"newsroom_{scan}"
        d.mkdir(parents=True, exist_ok=True)
        files = sorted(d.glob("????-??-??.json"))
        return (_load_json(files[-1]) if files else None) or {"date": None, "topics": []}
    with _lang(_ausgabe_key(ausgabe or lang)):
        return _newsroom_latest()


@app.post("/api/newsroom/refresh")
def newsroom_refresh(request: Request, ausgabe: str = "de"):
    require_admin(request)                     # kostet ~0.2 $/Lauf -> nur Admin
    key = _ausgabe_key(ausgabe)
    if not _newsroom_lock.acquire(blocking=False):
        raise HTTPException(409, "A scan is already running.")
    try:
        import newsroom
        with _lang(key):
            out = _newsroom_dir() / f"{_dt.date.today().isoformat()}.json"
        res = newsroom.build(out, ausgabe=key)
        return {"ok": True, "topics": len(res["topics"]), "scanned": res["scanned"]}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Newsroom scan failed: {str(e)[:200]}")
    finally:
        _newsroom_lock.release()


# ------------------------------ Seiten ------------------------------
def _page(name: str):
    return FileResponse(STATIC / name, media_type="text/html",
                        headers={"Cache-Control": "no-cache"})


@app.get("/")
def index(request: Request):
    # Eingeloggt -> Studio; sonst oeffentliche Landingpage (Funnel)
    return _page("app.html" if current_uid(request) else "landing.html")


@app.get("/en")
def index_en(request: Request):
    return _page("app.html" if current_uid(request) else "landing_en.html")


@app.get("/fr")
def index_fr(request: Request):
    return _page("app.html" if current_uid(request) else "landing_fr.html")


@app.get("/login")
def login_page(request: Request):
    return _page("app.html" if current_uid(request) else "login.html")


@app.get("/consent")
def consent_page():
    return _page("consent.html")


@app.get("/impressum")
def impressum_page():
    return _page("impressum.html")


@app.get("/datenschutz")
def datenschutz_page():
    return _page("datenschutz.html")


@app.get("/nutzungsbedingungen")
def nutzungsbedingungen_page():
    return _page("nutzungsbedingungen.html")


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
