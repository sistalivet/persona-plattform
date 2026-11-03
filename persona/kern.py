"""Grundlagen: App-Objekt, Pfade, Konfiguration, Nutzerprüfung, Ausgaben, Beitragsspeicher (Blog) und Suchindex.
Alle anderen Module bauen darauf auf; dieses Modul importiert keines von ihnen.

Aus platform_app.py herausgelöst am 31. Oktober 2026 (Bestandsaufnahme Punkt 8), Code unverändert.
"""

import contextvars
import functools
import json
import os
import re
import subprocess
import sys
import threading
import unicodedata
import webbrowser
from pathlib import Path
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
import auth
import suche as _suche_mod

HIER = Path(__file__).resolve().parent.parent   # der Ordner der Plattform, nicht persona/

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
            if not rec.get("archiviert"):   # archivierte Beitraege (3.11.2026) stehen nicht im Suchindex
                zeilen.append((stem, mt, dict(rec, _ausgabe=_eintrag_ausgabe(rec))))
    out.sort(key=lambda r: str(r.get("published_at", "")), reverse=True)
    with _BLOG_LOCK:
        _BLOG_CACHE["sig"], _BLOG_CACHE["alle"] = schluessel, out
    try:
        _SUCHE.aktualisieren(zeilen)
    except Exception as e:  # noqa: BLE001  (der Index darf die Seite nie aufhalten)
        print(f"[suche] Index nicht aktualisiert: {type(e).__name__}: {e}", file=sys.stderr)
    return out

def _blog_entries(lang: str | None = "", archiv: bool = False) -> list:
    """Beitraege der Ausgabe: lang "" = aktuelle Ausgabe (_LANG), None = alle, sonst der Schluessel
    einer Ausgabe (de, en, ...). Beitraege ohne `ausgabe` und ohne `sprache` gehoeren zur deutschen.
    Flache Kopien der Eintraege im Speicher (_blog_alle).
    Archiv (Entscheidung Max, 3.11.2026): Beitraege mit dem Feld `archiviert` (Zeitstempel) bleiben
    gespeichert, erscheinen aber nirgends auf der Website (Listen, Feeds, Sitemap, Suche, Dossiers,
    Verwandte); nur die Redaktion sieht sie (archiv=True, API /api/blog/posts). Archiviert wird mit archiv.py."""
    if lang == "":
        lang = _LANG.get()
    return [dict(r) for r in _blog_alle()
            if (not lang or _eintrag_ausgabe(r) == lang) and (archiv or not r.get("archiviert"))]

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

def _save_entry(pid: str, rec: dict):
    for k in ("id", "video", "poster"):
        rec.pop(k, None)
    BLOG_DIR.mkdir(parents=True, exist_ok=True)
    (BLOG_DIR / (pid + ".json")).write_text(json.dumps(rec, ensure_ascii=False, indent=2),
                                            encoding="utf-8")

def _nfc(s) -> str:
    return unicodedata.normalize("NFC", str(s or "")).strip()

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
        "aktuell_word": "Aktuell", "aktuell_title": "Ausgewählt nach Aktualität und Relevanz, nicht nach konstruktivem Gehalt; dafür zeigen wir keinen Konstruktiv-Score.",
        "err_archiv": "Dieser Beitrag ist archiviert und nicht mehr öffentlich.",
        "grenzen_label": "Was offen ist",
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
        "aktuell_word": "Latest", "aktuell_title": "Picked for timeliness and relevance rather than constructive content; no constructive score is shown for it.",
        "err_archiv": "This story has been archived and is no longer public.",
        "grenzen_label": "What is open",
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
