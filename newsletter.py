"""Newsletter von Persona AI News (Entscheidung Max, 19.10.2026): Anmeldung mit Bestaetigung
(Double Opt-in), Abmeldung per Link, taeglicher Versand der Beitraege des Tages je Ausgabe (de/en).

Speicher: data/newsletter.sqlite (E-Mail, Sprache, Token, Zeitpunkte). Versand ueber das Postfach
in data/mail.json (SMTP, wie mail_stats.py). Keine Weitergabe, keine Tracking-Pixel, keine Klick-
Verfolgung: Links zeigen direkt auf die Seite.

  python newsletter.py --senden            Tagesbrief fuer heute, alle Ausgaben (Timer 18:30 Europe/Berlin)
  python newsletter.py --senden --tag 2026-10-19 --sprache de     (--sprache = Schluessel der Ausgabe)
  python newsletter.py --vorschau de       HTML des Tagesbriefs auf stdout, nichts senden
  python newsletter.py --status            Abonnenten je Ausgabe
"""
import datetime as dt
import html
import json
import secrets
import smtplib
import sqlite3
import sys
from email.message import EmailMessage
from pathlib import Path

HIER = Path(__file__).resolve().parent
DATA = HIER / "data"
DB = DATA / "newsletter.sqlite"
BLOG = DATA / "blog"
MAIL_CFG = DATA / "mail.json"
SEITE = "https://persona-ainews.ch"

TEXTE = {
    "de": {
        "name": "Persona AI News",
        "betreff_bestaetigen": "Bitte bestätigen: Newsletter von Persona AI News",
        "bestaetigen": ("Hallo,\n\njemand hat diese Adresse für den täglichen Newsletter von Persona AI News eingetragen. "
                        "Wenn du das warst, bestätige die Anmeldung mit diesem Link:\n\n{link}\n\n"
                        "Wenn nicht, ignoriere diese Nachricht; es passiert nichts weiter.\n\nPersona AI News\n{seite}"),
        "betreff_tag": "Persona AI News, {datum}: {titel}",
        "intro": "Die Beiträge von heute, kurz und konstruktiv, gesprochen von unseren Avataren.",
        "lesen": "Zum Beitrag",
        "video": "Video ansehen",
        "fuss": "Du bekommst diese Mail, weil du den Newsletter von Persona AI News bestätigt hast.",
        "abmelden": "Abmelden",
        "datenschutz": "Datenschutz",
        "ki": "Alle Beiträge sind KI-gestützt erstellt und redaktionell geprüft; die Sprecher sind fiktive KI-Avatare.",
        "datum": "%d.%m.%Y",
    },
    "en": {
        "name": "Persona AI News",
        "betreff_bestaetigen": "Please confirm: Persona AI News newsletter",
        "bestaetigen": ("Hello,\n\nsomeone entered this address for the daily Persona AI News newsletter (English edition). "
                        "If that was you, confirm with this link:\n\n{link}\n\n"
                        "If not, ignore this message; nothing else happens.\n\nPersona AI News\n{seite}"),
        "betreff_tag": "Persona AI News, {datum}: {titel}",
        "intro": "Today's stories, short and constructive, presented by our AI anchors.",
        "lesen": "Read the story",
        "video": "Watch the video",
        "fuss": "You receive this mail because you confirmed the Persona AI News newsletter.",
        "abmelden": "Unsubscribe",
        "datenschutz": "Privacy",
        "ki": "All stories are produced with AI and checked by an editor; the anchors are fictional AI avatars.",
        "datum": "%d %B %Y",
    },
}


def connect() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=10)
    con.execute("CREATE TABLE IF NOT EXISTS abonnenten (email TEXT PRIMARY KEY, sprache TEXT, token TEXT, "
                "angelegt_at TEXT, bestaetigt_at TEXT, abgemeldet_at TEXT, ip TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS versand (tag TEXT, sprache TEXT, anzahl INTEGER, beitraege INTEGER, "
                "at TEXT, PRIMARY KEY (tag, sprache))")
    con.commit()
    return con


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ---- Ausgaben (Mandanten, 19.10.2026): die Spalte `sprache` traegt den Schluessel der Ausgabe
# (de, en, weitere aus data/ausgaben.json); Texte und Datum richten sich nach der Sprache der Ausgabe.
_FEST = {"de": {"sprache": "de", "prefix": "", "domain": "persona-ainews.ch", "site": "site.json"},
         "en": {"sprache": "en", "prefix": "/en", "domain": "persona-ainews.ch", "site": "site_en.json"}}


