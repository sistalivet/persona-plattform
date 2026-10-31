"""
Konstruktiv-Score fuer Persona AI News: bewertet einen Nachrichtentext (eigener Beitrag
oder fremder Artikel) nach den Kriterien des konstruktiven Journalismus.
Ein Prompt, eine Skala, damit eigene und fremde Texte vergleichbar sind.
Spezifikation: Desktop/Konstruktiv-Score Spezifikation.md (identischer Prompt).
"""
import html as _html
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

HIER = Path(__file__).resolve().parent
_AVATAR_TWIN = HIER.parent / "Avatar Twin"
if str(_AVATAR_TWIN) not in sys.path:
    sys.path.insert(0, str(_AVATAR_TWIN))

MODELL = "claude-sonnet-5-5"   # Bewertung auf dem kleineren Modell (Entscheidung Max, 18.10.2026; vorher claude-opus-5)
KRITERIEN = (
    "Du bewertest kurze Nachrichten-Texte nach den Kriterien des konstruktiven Journalismus. "
    "Antworte NUR mit einem JSON-Objekt, ohne Markdown, mit genau diesen Feldern:\n"
    '{"score": 0-100, "loesung": true/false, "belege": true/false, "grenzen": true/false, '
    '"ton": "nuechtern"|"lebendig"|"alarmistisch"|"werblich", "kurz": "ein Satz Begründung", '
    '"erlaeuterung": "zwei bis vier Sätze"}\n'
    "Kriterien: loesung = benennt eine Antwort, Lösung oder einen Fortschritt statt nur ein Problem; "
    "belege = nennt konkrete Fakten, Zahlen oder Quellen; grenzen = benennt ehrlich Einschränkungen "
    "oder offene Fragen; ton = Grundhaltung des Textes. score = Gesamtwert für 'konstruktiv': "
    "hoch bei lösungsorientiert, belegt, nüchtern-ermutigend; niedrig bei reiner Problembeschreibung, "
    "Alarmismus, Meinung ohne Belege. Bewerte streng, aber fair. Die Begründung (kurz) auf Deutsch, "
    "ein einziger Satz von höchstens 30 Wörtern. Die Erläuterung (erlaeuterung): zwei bis vier Sätze für "
    "Leserinnen und Leser, die das Urteil verständlich machen: Was macht der Text konstruktiv, was fehlt "
    "ihm, und was würde den Score erhöhen. Sachlich, ohne Fachjargon, ohne den Score zu wiederholen. "
    "Alles mit echten Umlauten (ä, ö, ü, ß), ohne Gedankenstriche."
)
TON_LABEL = {"nuechtern": "nüchtern", "lebendig": "lebendig", "alarmistisch": "alarmistisch", "werblich": "werblich"}
# Englische Ausgabe (18.10.2026): gleicher Prompt, gleiche Skala und Feldnamen, nur die beiden
# Begruendungstexte auf Englisch, weil sie unter dem Beitrag auf persona-ainews.ch/en stehen.
KRITERIEN_EN_ZUSATZ = (
    " Der Text, den du bewertest, ist auf Englisch. Schreibe die Felder kurz und erlaeuterung auf Englisch "
    "(the sentence in kurz and the two to four sentences in erlaeuterung in plain English, no dashes); "
    "alle anderen Felder und Werte bleiben genau wie oben vorgegeben (ton weiterhin einer der vier deutschen Werte)."
)


def _short(s: str, n: int = 300) -> str:
    s = s.strip()
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0].rstrip(",;:") + " …"


