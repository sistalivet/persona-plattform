"""Archiv der Beiträge (Entscheidung Max, 3. November 2026).

Je Ressort bleiben die jüngsten N Beiträge einer Ausgabe öffentlich, alle anderen bekommen das Feld
`archiviert` (Zeitstempel) und verschwinden von der Website (Listen, Feeds, Sitemap, Suche, Dossiers,
Verwandte; Artikelseite antwortet 410). Die Dateien in data/blog bleiben vollständig erhalten, ebenso
Videos, Karten und Register-Einträge; die Redaktion sieht archivierte Beiträge weiter in der App.

  /opt/persona/venv/bin/python archiv.py --ausgabe de --behalte 3 --trocken    zeigen, was passieren würde
  /opt/persona/venv/bin/python archiv.py --ausgabe de --behalte 3              archivieren
  /opt/persona/venv/bin/python archiv.py --zurueck p_abc p_def                 Beiträge wieder veröffentlichen
  /opt/persona/venv/bin/python archiv.py --stand                               Zahlen je Ausgabe

Idempotent: ein zweiter Lauf ändert nichts, solange keine neuen Beiträge dazukamen.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

HIER = Path(__file__).resolve().parent
sys.path.insert(0, str(HIER))

from persona.kern import BLOG_DIR, _eintrag_ausgabe  # noqa: E402


def _lade() -> list[dict]:
    out = []
    for p in sorted(BLOG_DIR.glob("*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if isinstance(rec, dict) and rec.get("titel"):
            rec["_id"], rec["_pfad"] = p.stem, p
            out.append(rec)
    out.sort(key=lambda r: str(r.get("published_at", "")), reverse=True)
    return out


def _schreibe(rec: dict) -> None:
    pfad = rec.pop("_pfad")
    rec.pop("_id", None)
    pfad.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")


def archivieren(ausgabe: str, behalte: int, trocken: bool) -> int:
    alle = [r for r in _lade() if _eintrag_ausgabe(r) == ausgabe]
    je_ressort: dict[str, int] = {}
    bleibt, dran, schon = [], [], 0
    for r in alle:
        if r.get("archiviert"):
            schon += 1
            continue
        res = r.get("ressort") or "(ohne Ressort)"
        je_ressort[res] = je_ressort.get(res, 0) + 1
        (bleibt if je_ressort[res] <= behalte else dran).append(r)
    print(f"Ausgabe {ausgabe}: {len(alle)} Beiträge, {schon} schon archiviert, {len(bleibt)} bleiben öffentlich, {len(dran)} werden archiviert")
    for r in bleibt:
        print(f"  bleibt   {r['_id']}  {str(r.get('published_at', ''))[:10]}  {r.get('ressort', ''):26} {r.get('titel', '')[:60]}")
    if trocken:
        print("(Trockenlauf, nichts geändert)")
        return 0
    stempel = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    for r in dran:
        r["archiviert"] = stempel
        _schreibe(r)
    print(f"{len(dran)} Beiträge archiviert ({stempel})")
    return 0


def zurueck(ids: list[str]) -> int:
    n = 0
    for r in _lade():
        if r["_id"] in ids and r.get("archiviert"):
            r.pop("archiviert", None)
            _schreibe(r)
            n += 1
            print(f"wieder öffentlich: {r['_id']}")
    print(f"{n} Beiträge aus dem Archiv geholt")
    return 0


def stand() -> int:
    z: dict[str, list[int]] = {}
    for r in _lade():
        a = z.setdefault(_eintrag_ausgabe(r), [0, 0])
        a[1 if r.get("archiviert") else 0] += 1
    for k, (oeff, arch) in sorted(z.items()):
        print(f"  {k}: {oeff} öffentlich, {arch} archiviert")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Beiträge archivieren: je Ressort die jüngsten N bleiben öffentlich.")
    ap.add_argument("--ausgabe", default="de")
    ap.add_argument("--behalte", type=int, default=3, help="Beiträge je Ressort, die öffentlich bleiben")
    ap.add_argument("--trocken", action="store_true")
    ap.add_argument("--zurueck", nargs="*", metavar="ID", help="diese Beiträge wieder veröffentlichen")
    ap.add_argument("--stand", action="store_true")
    a = ap.parse_args(argv)
    if a.stand:
        return stand()
    if a.zurueck:
        return zurueck(a.zurueck)
    return archivieren(a.ausgabe, a.behalte, a.trocken)


if __name__ == "__main__":
    sys.exit(main())