def ausgaben() -> dict:
    out = {k: dict(v) for k, v in _FEST.items()}
    try:
        d = json.loads((DATA / "ausgaben.json").read_text(encoding="utf-8"))
    except (ValueError, OSError):
        d = {}
    for k, v in (d.items() if isinstance(d, dict) else []):
        if isinstance(v, dict) and k.isalnum():
            rec = out.get(k) or {"sprache": "de", "prefix": "/" + k, "domain": _FEST["de"]["domain"], "site": f"site_{k}.json"}
            rec.update({kk: vv for kk, vv in v.items() if kk in ("sprache", "prefix", "domain", "site")})
            out[k] = rec
    return out


def ausgabe_key(s: str) -> str:
    return s if s in ausgaben() else "de"


def sprache_von(key: str) -> str:
    return "en" if ausgaben().get(key, {}).get("sprache") == "en" else "de"


def name_von(key: str) -> str:
    try:
        return json.loads((DATA / ausgaben()[key]["site"]).read_text(encoding="utf-8")).get("name") or "Persona AI News"
    except (ValueError, OSError, KeyError):
        return "Persona AI News"


def basis(key: str) -> str:
    """Startadresse der Ausgabe ohne Schraegstrich am Ende: eigene Domain an der Wurzel, sonst Stammdomain plus Pfad."""
    a = ausgaben().get(key) or _FEST["de"]
    stamm = _FEST["de"]["domain"]
    dom = (a.get("domain") or stamm).lower()
    return f"https://{dom}" if dom != stamm else f"https://{stamm}{a.get('prefix') or ''}"


def _eintrag_ausgabe(d: dict) -> str:
    return str(d.get("ausgabe") or ("en" if (d.get("sprache") or "de") == "en" else "de"))


# ---------------------------------------------------------------- Anmeldung

def anmelden(email: str, sprache: str, ip: str = "") -> str:
    """Eintragen (oder Token eines offenen Eintrags erneuern) und Bestaetigungsmail senden.
    Liefert den Token. Eine bestaetigte Adresse bekommt keine neue Mail. `sprache` = Schluessel der Ausgabe."""
    sprache = ausgabe_key(sprache)
    con = connect()
    row = con.execute("SELECT token, bestaetigt_at, abgemeldet_at FROM abonnenten WHERE email = ?", (email,)).fetchone()
    token = secrets.token_urlsafe(24)
    if row and row[1] and not row[2]:
        con.close()
        return row[0]        # schon dabei, still bleiben
    con.execute("INSERT INTO abonnenten (email, sprache, token, angelegt_at, bestaetigt_at, abgemeldet_at, ip) "
                "VALUES (?, ?, ?, ?, NULL, NULL, ?) ON CONFLICT(email) DO UPDATE SET sprache = excluded.sprache, "
                "token = excluded.token, angelegt_at = excluded.angelegt_at, bestaetigt_at = NULL, abgemeldet_at = NULL, ip = excluded.ip",
                (email, sprache, token, _now(), ip[:64]))
    con.commit()
    con.close()
    t = TEXTE[sprache_von(sprache)]
    link = f"{basis(sprache)}/newsletter/bestaetigen/{token}"
    senden_mail(email, t["betreff_bestaetigen"], t["bestaetigen"].format(link=link, seite=basis(sprache)), None)
    return token


def bestaetigen(token: str) -> str | None:
    con = connect()
    row = con.execute("SELECT email, sprache FROM abonnenten WHERE token = ?", (token,)).fetchone()
    if not row:
        con.close()
        return None
    con.execute("UPDATE abonnenten SET bestaetigt_at = COALESCE(bestaetigt_at, ?), abgemeldet_at = NULL WHERE token = ?",
                (_now(), token))
    con.commit()
    con.close()
    return row[1]


def abmelden(token: str) -> str | None:
    con = connect()
    row = con.execute("SELECT email, sprache FROM abonnenten WHERE token = ?", (token,)).fetchone()
    if not row:
        con.close()
        return None
    con.execute("UPDATE abonnenten SET abgemeldet_at = ? WHERE token = ?", (_now(), token))
    con.commit()
    con.close()
    return row[1]


