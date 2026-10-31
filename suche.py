"""Suchindex der Beitraege (Bestandsaufnahme 30.10.2026, Punkt 7).

Die JSON-Dateien in data/blog bleiben die Wahrheit. Hier liegt nur ein Abbild in SQLite (FTS5):
je Beitrag Titel, Teaser, Text, Ressort, Narrativ und Ausgabe, dazu die Aenderungszeit der Datei.
`aktualisieren()` bekommt die Beitraege, die die Plattform gerade gelesen hat, und schreibt nur,
was sich geaendert hat; `suchen()` liefert die Kennungen nach Relevanz (bm25) mit einem Ausschnitt.
Faellt die Datenbank weg, baut der naechste Aufruf sie neu auf.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path

_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS stand (id TEXT PRIMARY KEY, mtime INTEGER NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS beitraege USING fts5(
    id UNINDEXED, ausgabe UNINDEXED, titel, teaser, text, ressort, narrativ,
    tokenize='unicode61 remove_diacritics 2');
"""


class Index:
    def __init__(self, pfad: Path):
        self.pfad = Path(pfad)
        self.con: sqlite3.Connection | None = None

    def _verbinden(self) -> sqlite3.Connection:
        if self.con is None:
            self.pfad.parent.mkdir(parents=True, exist_ok=True)
            self.con = sqlite3.connect(self.pfad, check_same_thread=False)
            self.con.executescript(SCHEMA)
        return self.con

    def aktualisieren(self, zeilen: list[tuple[str, int, dict]]) -> int:
        """zeilen: (id, mtime_ns, Beitrag). Schreibt neue und geaenderte Beitraege, loescht verschwundene.
        Liefert die Zahl der geschriebenen Beitraege."""
        with _LOCK:
            con = self._verbinden()
            alt = dict(con.execute("SELECT id, mtime FROM stand").fetchall())
            neu = {pid: mt for pid, mt, _ in zeilen}
            weg = [pid for pid in alt if pid not in neu]
            dran = [(pid, mt, rec) for pid, mt, rec in zeilen if alt.get(pid) != mt]
            if not weg and not dran:
                return 0
            with con:
                for pid in weg:
                    con.execute("DELETE FROM beitraege WHERE id=?", (pid,))
                    con.execute("DELETE FROM stand WHERE id=?", (pid,))
                for pid, mt, rec in dran:
                    con.execute("DELETE FROM beitraege WHERE id=?", (pid,))
                    con.execute("INSERT INTO beitraege (id, ausgabe, titel, teaser, text, ressort, narrativ) VALUES (?,?,?,?,?,?,?)",
                                (pid, str(rec.get("_ausgabe") or ""), str(rec.get("titel") or ""), str(rec.get("teaser") or ""),
                                 str(rec.get("text") or rec.get("skript") or ""), str(rec.get("ressort") or ""),
                                 str(rec.get("narrativ") or "")))
                    con.execute("INSERT OR REPLACE INTO stand (id, mtime) VALUES (?,?)", (pid, mt))
            return len(dran)

    @staticmethod
    def anfrage(q: str) -> str:
        """Freitext zu einer FTS5-Anfrage: jedes Wort als Praefix, alles verknuepft mit UND;
        ein Ausdruck in Anfuehrungszeichen bleibt eine Wortfolge."""
        teile = []
        for m in re.finditer(r'"([^"]+)"|(\S+)', q or ""):
            if m.group(1):
                w = re.sub(r'[^\w\s]', " ", m.group(1)).strip()
                if w:
                    teile.append('"' + w + '"')
            else:
                w = re.sub(r"[^\w]", "", m.group(2))
                if w:
                    teile.append('"' + w + '"*')
        return " AND ".join(teile[:12])

    def suchen(self, q: str, ausgabe: str, limit: int = 100) -> list[tuple[str, str]]:
        """[(id, Ausschnitt mit <b>…</b>)] nach Relevanz; Titel wiegt am meisten, dann Teaser, dann Text."""
        anfrage = self.anfrage(q)
        if not anfrage:
            return []
        with _LOCK:
            con = self._verbinden()
            try:
                rows = con.execute(
                    "SELECT id, snippet(beitraege, 4, '<b>', '</b>', '…', 22) FROM beitraege "
                    "WHERE beitraege MATCH ? AND ausgabe = ? "
                    "ORDER BY bm25(beitraege, 0, 0, 12.0, 5.0, 1.0, 3.0, 3.0) LIMIT ?",
                    (anfrage, ausgabe, limit)).fetchall()
            except sqlite3.OperationalError:
                return []
        return [(r[0], r[1]) for r in rows]

    def anzahl(self) -> int:
        with _LOCK:
            return self._verbinden().execute("SELECT count(*) FROM stand").fetchone()[0]
