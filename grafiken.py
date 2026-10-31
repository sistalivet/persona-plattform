"""Grafikstrecke: Vollbildkarten im Wechsel mit dem Avatar.

Laeuft in twin_pipeline.py nach dem Veredeln und vor den Untertiteln (--grafik-plan), damit
Untertitel und KI-Label auch ueber den Karten liegen (Entscheidung Max, 13. Oktober).

Eingang: das fertige Avatar-Video (Untertitel und KI-Label schon eingebrannt), der Grafik-Plan
der Kette (Chip, Karten, Quellenzeile; vertrag/datensatz.md Abschnitt 5), der gesprochene Text,
die Wort-Zeiten des Renders (.align.json) und die Byline (Name, Bauchbinde).
Ausgang: ein Video gleicher Laenge, in dem der Avatar und Vollbildkarten abwechseln; die Stimme
laeuft durch. Karten erscheinen an ihrem Ausloeser (Wortfolge im gesprochenen Text), Karten ohne
Ausloeser fuellen die Luecken. Bauchbinde in den ersten Sekunden, Quellenzeile in den letzten.
Nur Pillow und ffmpeg, kein GPU. Regeln: vertrag/grafiken.md der Kette.
"""
from __future__ import annotations

import json
import re
import subprocess
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920
MARGIN = 90
FUSS_STANDARD = "Persona AI News"   # Fuss einer Karte ohne Quellenzeile; die Kampagnen-Kette setzt ihn leer
INHALT_MAX = 1370   # darunter liegen die Untertitel (werden nach den Karten eingebrannt)
# Farben der Website (platform_app, :root)
C = {"bg": "#f6f5f1", "surface": "#ffffff", "line": "#e6e2d9", "line2": "#d5d0c5", "ink": "#1c1b19",
     "muted": "#5d5a54", "faint": "#8b877e", "accent": "#0f766e", "accent_dark": "#0b5b55",
     "accent_soft": "#e3f1ef", "down": "#b4533a", "warn_bg": "#fdf3e7", "warn": "#9a5b12"}
RESSORT_FARBE = {"Politik und Gesellschaft": "#334e68", "Wirtschaft und Finanzen": "#0f766e",
                 "Tech und Wissenschaft": "#4c4ba3", "Kultur und Sport": "#a0522d", "Umwelt und Natur": "#3f7d3a",
                 # englische Ausgabe (site_en.json)
                 "World": "#334e68", "US Politics": "#7a2e3b", "Economy": "#0f766e", "Technology": "#4c4ba3",
                 "Science and Health": "#3f7d3a", "Culture and Sport": "#a0522d"}

# Zeiten (Sekunden), vertrag/grafiken.md Abschnitt 3
START_MIN = 4.0        # keine Karte in den ersten vier Sekunden (Avatar mit Bauchbinde)
KARTE_DAUER = 5.0
KARTE_MIN = 3.5
LUECKE = 3.0           # Avatar zwischen zwei Karten
SCHLUSS = 3.0          # Avatar am Ende, wenn es passt
FADE = 0.3
VORLAUF = 1.5          # ein Ausloeser darf so viel frueher starten, wenn es sonst nicht passt
QUELLEN_DAUER = 4.0
BAUCHBINDE = (0.8, 4.0)

_FONT_DIRS = [Path(__file__).resolve().parent / "static" / "fonts", Path("/usr/share/fonts/truetype")]
_FONT_FILES = {
    "serif": ["SourceSerif4-Regular.ttf", "SourceSerif4-Semibold.ttf", "dejavu/DejaVuSerif.ttf"],
    "serif_bold": ["SourceSerif4-Bold.ttf", "SourceSerif4-Semibold.ttf", "dejavu/DejaVuSerif-Bold.ttf"],
    "sans": ["Inter-Regular.ttf", "Inter.ttf", "dejavu/DejaVuSans.ttf"],
    "sans_bold": ["Inter-Bold.ttf", "Inter-SemiBold.ttf", "dejavu/DejaVuSans-Bold.ttf"],
}


