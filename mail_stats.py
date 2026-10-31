"""
Screenshot-Statistiken per E-Mail (Persona AI News).

Wer eure Videos postet, schickt regelmaessig Screenshots seiner Plattform-Statistik an
das Postfach (data/mail.json). Dieses Modul holt die Mails ab (IMAP), nimmt nur Bilder
von REGISTRIERTEN Absendern (data/senders.json), liest die Kennzahlen per Claude aus,
speichert Bild + Werte (metrics.db, data/stats_inbox/) und antwortet dem Absender mit
den erkannten Werten. Unbekannte Absender werden ignoriert (Mail bleibt ungelesen).

  python mail_stats.py pull      # Postfach abrufen und auswerten (Timer: alle 15 Min)
  python mail_stats.py report    # Wochenbericht per ntfy (Timer: Montag 07:00)
  python mail_stats.py status
"""
import base64
import datetime as dt
import email
import email.header
import email.utils
import imaplib
import json
import re
import smtplib
import sqlite3
import sys
import unicodedata
from email.message import EmailMessage
from pathlib import Path

HIER = Path(__file__).resolve().parent
DATA = HIER / "data"
MAIL_CFG = DATA / "mail.json"
SENDERS = DATA / "senders.json"
INBOX_DIR = DATA / "stats_inbox"
DB = DATA / "metrics.db"
_AVATAR_TWIN = HIER.parent / "Avatar Twin"
if str(_AVATAR_TWIN) not in sys.path:
    sys.path.insert(0, str(_AVATAR_TWIN))

MODELL = "claude-opus-5"
PLATFORMS = ("instagram", "tiktok", "facebook", "youtube", "x", "linkedin", "threads", "bluesky", "other")
METRICS = ("reach", "views", "followers", "follower_change", "likes", "comments", "shares", "saves",
           "impressions", "watch_time_min", "profile_visits", "posts", "engagement")
MAX_IMG = 12 * 1024 * 1024

PROMPT = (
    "Du liest Screenshots von Social-Media-Statistiken (Instagram Insights, TikTok Analytics, "
    "YouTube Studio, Facebook/Meta Business Suite, X Analytics u. ä.) für eine Redaktion aus. "
    "Antworte NUR mit einem JSON-Objekt, ohne Markdown:\n"
    '{"platform": "instagram|tiktok|facebook|youtube|x|linkedin|threads|bluesky|other", '
    '"konto": "sichtbarer Kontoname oder leer", '
    '"zeitraum": {"von": "JJJJ-MM-TT oder leer", "bis": "JJJJ-MM-TT oder leer", "text": "wie im Bild, z. B. Letzte 7 Tage"}, '
    '"werte": [{"metric": "reach|views|followers|follower_change|likes|comments|shares|saves|impressions|watch_time_min|profile_visits|posts|engagement", '
    '"value": Zahl, "label_im_bild": "Beschriftung im Bild", "sicher": true/false}], '
    '"hinweis": "ein Satz, falls etwas unklar ist, sonst leer"}\n'
    "Regeln: Nur Zahlen, die im Bild stehen. Abkürzungen auflösen (12,4 Tsd. = 12400; 1,2 Mio. = 1200000; "
    "K = Tausend, M = Million). Prozentwerte NICHT als Kennzahl aufnehmen, ausser follower_change wenn absolut angegeben. "
    "Wenn eine Zahl mehrdeutig, abgeschnitten oder unscharf ist: sicher=false. Wenn der Zeitraum nicht im Bild steht, "
    "Felder leer lassen, nicht raten. Bezieht sich ein Bild auf einen einzelnen Beitrag statt auf ein Konto, "
    "schreibe das in hinweis. Heutiges Datum: {heute}."
)


# ----------------------------------------------------------------- Ablage
def _db() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=10)
    con.execute("CREATE TABLE IF NOT EXISTS stats_msgs(id INTEGER PRIMARY KEY, msg_id TEXT UNIQUE, received TEXT, "
                "sender TEXT, sender_name TEXT, subject TEXT, image TEXT, platform TEXT, konto TEXT, "
                "period_from TEXT, period_to TEXT, period_text TEXT, week TEXT, hinweis TEXT, raw TEXT, "
                "status TEXT, processed TEXT, error TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS stats_values(id INTEGER PRIMARY KEY, msg INTEGER, metric TEXT, "
                "value REAL, label TEXT, sure INTEGER, UNIQUE(msg, metric))")
    con.execute("CREATE TABLE IF NOT EXISTS stats_seen(msg_id TEXT PRIMARY KEY, at TEXT, note TEXT)")
    return con


