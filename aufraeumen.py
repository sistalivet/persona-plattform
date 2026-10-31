#!/usr/bin/env python3
"""Aufraeumen auf dem Server (Entscheidung Max, 19.10.2026): regenerierbare und verwaiste Dateien
entfernen, damit die Platte nicht vollaeuft. Loescht nie Veroeffentlichtes (data/blog), keine
Profile, keine Datenbanken, keine Backups der letzten Tage.

  python aufraeumen.py               Probelauf: zeigt, was weg koennte, loescht nichts
  python aufraeumen.py --los         loescht (Timer persona-aufraeumen, sonntags 02:30 UTC)
  python aufraeumen.py --los --docker   zusaetzlich den Docker-Build-Cache leeren (Image und Container bleiben)

Was weg kann:
- Render-Zwischenstaende in data/users/*/outputs: Ordner _hg_* und Dateien _gp_*.json, aelter als 2 Tage
  (platform_app raeumt sie nach dem Render selbst weg; Reste stammen von abgebrochenen Renders)
- fertige Renders der Kette (Konto kette) aelter als 30 Tage: die veroeffentlichte Kopie liegt in data/blog
- Renders anderer Konten aelter als 180 Tage
- Profil-Sicherungen data/profiles_backup_*.tar.gz aelter als 30 Tage, die juengste bleibt
- Statistik-Screenshots data/stats_inbox aelter als 180 Tage
- systemd-Journal ueber 100 MB, apt-Cache
"""
import datetime as dt
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HIER = Path(__file__).resolve().parent
DATA = HIER / "data"
USERS = DATA / "users"
TAG = 86400
KETTE_UID = "12"           # Konto der Kette (Rolle editor)
KETTE_TAGE, ANDERE_TAGE, ZWISCHEN_TAGE, PROFIL_BACKUP_TAGE, STATS_TAGE = 30, 180, 2, 30, 180
JOURNAL_MAX = "100M"


def _groesse(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _alter_tage(p: Path) -> float:
    return (time.time() - p.stat().st_mtime) / TAG


def kandidaten() -> list[tuple[str, Path, int]]:
    """(Grund, Pfad, Bytes)"""
    out = []
    if USERS.is_dir():
        for udir in USERS.iterdir():
            outputs = udir / "outputs"
            if not outputs.is_dir():
                continue
            for p in outputs.iterdir():
                name = p.name
                if (name.startswith("_hg_") and p.is_dir()) or (name.startswith("_gp_") and name.endswith(".json")):
                    if _alter_tage(p) > ZWISCHEN_TAGE:
                        out.append(("Render-Zwischenstand", p, _groesse(p)))
                elif p.is_file() and p.suffix.lower() in (".mp4", ".mov", ".webm", ".wav", ".mp3"):
                    grenze = KETTE_TAGE if udir.name == KETTE_UID else ANDERE_TAGE
                    if _alter_tage(p) > grenze:
                        out.append((f"Render aelter als {grenze} Tage", p, _groesse(p)))
                        for seite in (p.with_suffix(".json"), p.with_suffix(".jpg"), Path(str(p) + ".align.json")):
                            if seite.is_file():
                                out.append(("Begleitdatei", seite, _groesse(seite)))
    backups = sorted(DATA.glob("profiles_backup_*.tar.gz"), key=lambda f: f.stat().st_mtime)
    for b in backups[:-1]:
        if _alter_tage(b) > PROFIL_BACKUP_TAGE:
            out.append(("alte Profil-Sicherung", b, _groesse(b)))
    inbox = DATA / "stats_inbox"
    if inbox.is_dir():
        for p in inbox.rglob("*"):
            if p.is_file() and _alter_tage(p) > STATS_TAGE:
                out.append(("alter Statistik-Screenshot", p, _groesse(p)))
    return out


def _run(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        return (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"{cmd[0]}: {e}"


def system(los: bool, docker: bool) -> list[str]:
    zeilen = []
    j = _run(["journalctl", "--disk-usage"])
    zeilen.append("Journal: " + j.split("take up")[-1].strip().split(" ")[0] if "take up" in j else "Journal: ?")
    if los:
        _run(["journalctl", f"--vacuum-size={JOURNAL_MAX}"])
        _run(["apt-get", "clean"])
        zeilen.append(f"Journal auf {JOURNAL_MAX} gekuerzt, apt-Cache geleert")
    if docker:
        du = _run(["docker", "buildx", "du"])
        rec = next((l.split(":", 1)[1].strip() for l in du.splitlines() if l.startswith("Reclaimable")), "?")
        zeilen.append(f"Docker-Build-Cache freigebbar: {rec}")
        if los:
            out = _run(["docker", "builder", "prune", "-f"])
            zeilen.append("Docker: " + (out.splitlines()[-1] if out else "geleert"))
    return zeilen


def main() -> None:
    los = "--los" in sys.argv
    docker = "--docker" in sys.argv
    k = kandidaten()
    gesamt = sum(b for _, _, b in k)
    frei_vorher = shutil.disk_usage("/").free
    print(f"{'Loesche' if los else 'Probelauf'}: {len(k)} Eintraege, {gesamt / 1e6:.0f} MB")
    gruende: dict[str, tuple[int, int]] = {}
    for grund, p, b in k:
        n, s = gruende.get(grund, (0, 0))
        gruende[grund] = (n + 1, s + b)
        if los:
            try:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
            except OSError as e:
                print(f"  nicht geloescht: {p} ({e})")
    for grund, (n, s) in sorted(gruende.items(), key=lambda x: -x[1][1]):
        print(f"  {grund}: {n} ({s / 1e6:.0f} MB)")
    for z in system(los, docker):
        print("  " + z)
    frei = shutil.disk_usage("/")
    print(f"Platte: {100 * frei.used / frei.total:.0f} % belegt, {frei.free / 1e9:.1f} GB frei"
          + (f", {(frei.free - frei_vorher) / 1e9:.1f} GB gewonnen" if los else ""))
    (DATA / "aufraeumen.json").write_text(json.dumps({"zeit": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                                       "los": los, "eintraege": len(k), "mb": round(gesamt / 1e6),
                                                       "frei_gb": round(frei.free / 1e9, 1)}), encoding="utf-8")


if __name__ == "__main__":
    main()