def empfaenger(sprache: str) -> list[tuple[str, str]]:
    con = connect()
    rows = con.execute("SELECT email, token FROM abonnenten WHERE sprache = ? AND bestaetigt_at IS NOT NULL "
                       "AND abgemeldet_at IS NULL", (sprache,)).fetchall()
    con.close()
    return [(r[0], r[1]) for r in rows]


def status() -> dict:
    con = connect()
    out = {}
    for sp in ausgaben():
        n = con.execute("SELECT COUNT(*) FROM abonnenten WHERE sprache = ? AND bestaetigt_at IS NOT NULL AND abgemeldet_at IS NULL", (sp,)).fetchone()[0]
        o = con.execute("SELECT COUNT(*) FROM abonnenten WHERE sprache = ? AND bestaetigt_at IS NULL", (sp,)).fetchone()[0]
        out[sp] = {"bestaetigt": n, "offen": o}
    con.close()
    return out


# ---------------------------------------------------------------- Mail

def mail_cfg() -> dict:
    return json.loads(MAIL_CFG.read_text(encoding="utf-8"))


def senden_mail(to: str, subject: str, text: str, html_body: str | None, list_unsub: str | None = None) -> None:
    cfg = mail_cfg()
    msg = EmailMessage()
    msg["From"] = f"Persona AI News <{cfg['user']}>"
    msg["To"] = to
    msg["Subject"] = subject
    if list_unsub:
        msg["List-Unsubscribe"] = f"<{list_unsub}>"
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    # Port 587 mit STARTTLS: Port 465 ist vom Hetzner-Server aus nicht erreichbar (gemessen 19.10.2026)
    host, port = cfg.get("smtp_host", cfg["host"]), int(cfg.get("smtp_port", 587))
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(cfg["user"], cfg["password"])
            s.send_message(msg)
        return
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.ehlo()
        s.starttls()
        s.ehlo()
        s.login(cfg["user"], cfg["password"])
        s.send_message(msg)


# ---------------------------------------------------------------- Tagesbrief