def _nfc(s) -> str:
    return unicodedata.normalize("NFC", str(s or "")).strip()


def load_senders() -> dict:
    try:
        d = json.loads(SENDERS.read_text(encoding="utf-8")) if SENDERS.is_file() else {}
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def save_senders(d: dict):
    DATA.mkdir(parents=True, exist_ok=True)
    SENDERS.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def _cfg() -> dict:
    return json.loads(MAIL_CFG.read_text(encoding="utf-8"))


def _week_key(day: str) -> str:
    y, w, _ = dt.date.fromisoformat(day).isocalendar()
    return f"{y}-W{w:02d}"


def _dec(s) -> str:
    return "".join(t.decode(c or "utf-8", "replace") if isinstance(t, bytes) else t
                   for t, c in email.header.decode_header(s or ""))


# ----------------------------------------------------------------- KI-Auswertung
def read_screenshot(img_bytes: bytes, mime: str, model: str = MODELL) -> dict | None:
    try:
        import anthropic
        from script_engine import _read_key
        client = anthropic.Anthropic(api_key=_read_key(), timeout=60.0, max_retries=1)
        resp = client.messages.create(
            model=model, max_tokens=900, output_config={"effort": "low"},
            system=PROMPT.replace("{heute}", dt.date.today().isoformat()),
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime,
                                             "data": base64.b64encode(img_bytes).decode()}},
                {"type": "text", "text": "Lies die Statistik-Kennzahlen aus diesem Screenshot."}]}])
        raw = "".join(b.text for b in resp.content if b.type == "text").strip()
        d = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        vals = []
        for v in d.get("werte", []) or []:
            m = str(v.get("metric", "")).strip()
            try:
                x = float(v.get("value"))
            except (TypeError, ValueError):
                continue
            if m in METRICS:
                vals.append({"metric": m, "value": x, "label": _nfc(v.get("label_im_bild"))[:80],
                             "sure": bool(v.get("sicher", True))})
        z = d.get("zeitraum") or {}
        return {"platform": str(d.get("platform", "other")).lower() if str(d.get("platform", "")).lower() in PLATFORMS else "other",
                "konto": _nfc(d.get("konto"))[:80],
                "period_from": _nfc(z.get("von"))[:10], "period_to": _nfc(z.get("bis"))[:10],
                "period_text": _nfc(z.get("text"))[:60], "hinweis": _nfc(d.get("hinweis"))[:300],
                "werte": vals, "raw": raw[:4000]}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}


