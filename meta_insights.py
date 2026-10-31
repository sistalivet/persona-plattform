#!/usr/bin/env python3
"""
Meta (Facebook) Insights fuer die Ziele-Auswertung. Rein stdlib.

Konfiguration: data/meta_pages.json (NIE ins Repo, NIE in den Chat):
  {"pages": [{"name": "Hauptseite", "page_id": "1234567890", "token": "EAAB..."}]}
  Token = langlebiges Page-Access-Token (Berechtigungen: pages_show_list,
  pages_read_engagement, read_insights).

Meta hat 2025/2026 viele Metriken gestrichen (impressions, fans, unique video views).
Deshalb PROBIERT dieses Modul beim ersten Lauf je Seite aus, welche Kandidaten die
API akzeptiert, und merkt sich die gueltige Menge in der Konfiguration ("ok_*").

  python meta_insights.py probe      # Metriken je Seite pruefen
  python meta_insights.py pull       # letzte 28 Tage Seiten- und Beitragswerte holen
  python meta_insights.py status     # was liegt vor
Daten landen in data/metrics.db (Tabellen meta_daily, meta_posts, meta_runs).
"""
import datetime as dt
import json
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HIER = Path(__file__).resolve().parent
DATA = HIER / "data"
CONFIG = DATA / "meta_pages.json"
DB = DATA / "metrics.db"
API = "https://graph.facebook.com/v26.0"

# Kandidaten (Stand 09/2026). Ungueltige werden beim Probe-Lauf verworfen.
PAGE_CANDIDATES = ["page_total_media_view_unique", "page_media_view", "page_total_media_view",
                   "page_post_engagements", "page_follows", "page_daily_follows",
                   "page_daily_unfollows", "page_video_views", "page_views_total"]
POST_CANDIDATES = ["post_media_view", "post_reactions_by_type_total", "post_video_views",
                   "post_clicks", "post_impressions_unique"]
REEL_CANDIDATES = ["blue_reels_play_count", "fb_reels_total_plays", "post_video_avg_time_watched",
                   "post_video_view_time", "post_video_social_actions", "fb_reels_replay_count",
                   "post_impressions_unique"]
POSITIVE = ("like", "love", "wow", "haha", "care")


def _db():
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=10)
    con.execute("CREATE TABLE IF NOT EXISTS meta_daily(day TEXT, page TEXT, metric TEXT, value REAL, "
                "PRIMARY KEY(day, page, metric))")
    con.execute("CREATE TABLE IF NOT EXISTS meta_posts(id TEXT, page TEXT, created TEXT, kind TEXT, "
                "message TEXT, permalink TEXT, metric TEXT, value REAL, fetched TEXT, "
                "PRIMARY KEY(id, metric))")
    con.execute("CREATE TABLE IF NOT EXISTS meta_runs(at TEXT, ok INTEGER, note TEXT)")
    return con


def _cfg() -> dict:
    if not CONFIG.is_file():
        return {"pages": []}
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except ValueError:
        return {"pages": []}


def _save_cfg(cfg: dict):
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _get(path: str, token: str, **params):
    params["access_token"] = token
    url = f"{API}/{path}?{urllib.parse.urlencode(params, doseq=True)}"
    req = urllib.request.Request(url, headers={"User-Agent": "persona-goals/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8")).get("error", {})
        except ValueError:
            err = {"message": str(e)}
        return None, err.get("message", str(e))
    except (urllib.error.URLError, TimeoutError) as e:
        return None, str(e)


def _redact(msg: str) -> str:
    """Fehlertexte nie mit Token ausgeben."""
    return msg.replace("access_token", "token")[:200] if msg else ""