def score_text(text: str, model: str = MODELL, lang: str = "de") -> dict | None:
    """Bewertung als dict (score, loesung, belege, grenzen, ton, kurz) oder None bei Fehler.
    lang en: Begruendungen auf Englisch (englische Ausgabe), Skala und Felder unveraendert."""
    text = (text or "").strip()
    if len(text) < 80:
        return None
    try:
        import anthropic
        from script_engine import _read_key
        client = anthropic.Anthropic(api_key=_read_key(), timeout=30.0, max_retries=1)
        resp = client.messages.create(model=model, max_tokens=600, output_config={"effort": "low"},
                                      system=KRITERIEN + (KRITERIEN_EN_ZUSATZ if (lang or "de")[:2] == "en" else ""),
                                      messages=[{"role": "user", "content": text[:6000]}])
        raw = "".join(b.text for b in resp.content if b.type == "text").strip()
        raw = raw[raw.find("{"): raw.rfind("}") + 1]
        d = json.loads(raw)
        return {"score": max(0, min(100, int(d.get("score", 0)))),
                "loesung": bool(d.get("loesung")), "belege": bool(d.get("belege")),
                "grenzen": bool(d.get("grenzen")), "ton": str(d.get("ton", ""))[:20],
                "kurz": _short(str(d.get("kurz", "")).replace("–", ",").replace("—", ",")),
                "erlaeuterung": _short(str(d.get("erlaeuterung", "")).replace("–", ",").replace("—", ","), 900),
                "modell": model}
    except Exception:  # noqa: BLE001  (Bewertung darf nie etwas blockieren)
        return None


_UA = "Mozilla/5.0 (compatible; PersonaAINews/1.0; +https://persona-ainews.ch/so-bewerten-wir)"


def _strip(s: str) -> str:
    s = re.sub(r"(?is)<(script|style|noscript|svg|figure|nav|header|footer|aside).*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def fetch_article(url: str) -> dict:
    """Artikeltext von einer oeffentlichen Seite holen (nur fuer die Bewertung, wird nicht gespeichert).
    Liefert {ok, title, text, domain, error}."""
    if not re.match(r"https?://", url or ""):
        return {"ok": False, "error": "Bitte einen http(s)-Link angeben."}
    domain = re.sub(r"^www\.", "", url.split("//", 1)[1].split("/")[0].lower())
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept-Language": "de,en;q=0.7",
                                               "Accept": "text/html,application/xhtml+xml"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read(2_000_000)
            ctype = r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return {"ok": False, "domain": domain, "error": f"Seite antwortet mit Fehler {e.code}."}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "domain": domain, "error": f"Seite nicht erreichbar ({str(e)[:60]})."}
    if "html" not in ctype and b"<html" not in raw[:2000].lower():
        return {"ok": False, "domain": domain, "error": "Kein Artikel (keine HTML-Seite)."}
    charset = re.search(r"charset=([\w-]+)", ctype)
    for enc in ([charset.group(1)] if charset else []) + ["utf-8", "latin-1"]:
        try:
            html = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        html = raw.decode("utf-8", "replace")
    title = ""
    m = re.search(r'property="og:title"\s+content="([^"]+)"', html) or re.search(r'content="([^"]+)"\s+property="og:title"', html)
    if m:
        title = _html.unescape(m.group(1)).strip()
    if not title:
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
        title = _strip(m.group(1)) if m else ""
    m = re.search(r"(?is)<article[^>]*>(.*?)</article>", html)
    body = m.group(1) if m else html
    paras = [_strip(p) for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", body)]
    paras = [p for p in paras if len(p) >= 60]
    text = "\n".join(paras)
    if len(text) < 400:
        return {"ok": False, "domain": domain, "title": title,
                "error": "Zu wenig Artikeltext gefunden (Bezahlschranke oder Seite ohne Textabsätze)."}
    return {"ok": True, "domain": domain, "title": title[:200], "text": text[:8000]}


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg.startswith("http"):
        a = fetch_article(arg)
        print(json.dumps({k: (v[:200] if isinstance(v, str) else v) for k, v in a.items()}, ensure_ascii=False))
        if a.get("ok"):
            print(json.dumps(score_text(a["text"]), ensure_ascii=False))
    else:
        print(json.dumps(score_text(sys.stdin.read()), ensure_ascii=False))
