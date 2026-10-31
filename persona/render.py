"""Render: Stimmen, Hintergründe, Stile, Skript, Videoerzeugung (twin_pipeline), Clips, Framing, Branding, Avatar-Upload.

Aus platform_app.py herausgelöst am 31. Oktober 2026 (Bestandsaufnahme Punkt 8), Code unverändert.
"""

import datetime as _dt
import hashlib
import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import unicodedata
import uuid
from pathlib import Path
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel
import auth
from .kern import (
    DATA, FORMATS, REFRAME_SCRIPT, RESSORTS, STEPS, TWIN_SCRIPT, VIDEO_EXT, _AVATAR_TWIN, _ausgaben, _lang,
    _site, app, require_user, user_avatars, user_dir,
)

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


# Späte Bindung: diese Namen werden nur in Funktionskörpern gebraucht. Die Module dürfen sich
# gegenseitig nutzen; der Import steht am Ende, damit beim Laden keine Kreisabhängigkeit entsteht.
# (Reihenfolge des Ladens: persona/__init__.py)
from .konten import require_admin  # noqa: E402
from .redaktion import PROFILES_DIR, _profile  # noqa: E402
