"""Redaktions-API: Kennzahlen und Ziele, Blog-API der Kette, Profile, Ticker, Konstruktiv-Score, Medien-Check,
Social-Posts, Statistik-Mails, Ausgaben-Register und Newsroom.

Aus platform_app.py herausgelöst am 31. Oktober 2026 (Bestandsaufnahme Punkt 8), Code unverändert.
"""

import datetime as _dt
import hashlib
import html as _htmlmod
import json
import re
import shutil
import sqlite3
import subprocess
import threading
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel
import auth
from .kern import (
    AUSGABEN_FILE, BLOG_DIR, DATA, VIDEO_EXT, _AUSGABEN_FEST, _AUSGABE_FELDER, _AUSGABE_RE, _AVATAR_TWIN,
    _LANG, _SITE_DEFAULT, _SITE_EN_DEFAULT, _ausgabe, _ausgabe_home_abs, _ausgabe_key, _ausgaben,
    _blog_entries, _blog_entry, _eintrag_ausgabe, _extract_audio, _lang, _nfc, _save_entry, _site, _slug,
    _stamm_domain, _t, app, require_user, user_avatars, user_dir,
)

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


# Späte Bindung: diese Namen werden nur in Funktionskörpern gebraucht. Die Module dürfen sich
# gegenseitig nutzen; der Import steht am Ende, damit beim Laden keine Kreisabhängigkeit entsteht.
# (Reihenfolge des Ladens: persona/__init__.py)
from .konten import _client_ip, _is_editor, _require_editor, require_admin  # noqa: E402
from .render import _CLIP_SRC_CAP, _load_json, _save_title, _titles_file, _video_seconds  # noqa: E402
from .website import _public_url, indexnow_submit  # noqa: E402