def beitraege(sprache: str, tag: str) -> list[dict]:
    out = []
    for j in BLOG.glob("*.json"):
        try:
            d = json.loads(j.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if _eintrag_ausgabe(d) != sprache or not d.get("titel"):
            continue
        if str(d.get("published_at", ""))[:10] != tag:
            continue
        d["id"] = j.stem
        d["video"] = (BLOG / (j.stem + ".mp4")).is_file()
        d["poster"] = (BLOG / (j.stem + ".jpg")).is_file()
        out.append(d)
    out.sort(key=lambda r: str(r.get("published_at", "")))
    return out


def _teaser(d: dict) -> str:
    t = (d.get("teaser") or "").strip()
    if t:
        return t
    first = str(d.get("text") or "").strip().split("\n")[0]
    return first if len(first) <= 220 else first[:217].rsplit(" ", 1)[0] + " …"


def tagesbrief(sprache: str, tag: str, unsub: str = "{unsub}") -> tuple[str, str, str, int]:
    """(Betreff, HTML, Text, Anzahl Beitraege). `unsub` ist der Platzhalter fuer den Abmeldelink.
    `sprache` ist der Schluessel der Ausgabe; die Texte folgen ihrer Sprache."""
    sp = sprache_von(sprache)
    t = dict(TEXTE[sp])
    t["name"] = name_von(sprache)
    t["betreff_tag"] = t["betreff_tag"].replace("Persona AI News", t["name"], 1)
    posts = beitraege(sprache, tag)
    esc = html.escape
    datum = dt.date.fromisoformat(tag).strftime(t["datum"])
    if sp == "de":
        monate = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"]
        d0 = dt.date.fromisoformat(tag)
        datum = f"{d0.day}. {monate[d0.month - 1]} {d0.year}"
    betreff = t["betreff_tag"].format(datum=datum, titel=(posts[0]["titel"] if posts else ""))[:150]
    B = basis(sprache)
    parts_html, parts_txt = [], []
    for d in posts:
        url = f"{B}/{d['id']}"
        score = (d.get("konstruktiv") or {}).get("score")
        badge = f'<span style="font-size:12px;color:#0b5b55;font-weight:600">{"Konstruktiv" if sp == "de" else "Constructive"} {score}</span>' if score is not None else ""
        poster = (f'<a href="{url}"><img src="{B}/media/{d["id"]}.jpg" alt="" width="240" '
                  f'style="display:block;border-radius:10px;max-width:100%"></a>' if d.get("poster") else "")
        parts_html.append(
            f'<div style="margin:0 0 28px;padding:0 0 24px;border-bottom:1px solid #e6e2d9">'
            f'<p style="margin:0 0 6px;font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#5d5a54">{esc(d.get("ressort") or "")}</p>'
            f'<h2 style="margin:0 0 10px;font-family:Georgia,serif;font-size:21px;line-height:1.3"><a href="{url}" style="color:#1c1b19;text-decoration:none">{esc(d["titel"])}</a></h2>'
            f'<p style="margin:0 0 12px;font-size:15.5px;line-height:1.55;color:#1c1b19">{esc(_teaser(d))}</p>'
            f'{poster}'
            f'<p style="margin:12px 0 0;font-size:14px"><a href="{url}" style="color:#0b5b55;font-weight:600">{t["lesen"]} →</a>'
            + (f' &nbsp;·&nbsp; <a href="{url}" style="color:#0b5b55">{t["video"]}</a>' if d.get("video") else "")
            + (f' &nbsp;·&nbsp; {badge}' if badge else "") + "</p></div>")
        parts_txt.append(f"{d.get('ressort') or ''}\n{d['titel']}\n{_teaser(d)}\n{url}\n")
    html_body = (
        f'<!doctype html><html lang="{sp}"><body style="margin:0;background:#f6f5f1;font-family:Inter,Helvetica,Arial,sans-serif;color:#1c1b19">'
        f'<div style="max-width:620px;margin:0 auto;padding:28px 20px">'
        f'<p style="margin:0 0 4px;font-family:Georgia,serif;font-size:26px;font-weight:700"><a href="{B}/" style="color:#1c1b19;text-decoration:none">{esc(t["name"])}<span style="color:#0f766e">.</span></a></p>'
        f'<p style="margin:0 0 24px;font-size:13px;color:#5d5a54">{esc(datum)} · {esc(t["intro"])}</p>'
        + "".join(parts_html) +
        f'<p style="font-size:12px;color:#8b877e;line-height:1.5">{esc(t["ki"])}<br>{esc(t["fuss"])} '
        f'<a href="{unsub}" style="color:#8b877e">{t["abmelden"]}</a> · <a href="{SEITE}/datenschutz" style="color:#8b877e">{t["datenschutz"]}</a></p>'
        f'</div></body></html>')
    text = f"{t['name']}, {datum}\n{t['intro']}\n\n" + "\n".join(parts_txt) + f"\n{t['fuss']}\n{t['abmelden']}: {unsub}\n"
    return betreff, html_body, text, len(posts)


def versenden(sprache: str, tag: str) -> dict:
    con = connect()
    if con.execute("SELECT 1 FROM versand WHERE tag = ? AND sprache = ?", (tag, sprache)).fetchone():
        con.close()
        return {"sprache": sprache, "uebersprungen": "schon versendet"}
    betreff, html_t, text_t, n = tagesbrief(sprache, tag)
    if not n:
        con.close()
        return {"sprache": sprache, "uebersprungen": "keine Beiträge"}
    gesendet, fehler = 0, 0
    for email, token in empfaenger(sprache):
        unsub = f"{basis(sprache)}/newsletter/abmelden/{token}"
        try:
            senden_mail(email, betreff, text_t.replace("{unsub}", unsub), html_t.replace("{unsub}", unsub), unsub)
            gesendet += 1
        except Exception as e:  # noqa: BLE001
            fehler += 1
            print(f"  Fehler bei einem Empfänger: {type(e).__name__}", file=sys.stderr)
    con.execute("INSERT INTO versand (tag, sprache, anzahl, beitraege, at) VALUES (?, ?, ?, ?, ?)",
                (tag, sprache, gesendet, n, _now()))
    con.commit()
    con.close()
    return {"sprache": sprache, "beitraege": n, "gesendet": gesendet, "fehler": fehler}


def main() -> None:
    args = sys.argv[1:]
    tag = dt.date.today().isoformat()
    if "--tag" in args:
        tag = args[args.index("--tag") + 1]
    if "--status" in args:
        print(json.dumps(status(), indent=1))
    elif "--vorschau" in args:
        sp = args[args.index("--vorschau") + 1]
        betreff, html_t, _, n = tagesbrief(sp, tag)
        print(f"<!-- {betreff} · {n} Beiträge -->")
        print(html_t)
    elif "--senden" in args:
        sprachen = [args[args.index("--sprache") + 1]] if "--sprache" in args else list(ausgaben())
        for sp in sprachen:
            print(versenden(sp, tag))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