def _font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    for d in _FONT_DIRS:
        for name in _FONT_FILES[kind]:
            p = d / name
            if p.is_file():
                return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size)


# ---------------------------------------------------------------- Text

def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    words, lines, cur = (text or "").split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _block(draw, x: int, y: int, text: str, font, fill: str, max_w: int, line_h: float = 1.25,
           max_lines: int = 8, limit: int = INHALT_MAX) -> int:
    """Draw wrapped text above `limit`; returns the y below the block."""
    lines = _wrap(draw, text, font, max_w)[:max_lines]
    lh = int(font.size * line_h)
    for ln in lines:
        if y + lh > limit:
            break
        draw.text((x, y), ln, font=font, fill=fill)
        y += lh
    return y


def _caps(s: str) -> str:
    return (s or "").upper()


LANG = "de"   # Sprache der Karten (de/en); twin_pipeline setzt sie aus dem Grafik-Plan (englische Ausgabe, 18.10.2026)
_WORTE = {"de": {"heute": "heute", "ziel": "Ziel"}, "en": {"heute": "today", "ziel": "Target"}}


def _wort(key: str) -> str:
    return _WORTE.get(LANG, _WORTE["de"]).get(key, key)


def _num_de(v) -> str:
    """Zahl im Format der Ausgabe: de 1.234,5, en 1,234.5."""
    if isinstance(v, (int, float)):
        s = f"{v:,.2f}".rstrip("0").rstrip(".")
        if LANG == "en":
            return s
        s = s.replace(",", "X").replace(".", ",").replace("X", ".")
        return s
    return str(v)


# ---------------------------------------------------------------- Karten

def _canvas(chip: str, ressort: str, label: str = "", fuss: str = "") -> tuple[Image.Image, ImageDraw.ImageDraw, int]:
    """Opaque card with chip, kicker label and footer. Returns (img, draw, y of the content start)."""
    img = Image.new("RGBA", (W, H), C["bg"])
    d = ImageDraw.Draw(img)
    # Kopfzeile
    d.rectangle((0, 0, W, 14), fill=RESSORT_FARBE.get(ressort, C["accent"]))
    y = 150
    if chip:
        f = _font("sans_bold", 34)
        tw = d.textlength(chip, font=f)
        d.rounded_rectangle((MARGIN, y, MARGIN + tw + 56, y + 70), radius=35, fill=RESSORT_FARBE.get(ressort, C["accent"]))
        d.text((MARGIN + 28, y + 14), chip, font=f, fill="#ffffff")
        y += 70
    y += 110
    if label:
        f = _font("sans_bold", 36)
        d.text((MARGIN, y), _caps(label), font=f, fill=C["muted"])
        y += 56
        d.line((MARGIN, y + 10, MARGIN + 140, y + 10), fill=C["accent"], width=6)
        y += 60
    # Fuss
    d.line((MARGIN, H - 230, W - MARGIN, H - 230), fill=C["line"], width=2)
    if fuss:
        _block(d, MARGIN, H - 205, fuss, _font("sans", 30), C["muted"], W - 2 * MARGIN, 1.25, 2, limit=H - 90)
    elif FUSS_STANDARD:
        d.text((MARGIN, H - 200), FUSS_STANDARD, font=_font("serif_bold", 34), fill=C["muted"])
    return img, d, y


def karte_kernzahl(k: dict, chip: str, ressort: str, fuss: str = "") -> Image.Image:
    f_ = k.get("felder") or {}
    img, d, y = _canvas(chip, ressort, f_.get("label", ""), fuss)
    zahl = str(f_.get("zahl", ""))
    size = 260 if len(zahl) <= 5 else (200 if len(zahl) <= 8 else 150)
    d.text((MARGIN - 6, y - 20), zahl, font=_font("serif_bold", size), fill=C["accent_dark"])
    y += size + 60
    y = _block(d, MARGIN, y, f_.get("titel", ""), _font("serif_bold", 80), C["ink"], W - 2 * MARGIN, 1.15, 3)
    if f_.get("untertitel"):
        y = _block(d, MARGIN, y + 10, f_["untertitel"], _font("sans", 48), C["muted"], W - 2 * MARGIN, 1.3, 2)
    d.line((MARGIN, y + 50, W - MARGIN, y + 50), fill=C["line2"], width=2)
    _block(d, MARGIN, y + 90, f_.get("satz", ""), _font("sans", 50), C["ink"], W - 2 * MARGIN, 1.35, 5)
    return img


