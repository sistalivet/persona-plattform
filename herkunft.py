"""Herkunftsregister von Persona AI News (Entscheidung Max, 30.10.2026).

Fuer jedes veroeffentlichte Video und jede Karte ein Eintrag: SHA-256, Wahrnehmungs-Hashes (pHash je
Standbild, ueberstehen Rekomprimierung, Zuschnitt und Screenshots), Zeit, Absender, Deklaration
(KI-Avatar, fiktive Figur, Ausgabe), signiert mit dem Ed25519-Schluessel der Redaktion. Dazu
Content Credentials (C2PA) in der Videodatei selbst und taeglich ein Merkle-Anker aller neuen
Eintraege bei OpenTimestamps (Bitcoin). Das Eintragsformat ist chain-neutral: ein weiterer Anker
(z. B. Avalanche) kommt als zusaetzliche Datei in anker/ dazu, kein Eintrag aendert sich.

Dateien (data/herkunft/): register.jsonl (ein Eintrag je Zeile), register_key.pem / register_pub.pem,
c2pa_key.pem / c2pa_cert.pem (openssl, 30.10.2026), anker/<tag>.root + .ots.

Befehle: python herkunft.py verankern | nachtragen | pruefen <datei>
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HIER = Path(__file__).resolve().parent
DATA = HIER / "data"
DIR = DATA / "herkunft"
REGISTER = DIR / "register.jsonl"
KEY = DIR / "register_key.pem"
PUB = DIR / "register_pub.pem"
C2PA_KEY = DIR / "c2pa_key.pem"
C2PA_CERT = DIR / "c2pa_cert.pem"
ANKER = DIR / "anker"
BLOG = DATA / "blog"
CODES = DIR / "codes.json"     # Kennzeichen, vergeben beim Render, vor dem Eintrag (30.10.2026)
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"   # keine 0/O, 1/I/L: lesbar auch vom Handybild
CODE_LAENGE = 5
ABSENDER = "Persona AI News (persona-ainews.ch)"
VERSION = 1
TSA = b"http://timestamp.digicert.com"
FRAMES = 16          # Standbilder je Video fuer die Wahrnehmungs-Hashes
TREFFER_BITS = 12    # Hamming-Distanz, bis zu der ein Standbild als gleich gilt (64-Bit-pHash)
TREFFER_ANTEIL = 0.5 # Anteil der Standbilder der Probe, die ein Gegenstueck brauchen
TREFFER_MITTEL = 0.22  # mittlere Distanz je Standbild als Anteil der 64 Bit, bis zu der eine Datei als gleich gilt
TREFFER_ABSTAND = 0.05 # kleinerer Abstand zum naechsten Beitrag = mehrdeutig


# ------------------------------------------------------------------ Schluessel und Signatur

def _schluessel():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    DIR.mkdir(parents=True, exist_ok=True)
    if KEY.exists():
        return serialization.load_pem_private_key(KEY.read_bytes(), password=None)
    k = ed25519.Ed25519PrivateKey.generate()
    KEY.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    KEY.chmod(0o600)
    PUB.write_bytes(k.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    return k


def oeffentlicher_schluessel() -> str:
    _schluessel()
    return PUB.read_text(encoding="utf-8")


def _kanon(e: dict) -> bytes:
    return json.dumps({k: v for k, v in e.items() if k != "signatur"}, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def signieren(e: dict) -> dict:
    e = dict(e)
    e["signatur"] = base64.b64encode(_schluessel().sign(_kanon(e))).decode()
    return e


def signatur_gueltig(e: dict) -> bool:
    from cryptography.hazmat.primitives import serialization
    try:
        pub = serialization.load_pem_public_key(PUB.read_bytes())
        pub.verify(base64.b64decode(e.get("signatur", "")), _kanon(e))
        return True
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ Hashes

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _dauer(path: Path) -> float:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nk=1:nw=1", str(path)],
                             capture_output=True, text=True, timeout=30)
        return float(out.stdout.strip() or 0)
    except (ValueError, OSError, subprocess.SubprocessError):
        return 0.0


def phashes(path: Path, art: str) -> list[str]:
    """pHash (64 Bit, hex) je Standbild: Bilder ein Wert, Videos bis zu FRAMES gleichmaessig verteilte Standbilder."""
    import imagehash
    from PIL import Image
    if art != "video":
        with Image.open(path) as im:
            return [str(imagehash.phash(im.convert("RGB")))]
    dauer = _dauer(path)
    n = FRAMES if dauer >= FRAMES else max(1, int(dauer) or 1)
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        schritt = max(dauer / n, 0.5)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-vf", f"fps=1/{schritt:.3f},scale=320:-2",
                        "-frames:v", str(n), f"{tmp}/f%03d.jpg"], capture_output=True, timeout=120)
        for f in sorted(Path(tmp).glob("f*.jpg")):
            with Image.open(f) as im:
                out.append(str(imagehash.phash(im.convert("RGB"))))
    return out


def _distanz(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


# ------------------------------------------------------------------ Kennzeichen

def normalisiere_code(s: str) -> str:
    s = "".join(ch for ch in str(s or "").upper() if ch.isalnum())
    return s if len(s) == CODE_LAENGE and all(ch in ALPHABET for ch in s) else ""


def _codes() -> dict:
    try:
        return json.loads(CODES.read_text(encoding="utf-8")) if CODES.exists() else {}
    except ValueError:
        return {}


def _codes_schreiben(d: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    CODES.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def neuer_code(job_id: str, uid: int | None = None) -> str:
    """A fresh five-character code, reserved for this render; it becomes part of the register entry
    when the video is attached to a story."""
    import secrets
    codes = _codes()
    vergeben = set(codes) | {e.get("kennzeichen") for e in alle() if e.get("kennzeichen")}
    while True:
        code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LAENGE))
        if code not in vergeben:
            break
    codes[code] = {"job": job_id, "uid": uid, "zeit": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "beitrag": None}
    _codes_schreiben(codes)
    return code


def code_zuordnen(code: str, pid: str) -> None:
    codes = _codes()
    if code in codes:
        codes[code]["beitrag"] = pid
        _codes_schreiben(codes)


def nach_code(code: str) -> dict:
    """{"status": "eingetragen" | "gerendert" | "unbekannt", "eintrag"?, "reserviert"?}"""
    code = normalisiere_code(code)
    if not code:
        return {"status": "ungueltig"}
    e = next((x for x in alle() if x.get("kennzeichen") == code), None)
    if e:
        return {"status": "eingetragen", "eintrag": e}
    r = _codes().get(code)
    if r:
        return {"status": "gerendert", "reserviert": r}
    return {"status": "unbekannt"}


# ------------------------------------------------------------------ Register

def alle() -> list[dict]:
    if not REGISTER.exists():
        return []
    out = []
    for line in REGISTER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def eintrag(eid: str) -> dict | None:
    return next((e for e in alle() if e.get("id") == eid), None)


def fuer_beitrag(pid: str) -> list[dict]:
    return [e for e in alle() if e.get("beitrag") == pid]


def eintragen(pid: str, datei: Path, art: str, deklaration: dict, c2pa: dict | None = None, url: str = "",
              kennzeichen: str = "") -> dict:
    """Append one signed entry. The same file (SHA-256) is registered once."""
    DIR.mkdir(parents=True, exist_ok=True)
    sha = sha256(datei)
    for e in alle():
        if e.get("sha256") == sha:
            return e
    e = {"version": VERSION, "id": "h_" + sha[:16], "beitrag": pid, "art": art, "datei": datei.name, "url": url,
         "sha256": sha, "phash": phashes(datei, art), "bytes": datei.stat().st_size,
         "zeit": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
         "absender": ABSENDER, "deklaration": deklaration, "c2pa": c2pa or None,
         "kennzeichen": normalisiere_code(kennzeichen) or None}
    e = signieren(e)
    with REGISTER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n")
    return e


def pruefen_datei(path: Path, art: str | None = None) -> dict:
    """Is this file (or a re-encoded copy, a crop, a screenshot of it) in the register?"""
    art = art or ("video" if path.suffix.lower() in (".mp4", ".mov", ".webm", ".mkv", ".m4v") else "bild")
    sha = sha256(path)
    eintraege = alle()
    exakt = next((e for e in eintraege if e.get("sha256") == sha), None)
    if exakt:
        return {"status": "exakt", "eintrag": exakt, "uebereinstimmung": 1.0, "distanz": 0}
    probe = phashes(path, art)
    if not probe:
        return {"status": "unbekannt", "grund": "kein Standbild lesbar"}
    # Rangfolge nach mittlerer Distanz je Standbild (Anteil der Bits): Videos mit demselben Sprecher sehen
    # sich in den Sprecherbildern gleich, nur die mittlere Distanz ueber alle Standbilder trennt sie
    # (30.10.2026, Probe: Kopie neu komprimiert und beschnitten 0,14 zum eigenen, 0,33 zum naechsten Fremden)
    kand = []
    for e in eintraege:
        hashes = e.get("phash") or []
        if not hashes:
            continue
        dists = [min(_distanz(p, h) for h in hashes) for p in probe]
        mittel = sum(dists) / len(dists) / 64.0
        anteil = sum(1 for d in dists if d <= TREFFER_BITS) / len(dists)
        kand.append((mittel, -anteil, e))
    kand.sort(key=lambda k: k[:2])
    if not kand:
        return {"status": "unbekannt", "uebereinstimmung": 0.0, "distanz": None}
    mittel, anteil, e = kand[0][0], -kand[0][1], kand[0][2]
    if mittel <= TREFFER_MITTEL and anteil >= TREFFER_ANTEIL:
        out = {"status": "aehnlich", "eintrag": e, "uebereinstimmung": round(anteil, 2), "distanz": round(mittel, 3)}
        # other stories of the same anchor nearly as close (the anchor's idle frames are identical across
        # videos): the file is ours for sure, the exact story may be ambiguous; name the candidates
        nahe = []
        for k in kand[1:]:
            b = k[2].get("beitrag")
            if k[0] - mittel < TREFFER_ABSTAND and b != e.get("beitrag") and b not in nahe:
                nahe.append(b)
        if nahe:
            out["mehrdeutig"] = nahe[:5]
        return out
    return {"status": "unbekannt", "uebereinstimmung": round(anteil, 2), "distanz": round(mittel, 3)}


# ------------------------------------------------------------------ Content Credentials (C2PA)

def c2pa_signieren(src: Path, dest: Path, titel: str, deklaration: dict) -> dict:
    """Embed a C2PA manifest (created with AI, our declaration) into a copy of the video. Returns what
    to store in the register entry; on any failure the copy is unsigned and the error is recorded."""
    try:
        import c2pa
        manifest = {
            "claim_generator_info": [{"name": "Persona AI News Herkunftsregister", "version": str(VERSION)}],
            "title": titel,
            "assertions": [
                {"label": "c2pa.actions", "data": {"actions": [
                    {"action": "c2pa.created",
                     "digitalSourceType": "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia",
                     "softwareAgent": {"name": "Persona (HeyGen-Fotoavatar, ElevenLabs-Stimme)"}}]}},
                {"label": "ch.persona-ainews.herkunft", "data": deklaration},
            ],
        }
        info = c2pa.C2paSignerInfo(b"es256", C2PA_CERT.read_bytes(), C2PA_KEY.read_bytes(), TSA)
        signer = c2pa.Signer.from_info(info)
        builder = c2pa.Builder.from_json(json.dumps(manifest, ensure_ascii=False))
        builder.sign_file(str(src), str(dest), signer)
        with c2pa.Reader(str(dest)) as r:
            m = r.get_active_manifest() or {}
            label = m.get("label") or ""
            state = r.get_validation_state()
        return {"eingebettet": True, "manifest": label, "pruefstand": state, "aussteller": "eigenes Zertifikat (nicht auf der C2PA-Vertrauensliste)"}
    except Exception as e:  # noqa: BLE001
        if not dest.exists() or dest.stat().st_size == 0:
            shutil.copyfile(src, dest)
        return {"eingebettet": False, "fehler": f"{type(e).__name__}: {str(e)[:160]}"}


def c2pa_lesen(path: Path) -> dict | None:
    try:
        import c2pa
        with c2pa.Reader(str(path)) as r:
            m = r.get_active_manifest() or {}
            return {"label": m.get("label"), "title": m.get("title"), "pruefstand": r.get_validation_state(),
                    "claim_generator": (m.get("claim_generator_info") or [{}])[0].get("name")}
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ Beitrag registrieren

def deklaration_fuer(rec: dict, art: str) -> dict:
    sprecher = rec.get("avatar") or ""
    return {"inhalt": "vollständig KI-erzeugt: Fotoavatar und synthetische Stimme" if art == "video" else "KI-gestützt gestaltete Grafik aus geprüften Zahlen des Beitrags",
            "sprecher": f"{sprecher} (fiktive Figur, KI-Avatar)" if sprecher else "",
            "text": "redaktionell erstellt und geprüft, jede Zahl mit Fundstelle in der Quelle",
            "ausgabe": rec.get("ausgabe") or ("en" if rec.get("sprache") == "en" else "de"),
            "kennzeichnung": "KI-Kennzeichnung und Bauchbinde im Bild" if art == "video" else "Quellenzeile auf der Karte",
            "titel": rec.get("titel", "")[:120]}


def beitrag_registrieren(pid: str, rec: dict, basis_url: str, kennzeichen: str = "") -> dict:
    """Sign the public video with C2PA, then register video and cards. Idempotent per file hash.
    kennzeichen: the code burnt into the video at render time (sidecar), if any."""
    kennzeichen = normalisiere_code(kennzeichen)
    out = {"video": None, "karten": [], "code": kennzeichen or None}
    video = BLOG / f"{pid}.mp4"
    if video.is_file():
        dekl = deklaration_fuer(rec, "video")
        if kennzeichen:
            dekl["kennzeichen"] = kennzeichen
        schon = c2pa_lesen(video)
        if not (schon and schon.get("label")):
            tmp = video.with_suffix(".c2pa.mp4")
            c2 = c2pa_signieren(video, tmp, rec.get("titel", pid), dekl)
            if tmp.exists() and tmp.stat().st_size > 0:
                tmp.replace(video)
        else:
            c2 = {"eingebettet": True, "manifest": schon.get("label"), "pruefstand": schon.get("pruefstand"), "aussteller": "eigenes Zertifikat (nicht auf der C2PA-Vertrauensliste)"}
        e = eintragen(pid, video, "video", dekl, c2, f"{basis_url}/media/{pid}.mp4", kennzeichen)
        out["video"] = e["id"]
        if kennzeichen:
            code_zuordnen(kennzeichen, pid)
        elif e.get("kennzeichen"):
            out["code"] = e["kennzeichen"]
    i = 1
    while (BLOG / f"{pid}_k{i}.png").is_file():
        k = BLOG / f"{pid}_k{i}.png"
        e = eintragen(pid, k, "karte", deklaration_fuer(rec, "karte"), None, f"{basis_url}/media/{pid}_k{i}.png")
        out["karten"].append(e["id"])
        i += 1
    return out


# ------------------------------------------------------------------ Verankerung

def _merkle(leaves: list[str]) -> str:
    if not leaves:
        return ""
    ebene = sorted(leaves)
    while len(ebene) > 1:
        if len(ebene) % 2:
            ebene.append(ebene[-1])
        ebene = [hashlib.sha256((ebene[i] + ebene[i + 1]).encode()).hexdigest() for i in range(0, len(ebene), 2)]
    return ebene[0]


def verankern(tag: str | None = None) -> dict:
    """Merkle root of the day's entries, stamped with OpenTimestamps (ots stamp); pending stamps are upgraded."""
    ANKER.mkdir(parents=True, exist_ok=True)
    tag = tag or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    blaetter = [hashlib.sha256(_kanon(e)).hexdigest() for e in alle() if str(e.get("zeit", "")).startswith(tag)]
    ots = shutil.which("ots") or str(Path(sys.executable).parent / "ots")
    ergebnis = {"tag": tag, "eintraege": len(blaetter), "wurzel": _merkle(blaetter), "gestempelt": False}
    root = ANKER / f"{tag}.root"
    if blaetter and not (ANKER / f"{tag}.root.ots").exists():
        root.write_text(ergebnis["wurzel"] + "\n", encoding="utf-8")
        p = subprocess.run([ots, "stamp", str(root)], capture_output=True, text=True, timeout=180)
        ergebnis["gestempelt"] = (ANKER / f"{tag}.root.ots").exists()
        ergebnis["ots"] = (p.stdout + p.stderr)[-300:]
    elif (ANKER / f"{tag}.root.ots").exists():
        ergebnis["gestempelt"] = True
    # earlier stamps: upgrade when Bitcoin has confirmed (ots upgrade), keep quiet otherwise
    for f in sorted(ANKER.glob("*.root.ots")):
        subprocess.run([ots, "upgrade", str(f)], capture_output=True, text=True, timeout=180)
    return ergebnis


