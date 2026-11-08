"""Website persona-ainews.ch: alle Seiten unter /news (Ausgaben, Artikel, Ressorts, Dossiers, Suche, Register,
Korrekturen, Podcast, Newsletter, Verteiler der weiteren Ausgaben) sowie die statischen Seiten der Plattform.

Aus platform_app.py herausgelöst am 31. Oktober 2026 (Bestandsaufnahme Punkt 8), Code unverändert.
"""

import datetime as _dt
import tempfile
import html as _htmlmod
import json
import re
import sqlite3
import threading
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as _StarletteHTTPException
from .kern import (
    BLOG_DIR, DATA, SITE_FILE, STATIC, _LANG, _SITE_DEFAULT, _SUCHE, _ausgabe, _ausgabe_home_abs,
    _ausgabe_key, _ausgaben, _ausgaben_sichtbar, _blog_entries, _blog_entry, _eintrag_ausgabe, _lang, _nfc,
    _site, _slug, _sprache, _stamm_domain, _t, app, current_uid, require_user,
)

@app.get("/news/media/{name}")
def news_media(name: str):
    if any(c in name for c in ("..", "/", "\\")) or not name.endswith((".mp4", ".jpg", ".png", ".mp3")):
        raise HTTPException(400, "Invalid.")
    p = BLOG_DIR / name
    if not p.is_file():
        raise HTTPException(404, "Nicht gefunden.")
    mt = {"mp4": "video/mp4", "jpg": "image/jpeg", "png": "image/png", "mp3": "audio/mpeg"}[name.rsplit(".", 1)[1]]
    return FileResponse(p, media_type=mt, headers={"X-Robots-Tag": "noindex, nofollow"})

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
<h2>Die Marke „Aktuell“</h2>
<p>Nicht jede wichtige Nachricht ist konstruktiv. Beiträge, die wir nach Aktualität und Relevanz auswählen, tragen statt des Scores
die Marke <b>Aktuell</b>. Das gilt für die Nachmittagsausgabe und für jeden Beitrag, dessen Score unter 60 liegt. Der Score wird
trotzdem berechnet und bleibt im Beitrag gespeichert; er sagt nichts über die Richtigkeit der Nachricht.</p>
<h2>Was der Score nicht kann</h2>
<p>Er prüft keine Fakten und erkennt keine Falschmeldungen. Er bewertet nur den Text, nicht Bilder oder Videos. Bei fremden Artikeln
lesen wir nur den öffentlich zugänglichen Text; Bezahlartikel können wir nicht einschätzen. Jede Einschätzung im Medien-Check wird
von einem Menschen der Redaktion geprüft, bevor sie erscheint. Wir wenden dieselben Kriterien und dieselbe Skala auf unsere eigenen
Beiträge an. Wer eine Einschätzung für falsch hält, erreicht uns über das <a href="{P}/impressum">Impressum</a>.</p></div>"""
    return _news_shell(request, f"So bewerten wir · {site['name']}", inner, active="medien",
                       desc="Die Kriterien hinter dem Konstruktiv-Score von Persona AI News.")

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
.lead.meldung{padding:14px 18px}
.lead.meldung h2{font-size:20px;margin:0 0 6px}
.lead.meldung .bd p.text{font-size:16px;line-height:1.5;margin:0 0 6px}
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
.lead .bd p.text{font-size:17px;line-height:1.55;color:var(--ink);margin:0 0 12px}
.grenzen{margin:14px 0 6px;padding:12px 16px;border-left:4px solid var(--accent-dark);border-radius:0 12px 12px 0;background:var(--bg)}
.grenzen .glabel{display:block;font-size:11.5px;letter-spacing:.08em;text-transform:uppercase;font-weight:700;color:var(--muted);margin-bottom:4px}
.grenzen p{margin:0;font-size:15.5px;line-height:1.5;color:var(--ink)}
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
  /* Kurzformat (3.11./4.11.2026): der ganze Text steht auf der Startseite. Das Video steht links und ist
     freigestellt (float), der Text beginnt rechts daneben und laeuft darunter ueber die volle Breite weiter;
     Kasten und Reaktionen beginnen unterhalb des Videos. Querformat-Videos stehen wie bisher oben ueber dem Text. */
  .lead.kurz{display:block}
  .lead.kurz::after{content:"";display:block;clear:both}
  .lead.kurz .hd{margin-bottom:10px}
  .lead.kurz .vid{float:left;width:42%;margin:2px 14px 6px 0;text-align:left}
  .lead.kurz .vid video{width:100%;max-width:none;max-height:none;margin:0}
  .lead.kurz.wide .vid{float:none;width:100%;margin:0 0 10px}
  .lead.kurz .bd p.text{font-size:16px;line-height:1.5;margin:0 0 10px}
  .lead.kurz .grenzen{clear:both}
  .lead.kurz .react{clear:both}
  .lead.kurz .bd a.more{display:inline-block;margin-top:4px}
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

AKTUELL_SCHWELLE = 60   # unter diesem Konstruktiv-Score traegt ein Beitrag die Marke "Aktuell" statt des Scores (2.11.2026)

def _ist_aktuell(e: dict) -> bool:
    """Spur Aktuell (26.10.2026): Beitraege der Nachmittagsausgabe nach Aktualitaet; der Konstruktiv-Score
    wird berechnet und gespeichert, aber nicht gezeigt, die Karte traegt stattdessen die Marke "Aktuell".
    Seit 2.11.2026 gilt das auch fuer jeden Beitrag mit einem Score unter AKTUELL_SCHWELLE: Die Nachricht
    ist nach Aktualitaet gewaehlt, ein niedriger Score sagt nichts ueber ihre Richtigkeit."""
    if e.get("spur") == "aktuell":
        return True
    k = e.get("konstruktiv")
    return isinstance(k, dict) and "score" in k and int(k["score"]) < AKTUELL_SCHWELLE

def _aktuell_badge(e: dict) -> str:
    if not _ist_aktuell(e) or not _site().get("show_scores", True):   # show_scores aus = keine Bewertung, auch keine Marke
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

def _absaetze_of(e: dict) -> list[str]:
    return [p.strip() for p in str(e.get("text") or e.get("skript") or "").split("\n") if p.strip()]

def _ist_kurz(e: dict) -> bool:
    """Kurzformat (3.11.2026): zwei Absaetze, der zweite nennt die Grenzen; seit dem Dreitakt (8.11.2026) ein
    einziger Absatz je Meldung. Aeltere Beitraege mit vier Absaetzen (Archiv, Wochenrueckblick) werden wie bisher gezeigt."""
    return len(_absaetze_of(e)) in (1, 2)


def _ist_meldung(e: dict) -> bool:
    """Eine Meldung des Dreitakts: ein Absatz, kein Video. Sie steht kompakt in der Tagesausgabe."""
    return len(_absaetze_of(e)) == 1 and not e.get("video")

def _text_html(e: dict, klasse: str = "") -> str:
    """Der Text eines Beitrags: im Kurzformat der Anlass als Absatz und die Grenzen als Kasten
    (Entscheidung Max, 3.11.2026), sonst alle Absaetze nacheinander."""
    esc = _htmlmod.escape
    paras = _absaetze_of(e)
    cls = f' class="{klasse}"' if klasse else ""
    if _ist_kurz(e) and len(paras) == 1:
        return f"<p{cls}>{esc(paras[0])}</p>"
    if _ist_kurz(e):
        return (f"<p{cls}>{esc(paras[0])}</p>"
                f'<div class="grenzen"><span class="glabel">{esc(_t("grenzen_label"))}</span><p>{esc(paras[1])}</p></div>')
    return "".join(f"<p{cls}>{esc(p)}</p>" for p in paras)

def _lead_html(e: dict, counts: dict, P: str) -> str:
    esc = _htmlmod.escape
    pid = esc(e["id"])
    body = str(e.get("text") or e.get("skript") or "").strip()
    teaser = _teaser_of(e)
    if _ist_kurz(e):
        # Kurzformat: der ganze Text steht schon auf der Startseite; der Link fuehrt zu Quellen, Karten und Register
        inhalt = _text_html(e, "text") + f'<a class="more" href="{P}/{pid}">{_t("read_more")}</a>'
    else:
        more = f'<a class="more" href="{P}/{pid}">{_t("read_more")}</a>' if (len(body) > len(teaser) + 20 or e.get("quellen")) else ""
        inhalt = f'<p class="teaser">{esc(teaser)}</p>{more}'
    kurz = (" kurz" if _ist_kurz(e) else "") + (" meldung" if _ist_meldung(e) else "")
    return (f'<div class="lead {_layout_cls(e)}{kurz}" data-post="{pid}">'
            f'<div class="hd"><h2><a href="{P}/{pid}">{esc(e["titel"])}</a></h2>{_meta_html(e, P)}</div>'
            f'{_video_html(e, P, preload="none")}'
            f'<div class="bd">{inhalt}</div>{_react_html(e, counts)}</div>')

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
    content = _text_html(e)
    paras = _absaetze_of(e)
    teaser = _teaser_of(e)
    # der Teaser steht nur ueber dem Text, wenn er ihn nicht wiederholt (im Kurzformat ist er der Anfang des Anlasses)
    if _nfc(e.get("teaser", "")) and not (paras and paras[0].startswith(teaser[:40])):
        content = f'<p class="teaser">{esc(teaser)}</p>' + content
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
    if not _site().get("show_scores", True):
        pass   # Bewertung ausgeblendet (3.11.2026): weder Score noch Marke in den Daten
    elif _ist_aktuell(e):
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
    # Dreitakt (8.11.2026, Max): die Beitraege mit Video-Avatar stehen oben (neuester zuerst), danach die Meldungen
    heute = [e for e in heute if e.get("video")] + [e for e in heute if not e.get("video")]
    inner = (f'<div class="today"><h2>{_t("today_ed")}</h2>'
             f'<span>{esc(_t("ed_of").format(d=_datum_lang(tag, lang)))} · '
             f'{esc((_t("stories_one") if len(heute) == 1 else _t("stories_n")).format(n=len(heute)))}</span></div>')
    # Kurzformat (3.11.2026, Entscheidung Max): jeder Beitrag des Tages steht mit seinem ganzen Text auf der
    # Startseite; aeltere Beitraege mit vier Absaetzen bleiben Karten mit Teaser
    inner += _lead_html(heute[0], counts, P)
    rest = heute[1:25]   # Dreitakt: bis zu 15 Beitraege am Tag, alle mit Text
    inner += "".join(_lead_html(e, counts, P) for e in rest if _ist_kurz(e))
    karten = [e for e in rest if not _ist_kurz(e)]
    if karten:
        inner += '<div class="grid">' + "".join(_card_html(e, P) for e in karten) + "</div>"
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
    if _sprache() == "de":
        # Transparenz zum Umbau vom 3. November 2026 (Kurzformat und Archiv, Entscheidung Max)
        inner += ('<div class="korr"><b>3. November 2026</b> · Umstellung auf das Kurzformat: Seither haben unsere Beiträge zwei Absätze '
                  'und nennen im ersten Satz, wer die Nachricht gemeldet hat. Die bis dahin erschienenen Beiträge sind archiviert und nicht '
                  'mehr öffentlich; je Ressort blieben die drei jüngsten stehen und wurden auf das Kurzformat gekürzt. Überschriften, Videos '
                  'und Kennzeichen im Herkunftsregister sind unverändert.</div>')
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
    if e.get("archiviert") and not current_uid(request):
        # Archiv (3.11.2026): der Beitrag bleibt gespeichert, ist aber nicht mehr oeffentlich; 410 sagt
        # Suchmaschinen, dass die Adresse bewusst weg ist. Angemeldete Redaktion sieht ihn weiter.
        raise HTTPException(410, _t("err_archiv"))
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
<h2>The "Latest" label</h2>
<p>Not every important story is constructive. Stories we pick for timeliness and relevance carry the label <b>Latest</b> instead of
a score. This applies to the afternoon edition and to every story whose score is below 60. The score is still computed and stays
stored with the story; it says nothing about whether the story is accurate.</p>
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


# Späte Bindung: diese Namen werden nur in Funktionskörpern gebraucht. Die Module dürfen sich
# gegenseitig nutzen; der Import steht am Ende, damit beim Laden keine Kreisabhängigkeit entsteht.
# (Reihenfolge des Ladens: persona/__init__.py)
from .konten import _EMAIL_RE, _LEAD_MAX, _LEAD_WINDOW, _client_ip, _require_editor  # noqa: E402
from .redaktion import (
    PROFILES_DIR, _profile, _profile_for_avatar, _profiles, _reaction_counts, _reviews, _social_html,
    _ticker_html, _ticker_items,
)  # noqa: E402
from .render import _lock  # noqa: E402