def karte_vergleich(k: dict, chip: str, ressort: str, fuss: str = "") -> Image.Image:
    f_ = k.get("felder") or {}
    img, d, y = _canvas(chip, ressort, f_.get("label", ""), fuss)
    balken = [b for b in (f_.get("balken") or []) if isinstance(b, dict)][:3]
    vals = [float(b.get("wert") or 0) for b in balken]
    vmax = max([abs(v) for v in vals] + [1e-9])
    top, bottom = y + 120, y + 560
    n = max(len(balken), 1)
    slot = (W - 2 * MARGIN) / n
    bw = min(260, int(slot * 0.55))
    for i, b in enumerate(balken):
        v = float(b.get("wert") or 0)
        h = int((bottom - top) * (abs(v) / vmax))
        x0 = int(MARGIN + slot * i + (slot - bw) / 2)
        col = C["accent"] if i == len(balken) - 1 else C["line2"]
        d.rounded_rectangle((x0, bottom - h, x0 + bw, bottom), radius=18, fill=col)
        val = _num_de(v) + (" " + str(b.get("einheit") or "") if b.get("einheit") else "")
        fv = _font("serif_bold", 72)
        d.text((x0 + bw / 2 - d.textlength(val, font=fv) / 2, bottom - h - 95), val, font=fv,
               fill=C["accent_dark"] if i == len(balken) - 1 else C["muted"])
        fn = _font("sans", 42)
        name = str(b.get("name") or "")
        d.text((x0 + bw / 2 - d.textlength(name, font=fn) / 2, bottom + 24), name, font=fn, fill=C["muted"])
    d.line((MARGIN, bottom, W - MARGIN, bottom), fill=C["line2"], width=3)
    y = _block(d, MARGIN, bottom + 110, f_.get("fazit", ""), _font("serif_bold", 60), C["ink"], W - 2 * MARGIN, 1.2, 3)
    if f_.get("fussnote"):
        _block(d, MARGIN, y + 24, f_["fussnote"], _font("sans", 36), C["muted"], W - 2 * MARGIN, 1.3, 2)
    return img


def karte_liste(k: dict, chip: str, ressort: str, fuss: str = "") -> Image.Image:
    f_ = k.get("felder") or {}
    img, d, y = _canvas(chip, ressort, f_.get("label", ""), fuss)
    y += 20
    fz = _font("sans_bold", 58)
    for z in (f_.get("zeilen") or [])[:5]:
        if not isinstance(z, dict):
            continue
        r = (z.get("richtung") or "neutral").lower()
        glyph, col = {"hoch": ("▲", C["accent"]), "runter": ("▼", C["down"])}.get(r, ("●", C["faint"]))
        d.rounded_rectangle((MARGIN, y, MARGIN + 92, y + 92), radius=22, fill=C["accent_soft"] if r == "hoch" else C["surface"],
                            outline=C["line2"], width=2)
        fg = _font("sans_bold", 46)
        d.text((MARGIN + 46 - d.textlength(glyph, font=fg) / 2, y + 16), glyph, font=fg, fill=col)
        lines = _wrap(d, str(z.get("text") or ""), fz, W - 2 * MARGIN - 130)[:2]
        ty = y + (14 if len(lines) == 1 else -14)
        for ln in lines:
            d.text((MARGIN + 130, ty), ln, font=fz, fill=C["ink"])
            ty += int(fz.size * 1.2)
        y += 140
        d.line((MARGIN + 130, y - 28, W - MARGIN, y - 28), fill=C["line"], width=2)
    if f_.get("satz"):
        _block(d, MARGIN, y + 40, f_["satz"], _font("sans", 50), C["muted"], W - 2 * MARGIN, 1.35, 4)
    return img