def anker_stand(tag: str) -> str:
    if (ANKER / f"{tag}.root.ots").exists():
        ots = shutil.which("ots") or str(Path(sys.executable).parent / "ots")
        p = subprocess.run([ots, "info", str(ANKER / f"{tag}.root.ots")], capture_output=True, text=True, timeout=60)
        return "bestätigt (Bitcoin)" if "BitcoinBlockHeaderAttestation" in p.stdout else "gestempelt, Bestätigung ausstehend"
    return "noch kein Anker"


# ------------------------------------------------------------------ Kommandozeile

def _nachtragen() -> None:
    """Register every published post that has a video (one-off backfill, 30 October)."""
    n = 0
    for j in sorted(BLOG.glob("p_*.json")):
        pid = j.stem
        if not (BLOG / f"{pid}.mp4").is_file():
            continue
        try:
            rec = json.loads(j.read_text(encoding="utf-8"))
        except ValueError:
            continue
        key = rec.get("ausgabe") or ("en" if rec.get("sprache") == "en" else "de")
        basis = "https://persona-ainews.ch" + ("/en" if key == "en" else "")
        r = beitrag_registrieren(pid, rec, basis)
        if r["video"] and rec.get("register") != r:
            rec["register"] = r   # `herkunft` am Beitrag ist schon die Textherkunft (persona/kette)
            j.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        n += 1
        print(pid, r["video"], len(r["karten"]), "Karten")
    print(n, "Beiträge registriert,", len(alle()), "Einträge im Register")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "verankern":
        print(json.dumps(verankern(sys.argv[2] if len(sys.argv) > 2 else None), ensure_ascii=False, indent=1))
    elif cmd == "nachtragen":
        _nachtragen()
    elif cmd == "code" and len(sys.argv) > 2:
        print(json.dumps(nach_code(sys.argv[2]), ensure_ascii=False, indent=1)[:1500])
    elif cmd == "pruefen" and len(sys.argv) > 2:
        r = pruefen_datei(Path(sys.argv[2]))
        print(json.dumps({k: (v if k != "eintrag" else {x: v[x] for x in ("id", "beitrag", "art", "zeit", "deklaration")}) for k, v in r.items()}, ensure_ascii=False, indent=1))
    else:
        print("python herkunft.py verankern [tag] | nachtragen | pruefen <datei>")
