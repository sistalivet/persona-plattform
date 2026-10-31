"""
HeyGen-Anbindung (v3-API, stdlib): Foto-Avatar aus einem Profilbild + eigene Audiodatei
(ElevenLabs-Stimme aus der Pipeline) -> fertiges Sprechvideo. Key: HEYGEN_API_KEY in
Avatar Twin/avatar_keys.txt.

  Ablauf: Bild einmalig als Asset hochladen (wird je Profil gemerkt) -> Audio als Asset
  hochladen -> POST /v3/videos {type: image, image: {asset_id}, audio_asset_id, aspect_ratio}
  -> GET /v3/videos/{id} bis completed -> video_url herunterladen.
"""
import json
import mimetypes
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HIER = Path(__file__).resolve().parent
_AVATAR_TWIN = HIER.parent / "Avatar Twin"
API = "https://api.heygen.com"


def _key() -> str:
    for line in (_AVATAR_TWIN / "avatar_keys.txt").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("HEYGEN_API_KEY="):
            v = line.split("=", 1)[1].strip()
            if v and "HIER-DEN" not in v:
                return v
    raise RuntimeError("HEYGEN_API_KEY fehlt in avatar_keys.txt")


def _req(method: str, path: str, body: bytes | None = None, headers: dict | None = None, timeout: int = 120) -> dict:
    h = {"x-api-key": _key(), "Accept": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(API + path, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HeyGen {method} {path}: HTTP {e.code} {e.read()[:300].decode('utf-8', 'replace')}")


def upload_asset(path: Path) -> dict:
    """POST /v3/assets (multipart). Liefert {asset_id, url, mime_type, size_bytes}."""
    boundary = "----persona" + uuid.uuid4().hex
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n").encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    d = _req("POST", "/v3/assets", body, {"Content-Type": f"multipart/form-data; boundary={boundary}",
                                           "Idempotency-Key": uuid.uuid4().hex}, timeout=300)
    return d.get("data", d)


def create_image_video(image_asset_id: str, audio_asset_id: str, title: str = "", aspect: str = "9:16") -> str:
    """POST /v3/videos, Typ image (Bild + eigene Audiodatei). Liefert video_id."""
    payload = {"type": "image", "image": {"type": "asset_id", "asset_id": image_asset_id},
               "audio_asset_id": audio_asset_id, "aspect_ratio": aspect, "resolution": "1080p",
               "title": (title or "Persona")[:100]}
    d = _req("POST", "/v3/videos", json.dumps(payload).encode(),
             {"Content-Type": "application/json", "Idempotency-Key": uuid.uuid4().hex})
    return d.get("data", d)["video_id"]


def video_status(video_id: str) -> dict:
    return _req("GET", f"/v3/videos/{video_id}").get("data", {})


def wait_and_download(video_id: str, out: Path, timeout_s: int = 900, log=print) -> Path:
    t0 = time.time()
    while True:
        s = video_status(video_id)
        st = s.get("status")
        if st == "completed" and s.get("video_url"):
            urllib.request.urlretrieve(s["video_url"], out)
            return out
        if st == "failed":
            raise RuntimeError(f"HeyGen: {s.get('failure_code')} {s.get('failure_message')}")
        if time.time() - t0 > timeout_s:
            raise RuntimeError("HeyGen: Zeitüberschreitung beim Rendern")
        log(f"HeyGen: {st} …")
        time.sleep(10)


def speech(text: str, voice_id: str, out: Path, speed: float = 1.0, language: str = "de") -> dict:
    """POST /v3/voices/speech: HeyGen-eigene Sprachsynthese einer Katalogstimme, mit Wortzeiten.
    Schreibt die Tonspur nach `out` (mp3) und die Wortzeiten nach `<out>.align.json` im Format der
    Pipeline ({"words": [{"text", "start", "end"}]}). Ersetzt ElevenLabs seit dem 17.10.2026
    (Entscheidung Max: nur noch HeyGen-Stimmen). Liefert {duration, engine, words}."""
    payload = {"text": text, "voice_id": voice_id, "speed": float(speed), "language": language}
    d = _req("POST", "/v3/voices/speech", json.dumps(payload).encode(),
             {"Content-Type": "application/json"}, timeout=300)
    data = d.get("data", d)
    url = data.get("audio_url")
    if not url:
        raise RuntimeError(f"HeyGen speech ohne audio_url: {str(d)[:300]}")
    urllib.request.urlretrieve(url, out)
    words = [{"text": w["word"], "start": float(w["start"]), "end": float(w["end"])}
             for w in (data.get("word_timestamps") or [])
             if w.get("word") and not str(w["word"]).startswith("<")]
    if words:
        Path(str(out) + ".align.json").write_text(json.dumps({"words": words}, ensure_ascii=False), encoding="utf-8")
    return {"duration": data.get("duration"), "engine": data.get("engine"), "words": len(words)}


def wallet() -> float | None:
    """Remaining HeyGen wallet balance in USD (billing_type wallet), None if unknown."""
    try:
        w = (_req("GET", "/v1/user/me").get("data") or {}).get("wallet") or {}
        v = w.get("remaining_balance")
        return float(v) if v is not None else None
    except Exception:  # noqa: BLE001
        return None


def quota() -> dict:
    """Account and wallet (GET /v3/users/me); the legacy /v2/user/remaining_quota is switched off on 31.10.2026."""
    return _req("GET", "/v3/users/me").get("data", {})


if __name__ == "__main__":
    if len(sys.argv) >= 3:
        img, audio = Path(sys.argv[1]), Path(sys.argv[2])
        a = upload_asset(img)
        print("Bild-Asset:", a["asset_id"])
        b = upload_asset(audio)
        print("Audio-Asset:", b["asset_id"])
        vid = create_image_video(a["asset_id"], b["asset_id"], "Persona Test")
        print("Video:", vid)
        print("fertig:", wait_and_download(vid, Path(sys.argv[3] if len(sys.argv) > 3 else "heygen_test.mp4")))
    else:
        print(json.dumps(quota(), ensure_ascii=False))