def karte_hinweis(k: dict, chip: str, ressort: str, fuss: str = "") -> Image.Image:
    f_ = k.get("felder") or {}
    img, d, y = _canvas(chip, ressort, "", fuss)
    # Band
    d.rounded_rectangle((MARGIN, y, W - MARGIN, y + 90), radius=20, fill=C["warn_bg"])
    d.text((MARGIN + 30, y + 22), _caps(f_.get("label") or "Wichtig"), font=_font("sans_bold", 40), fill=C["warn"])
    y += 170
    y = _block(d, MARGIN, y, f_.get("titel", ""), _font("serif_bold", 88), C["ink"], W - 2 * MARGIN, 1.12, 3)
    y = _block(d, MARGIN, y + 40, f_.get("satz", ""), _font("sans", 56), C["ink"], W - 2 * MARGIN, 1.35, 6)
    if f_.get("fussnote"):
        d.line((MARGIN, y + 50, MARGIN + 140, y + 50), fill=C["line2"], width=4)
        _block(d, MARGIN, y + 90, f_["fussnote"], _font("sans", 42), C["muted"], W - 2 * MARGIN, 1.3, 3)
    return img


def karte_ziel(k: dict, chip: str, ressort: str, fuss: str = "") -> Image.Image:
    f_ = k.get("felder") or {}
    img, d, y = _canvas(chip, ressort, f_.get("label", ""), fuss)
    try:
        ziel, heute = float(f_.get("ziel") or 0), float(f_.get("heute") or 0)
    except (TypeError, ValueError):
        ziel, heute = 0.0, 0.0
    frac = max(0.0, min(1.0, heute / ziel)) if ziel else 0.0
    y += 40
    fh = _font("serif_bold", 120)
    d.text((MARGIN, y), f_.get("heute_text") or _num_de(heute), font=fh, fill=C["accent_dark"])
    y += 150
    d.text((MARGIN, y), _wort("heute"), font=_font("sans", 44), fill=C["muted"])
    y += 130
    x0, x1 = MARGIN, W - MARGIN
    d.rounded_rectangle((x0, y, x1, y + 56), radius=28, fill=C["line"])
    if frac > 0:
        d.rounded_rectangle((x0, y, x0 + int((x1 - x0) * frac), y + 56), radius=28, fill=C["accent"])
    y += 90
    fz = _font("serif_bold", 64)
    zt = f_.get("ziel_text") or f"{_wort('ziel')} {_num_de(ziel)}"
    d.text((x1 - d.textlength(zt, font=fz), y), zt, font=fz, fill=C["ink"])
    y += 170
    _block(d, MARGIN, y, f_.get("satz", ""), _font("sans", 52), C["ink"], W - 2 * MARGIN, 1.35, 5)
    return img


KARTEN = {"kernzahl": karte_kernzahl, "vergleich": karte_vergleich, "liste": karte_liste,
          "hinweis": karte_hinweis, "ziel": karte_ziel}


def bauchbinde_png(name: str, zeile: str) -> Image.Image:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fn, fz = _font("serif_bold", 54), _font("sans", 38)
    z = (zeile or "").strip()
    if z.lower().startswith(name.strip().lower()):   # Profilfeld "bauchbinde" beginnt oft mit dem Namen
        z = z[len(name.strip()):].lstrip(" ·,:-|").strip()
    zeile = z
    tw = max(d.textlength(name, font=fn), d.textlength(zeile or "", font=fz)) + 110
    tw = min(tw, W - 2 * 70)
    # Unter den Untertiteln (Band um 80 % der Hoehe, bis etwa 1610) und ueber dem KI-Label am
    # unteren Rand (ab etwa 1850): Entscheidung Max, 18.10.2026, die Binde lag vorher ueber dem Gesicht.
    x0, y0 = 70, 1660
    hgt = 150 if zeile else 100
    d.rounded_rectangle((x0, y0, x0 + tw, y0 + hgt), radius=16, fill=(255, 255, 255, 235))
    d.rectangle((x0, y0 + 14, x0 + 12, y0 + hgt - 14), fill=C["accent"])
    d.text((x0 + 42, y0 + 18), name, font=fn, fill=C["ink"])
    if zeile:
        d.text((x0 + 42, y0 + 88), zeile, font=fz, fill=C["muted"])
    return img