def probe(page: dict) -> dict:
    """Findet je Kandidatenliste die Metriken, die die API fuer diese Seite akzeptiert."""
    tok, pid = page["token"], page["page_id"]
    yest = dt.date.today() - dt.timedelta(days=1)
    since, until = (yest - dt.timedelta(days=2)).isoformat(), yest.isoformat()
    ok_page = []
    for m in PAGE_CANDIDATES:
        d, err = _get(f"{pid}/insights", tok, metric=m, period="day", since=since, until=until)
        if d is not None:
            ok_page.append(m)
    ok_post, ok_reel = [], []
    posts, _ = _get(f"{pid}/published_posts", tok, fields="id", limit=3)
    for p in (posts or {}).get("data", [])[:1]:
        for m in POST_CANDIDATES:
            d, err = _get(f"{p['id']}/insights", tok, metric=m)
            if d is not None:
                ok_post.append(m)
    reels, _ = _get(f"{pid}/video_reels", tok, fields="id", limit=3)
    for r in (reels or {}).get("data", [])[:1]:
        for m in REEL_CANDIDATES:
            d, err = _get(f"{r['id']}/video_insights", tok, metric=m)
            if d is not None:
                ok_reel.append(m)
    page.update(ok_page=ok_page, ok_post=ok_post, ok_reel=ok_reel,
                probed=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    return page


def _store_daily(con, page_name: str, data: dict):
    n = 0
    for m in data.get("data", []):
        for v in m.get("values", []):
            val = v.get("value")
            day = str(v.get("end_time", ""))[:10]
            if isinstance(val, dict):   # z. B. reactions by type
                for k, x in val.items():
                    con.execute("INSERT OR REPLACE INTO meta_daily VALUES (?,?,?,?)",
                                (day, page_name, f"{m['name']}.{k}", float(x or 0)))
                    n += 1
            elif val is not None and day:
                con.execute("INSERT OR REPLACE INTO meta_daily VALUES (?,?,?,?)",
                            (day, page_name, m["name"], float(val)))
                n += 1
    return n


def _store_post(con, page_name: str, post: dict, kind: str, ins: dict, fetched: str):
    n = 0
    for m in (ins or {}).get("data", []):
        vals = m.get("values") or [{}]
        val = vals[-1].get("value") if vals else None
        if isinstance(val, dict):
            for k, x in val.items():
                con.execute("INSERT OR REPLACE INTO meta_posts VALUES (?,?,?,?,?,?,?,?,?)",
                            (post["id"], page_name, post.get("created_time", "")[:19], kind,
                             (post.get("message") or post.get("description") or "")[:200],
                             post.get("permalink_url", ""), f"{m['name']}.{k}", float(x or 0), fetched))
                n += 1
        elif val is not None:
            con.execute("INSERT OR REPLACE INTO meta_posts VALUES (?,?,?,?,?,?,?,?,?)",
                        (post["id"], page_name, post.get("created_time", "")[:19], kind,
                         (post.get("message") or post.get("description") or "")[:200],
                         post.get("permalink_url", ""), m["name"], float(val), fetched))
            n += 1
    return n


def pull(days: int = 28) -> dict:
    cfg = _cfg()
    if not cfg.get("pages"):
        return {"ok": False, "note": "Keine Konfiguration (data/meta_pages.json fehlt oder leer)."}
    con = _db()
    fetched = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    until = dt.date.today().isoformat()
    notes, total_ok = [], True
    for page in cfg["pages"]:
        if not page.get("probed"):
            probe(page)
        name, tok, pid = page["name"], page["token"], page["page_id"]
        n = 0
        if page.get("ok_page"):
            d, err = _get(f"{pid}/insights", tok, metric=",".join(page["ok_page"]),
                          period="day", since=since, until=until)
            if d is not None:
                n += _store_daily(con, name, d)
            else:
                total_ok = False
                notes.append(f"{name}: Seitenwerte: {_redact(err)}")
        posts, err = _get(f"{pid}/published_posts", tok, since=since, limit=100,
                          fields="id,created_time,message,permalink_url")
        if posts is None:
            total_ok = False
            notes.append(f"{name}: Beitraege: {_redact(err)}")
        for p in (posts or {}).get("data", []):
            if page.get("ok_post"):
                ins, _ = _get(f"{p['id']}/insights", tok, metric=",".join(page["ok_post"]))
                n += _store_post(con, name, p, "post", ins, fetched)
        reels, err = _get(f"{pid}/video_reels", tok, since=since, limit=100,
                          fields="id,created_time,description,permalink_url")
        for r in (reels or {}).get("data", []):
            if page.get("ok_reel"):
                ins, _ = _get(f"{r['id']}/video_insights", tok, metric=",".join(page["ok_reel"]))
                n += _store_post(con, name, r, "reel", ins, fetched)
        notes.append(f"{name}: {n} Werte")
    con.execute("INSERT INTO meta_runs VALUES (?,?,?)", (fetched, int(total_ok), " | ".join(notes)[:500]))
    con.commit()
    con.close()
    _save_cfg(cfg)   # gemerkte ok_*-Listen
    return {"ok": total_ok, "note": " | ".join(notes)}


def status() -> dict:
    cfg = _cfg()
    out = {"pages": [{"name": p.get("name"), "ok_page": p.get("ok_page"), "ok_post": p.get("ok_post"),
                      "ok_reel": p.get("ok_reel"), "probed": p.get("probed")} for p in cfg.get("pages", [])],
           "last_run": None}
    if DB.is_file():
        con = _db()
        row = con.execute("SELECT at, ok, note FROM meta_runs ORDER BY at DESC LIMIT 1").fetchone()
        con.close()
        if row:
            out["last_run"] = {"at": row[0], "ok": bool(row[1]), "note": row[2]}
    return out


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "probe":
        cfg = _cfg()
        for p in cfg.get("pages", []):
            probe(p)
            print(p["name"], "| Seite:", p["ok_page"], "| Post:", p["ok_post"], "| Reel:", p["ok_reel"])
        _save_cfg(cfg)
    elif cmd == "pull":
        print(json.dumps(pull(), ensure_ascii=False))
    else:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
