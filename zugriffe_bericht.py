#!/usr/bin/env python3
"""Taeglicher Zugriffsbericht fuer die Telegram-Gruppe (Entscheidung Max, 18.10.2026).

Liest den cookielosen Zaehler (data/metrics.db, platform_app: view, seen, play, progress, react),
fasst den Vortag und die letzten sieben Tage zusammen, getrennt nach deutscher und englischer
Ausgabe (ueber das Feld `sprache` der Beitraege), und schickt eine Nachricht ueber den Redaktionsbot
in die Gruppe der Kette. Keine Personendaten: der Zaehler kennt nur Tages-Hashes.

  python zugriffe_bericht.py            Bericht fuer gestern senden (Timer 08:00 Europe/Berlin)
  python zugriffe_bericht.py --heute    Bericht fuer den laufenden Tag (Test)
  python zugriffe_bericht.py --nur-text Nur ausgeben, nichts senden
"""
import datetime as dt
import html
import json
import sqlite3
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HIER = Path(__file__).resolve().parent
METRICS = HIER / "data" / "metrics.db"
BLOG = HIER / "data" / "blog"
KETTE_ENV = Path("/opt/persona/kette/.env")
KETTE_TELEGRAM = Path("/opt/persona/kette/data/telegram.json")
SEITE = "https://persona-ainews.ch"


def _env(path: Path) -> dict:
    out = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _posts() -> dict:
    """id -> (titel, sprache)"""
    out = {}
    for j in BLOG.glob("*.json"):
        try:
            d = json.loads(j.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        out[j.stem] = (d.get("titel") or j.stem, (d.get("sprache") or "de"))
    return out


def _rows(con, von: str, bis: str):
    return con.execute("SELECT day, kind, value, post, source, COUNT(*), COUNT(DISTINCT visitor) FROM events "
                       "WHERE day >= ? AND day <= ? GROUP BY day, kind, value, post, source", (von, bis)).fetchall()


def zusammenfassen(rows, posts: dict) -> dict:
    z = {"de": {"views": 0, "besucher": set(), "plays": 0, "p100": 0, "react": 0},
         "en": {"views": 0, "besucher": set(), "plays": 0, "p100": 0, "react": 0}}
    top: dict = {}
    quellen: dict = {}
    for day, kind, value, post, source, n, uniq in rows:
        sprache = posts.get(post, ("", "de"))[1] if post != "index" else "de"
        e = z["en" if sprache == "en" else "de"]
        if kind == "view":
            e["views"] += n
            e["besucher"].add((day, post, uniq))   # Besucher sind Tages-Hashes; Summe der Gruppen als Naeherung
            quellen[source or "direkt"] = quellen.get(source or "direkt", 0) + uniq
            if post != "index":
                top[post] = top.get(post, 0) + uniq
        elif kind == "play":
            e["plays"] += uniq
        elif kind == "progress" and value == "100":
            e["p100"] += uniq
        elif kind == "react":
            e["react"] += uniq
    for e in z.values():
        e["besucher"] = sum(u for _, _, u in e["besucher"])
    return {"ausgaben": z, "top": sorted(top.items(), key=lambda x: -x[1])[:3],
            "quellen": sorted(quellen.items(), key=lambda x: -x[1])[:3]}


def _zeile(name: str, e: dict) -> str:
    quote = f", {round(100 * e['p100'] / e['plays'])} % bis zum Ende" if e["plays"] else ""
    return (f"<b>{name}</b>: {e['views']} Aufrufe, {e['besucher']} Besucher, {e['plays']} Videostarts{quote}"
            + (f", {e['react']} Reaktionen" if e["react"] else ""))


def bericht(tag: dt.date) -> str:
    posts = _posts()
    con = sqlite3.connect(METRICS, timeout=10)
    try:
        gestern = zusammenfassen(_rows(con, tag.isoformat(), tag.isoformat()), posts)
        woche = zusammenfassen(_rows(con, (tag - dt.timedelta(days=6)).isoformat(), tag.isoformat()), posts)
    finally:
        con.close()
    esc = html.escape
    lines = [f"📊 <b>Zugriffe {tag.strftime('%d.%m.%Y')}</b>",
             _zeile("Deutsch", gestern["ausgaben"]["de"]), _zeile("Englisch", gestern["ausgaben"]["en"])]
    if gestern["top"]:
        lines.append("Meistgelesen:")
        for pid, n in gestern["top"]:
            titel, sprache = posts.get(pid, (pid, "de"))
            pfad = f"/en/{pid}" if sprache == "en" else f"/{pid}"
            lines.append(f"  {n} · <a href=\"{SEITE}{pfad}\">{esc(titel[:70])}</a>")
    if gestern["quellen"] and any(q != "direkt" for q, _ in gestern["quellen"]):
        lines.append("Herkunft: " + ", ".join(f"{esc(q)} {n}" for q, n in gestern["quellen"]))
    w = woche["ausgaben"]
    lines.append(f"<i>7 Tage: {w['de']['views'] + w['en']['views']} Aufrufe, "
                 f"{w['de']['besucher'] + w['en']['besucher']} Besucher, {w['de']['plays'] + w['en']['plays']} Videostarts; "
                 f"davon Englisch {w['en']['views']} Aufrufe</i>")
    return "\n".join(lines)


def senden(text: str) -> None:
    tok = _env(KETTE_ENV).get("TELEGRAM_BOT_TOKEN", "")
    chat = json.loads(KETTE_TELEGRAM.read_text(encoding="utf-8")).get("chat_id") if KETTE_TELEGRAM.is_file() else None
    if not (tok and chat):
        raise SystemExit("Telegram-Token oder Gruppe fehlt (kette/.env, kette/data/telegram.json)")
    body = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", body, timeout=30) as r:
        if r.status != 200:
            raise SystemExit(f"Telegram antwortete {r.status}")


def main() -> None:
    tag = dt.date.today() if "--heute" in sys.argv else dt.date.today() - dt.timedelta(days=1)
    text = bericht(tag)
    print(text)
    if "--nur-text" not in sys.argv:
        senden(text)


if __name__ == "__main__":
    main()