def quellen_png(text: str) -> Image.Image:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f = _font("sans", 32)
    lines = _wrap(d, text or "", f, W - 2 * 80 - 60)[:2]
    hgt = 40 + len(lines) * 44
    y0 = 1340 - hgt
    d.rounded_rectangle((70, y0, W - 70, y0 + hgt), radius=14, fill=(255, 255, 255, 225))
    y = y0 + 20
    for ln in lines:
        d.text((100, y), ln, font=f, fill=C["muted"])
        y += 44
    return img


# ---------------------------------------------------------------- Zeitplan

def _norm_token(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    return re.sub(r"[^\w]", "", s, flags=re.U)


def _find_phrase(words: list[dict], phrase: str) -> float | None:
    toks = [_norm_token(t) for t in phrase.split()]
    toks = [t for t in toks if t]
    if not toks:
        return None
    seq = [(_norm_token(w.get("text", "")), float(w.get("start", 0))) for w in words]
    seq = [(t, s) for t, s in seq if t]
    for i in range(len(seq) - len(toks) + 1):
        if all(seq[i + j][0] == toks[j] for j in range(len(toks))):
            return seq[i][1]
    # tolerant: first token alone (numbers like "1,3" may be tokenised differently)
    first = toks[0]
    for t, s in seq:
        if t == first and len(first) >= 4:
            return s
    return None


def zeitplan(plan: dict, words: list[dict], dauer: float) -> list[dict]:
    """Cards with start/end. Anchored cards at their trigger, the others in the gaps.
    Returns a list of {"karte": card, "start": s, "end": e} in time order."""
    karten = [k for k in (plan.get("karten") or []) if isinstance(k, dict) and k.get("vorlage") in KARTEN]
    karten.sort(key=lambda k: int(k.get("nr") or 0))
    end_max = dauer - 0.6
    anchored, free = [], []
    for k in karten:
        wort = ((k.get("ausloeser") or {}).get("wort") or "").strip()
        t = _find_phrase(words, wort) if wort else None
        if t is not None:
            anchored.append({"karte": k, "anker": max(t - 0.15, START_MIN)})
        else:
            free.append(k)
    anchored.sort(key=lambda a: a["anker"])
    placed: list[dict] = []
    # anchored first, in time order; pull back by at most VORLAUF when the card would not fit
    cursor = START_MIN
    for a in anchored:
        start = max(a["anker"], cursor)
        dur = KARTE_DAUER
        if start + dur > end_max:
            dur = max(KARTE_MIN, end_max - start)
            if start + dur > end_max:
                start = max(cursor, a["anker"] - VORLAUF, end_max - dur)
            if start + KARTE_MIN > end_max:
                continue  # no room at all
        placed.append({"karte": a["karte"], "start": round(start, 2), "end": round(min(start + dur, end_max), 2)})
        cursor = start + dur + LUECKE
    # free cards (priority 2, 3, 5 by nr) into the earliest gap that fits
    for k in sorted(free, key=lambda k: int(k.get("nr") or 0)):
        windows = []
        t0 = START_MIN
        for p in sorted(placed, key=lambda p: p["start"]):
            windows.append((t0, p["start"] - LUECKE))
            t0 = p["end"] + LUECKE
        windows.append((t0, end_max - SCHLUSS))
        for w0, w1 in windows:
            if w1 - w0 >= KARTE_MIN:
                dur = min(KARTE_DAUER, w1 - w0)
                start = w0 + (w1 - w0 - dur) / 2 if (w1 - w0 - dur) < LUECKE else w0
                placed.append({"karte": k, "start": round(start, 2), "end": round(start + dur, 2)})
                break
    placed.sort(key=lambda p: p["start"])
    return placed


# ---------------------------------------------------------------- Zusammenbau

def _dauer(video: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def anreichern(video: Path, plan: dict, words: list[dict], byline: dict, ressort: str, out: Path,
               work: Path | None = None, log=print) -> dict:
    """Build the enriched video. Returns {"karten": n, "zeitplan": [...], "file": out}."""
    work = work or out.parent / f"_grafik_{out.stem}"
    work.mkdir(parents=True, exist_ok=True)
    dauer = _dauer(video)
    chip = str(plan.get("chip") or "")
    slots = zeitplan(plan, words, dauer)
    overlays: list[tuple[Path, float, float]] = []  # (png, start, end)
    quellen = str(plan.get("quellenzeile") or "")
    q_start = max(0.0, dauer - QUELLEN_DAUER)
    for i, s in enumerate(slots, 1):
        k = s["karte"]
        fuss = quellen if (quellen and s["end"] > q_start) else ""   # last card carries the source line
        if fuss:
            q_start = max(q_start, s["end"])
        img = KARTEN[k["vorlage"]](k, chip, ressort, fuss)
        p = work / f"karte_{i}_{k['vorlage']}.png"
        img.save(p)
        overlays.append((p, s["start"], s["end"]))
        log(f"Karte {k.get('nr')} ({k['vorlage']}): {s['start']} bis {s['end']} s")
    if byline.get("name"):
        p = work / "bauchbinde.png"
        bauchbinde_png(byline["name"], byline.get("bauchbinde") or "").save(p)
        overlays.append((p, BAUCHBINDE[0], min(BAUCHBINDE[1], dauer - 0.5)))
    if quellen and dauer - 0.2 - q_start >= 1.0:
        p = work / "quellen.png"
        quellen_png(quellen).save(p)
        overlays.append((p, q_start, dauer - 0.2))

    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(video)]
    fc, last = [], "0:v"
    for i, (png, start, end) in enumerate(overlays, 1):
        d = max(0.5, end - start)
        cmd += ["-loop", "1", "-framerate", "30", "-t", f"{d:.2f}", "-i", str(png)]
        fade = min(FADE, d / 3)
        fc.append(f"[{i}:v]format=rgba,fade=t=in:st=0:d={fade:.2f}:alpha=1,"
                  f"fade=t=out:st={d - fade:.2f}:d={fade:.2f}:alpha=1,setpts=PTS-STARTPTS+{start:.2f}/TB[o{i}]")
        fc.append(f"[{last}][o{i}]overlay=0:0:eof_action=pass:format=auto[v{i}]")
        last = f"v{i}"
    cmd += ["-filter_complex", ";".join(fc), "-map", f"[{last}]", "-map", "0:a?", "-c:v", "libx264", "-crf", "18",
            "-preset", "medium", "-pix_fmt", "yuv420p", "-c:a", "copy", "-map_metadata", "0",
            "-movflags", "+faststart+use_metadata_tags", str(out)]
    subprocess.run(cmd, check=True)
    (work / "zeitplan.json").write_text(json.dumps(
        [{"nr": s["karte"].get("nr"), "vorlage": s["karte"]["vorlage"], "start": s["start"], "end": s["end"]} for s in slots],
        ensure_ascii=False, indent=1), encoding="utf-8")
    return {"karten": len(slots), "zeitplan": slots, "file": out}


def load_words(align: Path | None) -> list[dict]:
    if align and Path(align).is_file():
        try:
            return json.loads(Path(align).read_text(encoding="utf-8")).get("words") or []
        except ValueError:
            return []
    return []
