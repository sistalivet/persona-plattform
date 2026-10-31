#!/usr/bin/env python3
"""
Admin-CLI der Twin-Plattform (fuer die Beta: manuelles Onboarding).

  python admin.py add-user <name> <passwort> [--role user|admin]
  python admin.py list-users
  python admin.py set-password <name> <passwort>
  python admin.py add-avatar <name> "<video>" [--label "Anzeigename"]

add-avatar: kopiert das Video in den Ordner DIESES Nutzers, klont die Stimme
(ueber Max' geteilten ElevenLabs-Key) und traegt sie ins Voice-Mapping des
Nutzers ein. Daten sind pro Nutzer isoliert unter data/users/<id>/.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import auth

HIER = Path(__file__).resolve().parent
DATA = HIER / "data"
_BASE = Path(os.environ.get("PERSONA_BASE", r"C:/Users/maxst/Desktop"))
AVATAR_TWIN = Path(os.environ.get("PERSONA_TOOLS", str(_BASE / "Avatar Twin")))
_VENV_SCRIPTS = _BASE / "share-bundle-2.1" / "venv" / "Scripts"
if _VENV_SCRIPTS.is_dir():
    os.environ["PATH"] = str(_VENV_SCRIPTS) + os.pathsep + os.environ.get("PATH", "")
sys.path.insert(0, str(AVATAR_TWIN))
import elevenlabs_voice as ev  # noqa: E402  (nutzt Avatar Twin/avatar_keys.txt = geteilter Key)

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}


def user_dir(uid: int) -> Path:
    return DATA / "users" / str(uid)


def _uid(username: str) -> int:
    for u in auth.list_users():
        if u["username"] == username.lower().strip():
            return u["id"]
    sys.exit(f"[FEHLER] Nutzer '{username}' nicht gefunden.")


def add_avatar(username: str, video: str, label: str | None):
    uid = _uid(username)
    src = Path(video)
    if not src.exists():
        sys.exit(f"[FEHLER] Video nicht gefunden: {src}")
    if src.suffix.lower() not in VIDEO_EXT:
        sys.exit(f"[FEHLER] Kein Videoformat: {src.suffix}")
    name = (label or src.stem).strip()
    presets = user_dir(uid) / "presets"
    presets.mkdir(parents=True, exist_ok=True)
    dst = presets / (name + src.suffix.lower())
    shutil.copy2(src, dst)
    print(f"[1/2] Video kopiert -> {dst.name}")

    tmp = presets / "_clone.mp3"
    p = subprocess.run(["ffmpeg", "-y", "-i", str(dst), "-vn", "-ac", "1", "-ar", "44100",
                        str(tmp)], capture_output=True, text=True)
    if p.returncode != 0 or not tmp.exists():
        sys.exit(f"[FEHLER] Audio-Extraktion: {p.stderr[-300:]}")
    print("[2/2] Stimme klonen (ElevenLabs) …")
    try:
        voice_id = ev.clone_voice(str(tmp), f"{username}:{name}")
    except RuntimeError as e:
        tmp.unlink(missing_ok=True)
        sys.exit(f"[FEHLER] {e}")
    tmp.unlink(missing_ok=True)

    vf = presets / "voices.json"
    voices = json.loads(vf.read_text(encoding="utf-8")) if vf.exists() else {}
    voices[name] = voice_id
    vf.write_text(json.dumps(voices, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Fertig: Avatar '{name}' fuer Nutzer '{username}' (voice_id {voice_id}).")


def main():
    ap = argparse.ArgumentParser(description="Twin-Plattform Admin")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add-user"); a.add_argument("name"); a.add_argument("passwort")
    a.add_argument("--role", default="user", choices=["user", "admin"])
    sub.add_parser("list-users")
    s = sub.add_parser("set-password"); s.add_argument("name"); s.add_argument("passwort")
    av = sub.add_parser("add-avatar"); av.add_argument("name"); av.add_argument("video")
    av.add_argument("--label", default=None)
    sub.add_parser("leads", help="Anfragen von der Landingpage anzeigen")
    ls = sub.add_parser("lead-status", help="Status einer Anfrage setzen (new/contacted/won/lost)")
    ls.add_argument("id", type=int); ls.add_argument("status")
    sub.add_parser("test-ntfy", help="Test-Push an das ntfy-Topic aus notify.json senden")
    sub.add_parser("consents", help="Einwilligungen (Gesicht/Stimme) anzeigen")
    cr = sub.add_parser("consent-revoke", help="Einwilligung als widerrufen markieren")
    cr.add_argument("id", type=int)
    args = ap.parse_args()

    if args.cmd == "add-user":
        try:
            uid = auth.create_user(args.name, args.passwort, args.role)
            print(f"Nutzer '{args.name.lower()}' angelegt (id {uid}, Rolle {args.role}).")
        except ValueError as e:
            sys.exit(f"[FEHLER] {e}")
    elif args.cmd == "list-users":
        for u in auth.list_users():
            print(f"  {u['id']:3} {u['username']:20} {u['role']:6} {u['created']}")
    elif args.cmd == "set-password":
        auth.set_password(args.name, args.passwort)
        print(f"Passwort fuer '{args.name}' gesetzt.")
    elif args.cmd == "add-avatar":
        add_avatar(args.name, args.video, args.label)
    elif args.cmd == "leads":
        rows = auth.list_leads()
        if not rows:
            print("Keine Anfragen.")
        for r in rows:
            print(f"  #{r['id']:<4} {r['created']:19} [{r['status']:9}] {r['interest']:10} "
                  f"{r['name']} <{r['email']}> {r['role']}")
            if r["message"]:
                print(f"        {r['message'][:200]}")
    elif args.cmd == "lead-status":
        auth.set_lead_status(args.id, args.status)
        print(f"Anfrage #{args.id} -> {args.status}")
    elif args.cmd == "test-ntfy":
        import platform_app
        ok = platform_app.notify_lead(0, "Test Person", "test@example.com", "berater", "probe",
                                      "Das ist ein Test-Push von Persona (admin.py test-ntfy).")
        print("Push gesendet." if ok else "[FEHLER] Kein Push (notify.json / Netz pruefen).")
    elif args.cmd == "consents":
        rows = auth.list_consents()
        if not rows:
            print("Keine Einwilligungen.")
        for r in rows:
            print(f"  #{r['id']:<4} {r['created']:19} [{r['status']:8}] {r['lang']} v{r['version']} "
                  f"{r['name']} <{r['email']}> Auftraggeber: {r['client'] or '-'} | {r['scope']}")
    elif args.cmd == "consent-revoke":
        auth.revoke_consent(args.id)
        print(f"Einwilligung #{args.id} als widerrufen markiert. Jetzt Avatar/Stimme/Videos loeschen!")


if __name__ == "__main__":
    main()