# ----------------------------------------------------------------- Mail
def _send_reply(cfg: dict, to: str, subject: str, body: str):
    try:
        msg = EmailMessage()
        msg["From"] = f"Persona AI News <{cfg['user']}>"
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        # Port 587 mit STARTTLS: Port 465 ist vom Hetzner-Server aus nicht erreichbar (19.10.2026)
        host, port = cfg.get("smtp_host", cfg["host"]), int(cfg.get("smtp_port", 587))
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30) as s:
                s.login(cfg["user"], cfg["password"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.ehlo()
                s.starttls()
                s.ehlo()
                s.login(cfg["user"], cfg["password"])
                s.send_message(msg)
        return True
    except Exception:  # noqa: BLE001
        return False


_LABELS = {"reach": "Reichweite", "views": "Aufrufe", "followers": "Follower", "follower_change": "Follower-Veränderung",
           "likes": "Likes", "comments": "Kommentare", "shares": "Geteilt", "saves": "Gespeichert",
           "impressions": "Impressionen", "watch_time_min": "Wiedergabezeit (Min.)", "profile_visits": "Profilbesuche",
           "posts": "Beiträge", "engagement": "Interaktionen"}


def _fmt(v: float) -> str:
    return f"{int(v):,}".replace(",", "'") if float(v).is_integer() else f"{v:,.1f}".replace(",", "'")


def _reply_text(name: str, res: dict, week: str) -> str:
    lines = [f"Hallo {name},", "", "danke für deinen Screenshot. Das haben wir erkannt:", ""]
    lines.append(f"Plattform: {res.get('platform', '?')}" + (f" ({res['konto']})" if res.get("konto") else ""))
    if res.get("period_text") or res.get("period_from"):
        lines.append(f"Zeitraum: {res.get('period_text') or (res.get('period_from', '') + ' bis ' + res.get('period_to', ''))} (Woche {week})")
    for v in res.get("werte", []):
        lines.append(f"{_LABELS.get(v['metric'], v['metric'])}: {_fmt(v['value'])}" + ("" if v["sure"] else "  (unsicher, wird geprüft)"))
    if not res.get("werte"):
        lines.append("Leider keine Kennzahlen gefunden. Bitte schick den Screenshot der Übersichtsseite (Reichweite, Aufrufe, Follower).")
    if res.get("hinweis"):
        lines += ["", "Hinweis: " + res["hinweis"]]
    lines += ["", "Stimmt etwas nicht? Einfach auf diese Mail antworten.", "", "Persona AI News"]
    return "\n".join(lines)


def pull(limit: int = 30) -> dict:
    cfg = _cfg()
    senders = load_senders()
    out = {"checked": 0, "processed": 0, "skipped_unknown": 0, "errors": 0, "items": []}
    con = _db()
    M = imaplib.IMAP4_SSL(cfg["host"], int(cfg.get("imap_port", 993)))
    M.login(cfg["user"], cfg["password"])
    M.select("INBOX")
    typ, data = M.search(None, "UNSEEN")
    ids = data[0].split()[-limit:]
    for i in ids:
        typ, d = M.fetch(i, "(BODY.PEEK[])")
        msg = email.message_from_bytes(d[0][1])
        out["checked"] += 1
        mid = (msg.get("Message-ID") or f"<no-id-{i.decode()}-{msg.get('Date', '')}>").strip()
        if con.execute("SELECT 1 FROM stats_seen WHERE msg_id=?", (mid,)).fetchone():
            continue
        addr = (email.utils.parseaddr(msg.get("From", ""))[1] or "").lower()
        reg = senders.get(addr)
        if not reg:
            out["skipped_unknown"] += 1
            continue   # bleibt ungelesen; unbekannte Absender ignorieren wir bewusst
        try:
            rdate = email.utils.parsedate_to_datetime(msg.get("Date")) if msg.get("Date") else dt.datetime.now(dt.timezone.utc)
        except (TypeError, ValueError):
            rdate = dt.datetime.now(dt.timezone.utc)
        received = rdate.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
        images = []
        for part in msg.walk():
            if part.get_content_maintype() == "image":
                b = part.get_payload(decode=True) or b""
                if 5_000 < len(b) <= MAX_IMG:
                    images.append((part.get_content_type(), b, part.get_filename() or "bild"))
        subject = _dec(msg.get("Subject"))[:120]
        if not images:
            con.execute("INSERT OR REPLACE INTO stats_seen VALUES (?,?,?)", (mid, received, "kein Bild"))
            con.commit()
            _send_reply(cfg, addr, "Re: " + subject, f"Hallo {reg.get('name', '')},\n\nin deiner Mail war kein Bild. Bitte den Screenshot als Anhang oder direkt in die Mail einfügen.\n\nPersona AI News")
            M.store(i, "+FLAGS", "\\Seen")
            continue
        for k, (mime, b, fname) in enumerate(images[:4]):
            ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(mime, ".img")
            if mime not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
                continue
            folder = INBOX_DIR / rdate.strftime("%Y-%m")
            folder.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^a-z0-9]+", "-", addr.split("@")[0].lower())[:20]
            img_path = folder / f"{rdate.strftime('%Y%m%d-%H%M%S')}-{safe}-{k}{ext}"
            img_path.write_bytes(b)
            res = read_screenshot(b, mime)
            rel = str(img_path.relative_to(DATA)).replace("\\", "/")
            if not res or res.get("error"):
                con.execute("INSERT OR IGNORE INTO stats_msgs(msg_id, received, sender, sender_name, subject, image, status, processed, error) "
                            "VALUES (?,?,?,?,?,?,?,?,?)", (f"{mid}#{k}", received, addr, reg.get("name", ""), subject, rel, "error",
                                                          dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), (res or {}).get("error", "unbekannt")))
                out["errors"] += 1
                continue
            platform = reg.get("platform") or res["platform"]
            konto = reg.get("konto") or res.get("konto", "")
            day = res.get("period_to") or received[:10]
            try:
                week = _week_key(day)
            except ValueError:
                week = _week_key(received[:10])
            status = "ok" if res["werte"] and all(v["sure"] for v in res["werte"]) else "check"
            cur = con.execute("INSERT OR IGNORE INTO stats_msgs(msg_id, received, sender, sender_name, subject, image, platform, konto, "
                              "period_from, period_to, period_text, week, hinweis, raw, status, processed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (f"{mid}#{k}", received, addr, reg.get("name", ""), subject, rel, platform, konto,
                               res.get("period_from", ""), res.get("period_to", ""), res.get("period_text", ""), week,
                               res.get("hinweis", ""), res.get("raw", ""), status,
                               dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
            msg_row = cur.lastrowid
            for v in res["werte"]:
                con.execute("INSERT OR REPLACE INTO stats_values(msg, metric, value, label, sure) VALUES (?,?,?,?,?)",
                            (msg_row, v["metric"], v["value"], v["label"], int(v["sure"])))
            out["processed"] += 1
            out["items"].append({"sender": addr, "platform": platform, "week": week, "n": len(res["werte"]), "status": status})
            res2 = dict(res, platform=platform, konto=konto)
            _send_reply(cfg, addr, "Re: " + subject, _reply_text(reg.get("name", ""), res2, week))
        con.execute("INSERT OR REPLACE INTO stats_seen VALUES (?,?,?)", (mid, received, "verarbeitet"))
        con.commit()
        M.store(i, "+FLAGS", "\\Seen")
    M.logout()
    con.close()
    return out


# ----------------------------------------------------------------- Auswertung
def rows(weeks: int = 8) -> list:
    con = _db()
    since = (dt.date.today() - dt.timedelta(days=7 * weeks)).isoformat()
    ms = con.execute("SELECT id, msg_id, received, sender, sender_name, subject, image, platform, konto, period_from, "
                     "period_to, period_text, week, hinweis, status, error FROM stats_msgs WHERE received>=? "
                     "ORDER BY received DESC", (since,)).fetchall()
    out = []
    for m in ms:
        vals = con.execute("SELECT metric, value, label, sure FROM stats_values WHERE msg=? ORDER BY id", (m[0],)).fetchall()
        out.append({"id": m[0], "received": m[2], "sender": m[3], "name": m[4], "subject": m[5], "image": m[6],
                    "platform": m[7], "konto": m[8], "period_from": m[9], "period_to": m[10], "period_text": m[11],
                    "week": m[12], "hinweis": m[13], "status": m[14], "error": m[15],
                    "values": [{"metric": v[0], "value": v[1], "label": v[2], "sure": bool(v[3])} for v in vals]})
    con.close()
    return out


def report() -> str:
    """Wochenbericht als Text (fuer ntfy). Wer hat geliefert, wer nicht, Entwicklung je Konto."""
    senders = load_senders()
    data = rows(weeks=3)
    today = dt.date.today()
    this_week = _week_key(today.isoformat())
    last_week = _week_key((today - dt.timedelta(days=7)).isoformat())
    delivered = {(r["sender"]) for r in data if r["week"] in (this_week, last_week) and r["status"] != "error"}
    missing = [v.get("name") or k for k, v in senders.items() if k not in delivered]
    lines = [f"Screenshot-Statistiken, Stand {today.strftime('%d.%m.%Y')}"]
    by_acc: dict = {}
    for r in data:
        key = f"{r['platform']}{' ' + r['konto'] if r['konto'] else ''} ({r['name'] or r['sender']})"
        by_acc.setdefault(key, {})[r["week"]] = {v["metric"]: v["value"] for v in r["values"]}
    for key, wk in sorted(by_acc.items()):
        weeks_sorted = sorted(wk)
        cur = wk[weeks_sorted[-1]]
        prev = wk[weeks_sorted[-2]] if len(weeks_sorted) > 1 else {}
        parts = []
        for m in ("reach", "views", "followers", "engagement", "likes"):
            if m in cur:
                s = f"{_LABELS[m]} {_fmt(cur[m])}"
                if m in prev and prev[m]:
                    s += f" ({(cur[m] - prev[m]) / prev[m] * 100:+.0f} %)"
                parts.append(s)
        lines.append(f"{key}, {weeks_sorted[-1]}: " + (", ".join(parts) if parts else "keine Kennzahlen"))
    open_checks = sum(1 for r in data if r["status"] == "check")
    if open_checks:
        lines.append(f"{open_checks} Eintrag/Einträge warten auf Prüfung in Persona (Goals).")
    if missing:
        lines.append("Nicht geliefert: " + ", ".join(missing))
    return "\n".join(lines)


def status() -> dict:
    con = _db()
    n = con.execute("SELECT COUNT(*), MAX(received) FROM stats_msgs").fetchone()
    con.close()
    return {"configured": MAIL_CFG.is_file(), "senders": len(load_senders()), "messages": n[0], "last": n[1]}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "pull":
        print(json.dumps(pull(), ensure_ascii=False))
    elif cmd == "report":
        txt = report()
        print(txt)
        try:
            sys.path.insert(0, str(HIER))
            import platform_app
            platform_app._ntfy("Persona: Wochenzahlen", txt, tags="bar_chart", tag="stats")
        except Exception as e:  # noqa: BLE001
            print("ntfy nicht gesendet:", str(e)[:80])
    else:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
