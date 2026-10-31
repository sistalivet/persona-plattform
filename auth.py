"""
Accounts + Sessions fuer die Twin-Plattform. Reine Standardbibliothek:
- Nutzer in SQLite, Passwoerter mit PBKDF2-HMAC-SHA256 (Salt pro Nutzer).
- Sessions als signiertes Cookie (HMAC, stateless -> ueberlebt Neustart).
"""
import base64
import hashlib
import hmac
import secrets
import sqlite3
import time
from pathlib import Path

HIER = Path(__file__).resolve().parent
DATA = HIER / "data"
DB = DATA / "platform.db"
SECRET_FILE = DATA / "secret.key"
PBKDF2_ROUNDS = 200_000


def _secret() -> bytes:
    DATA.mkdir(parents=True, exist_ok=True)
    if not SECRET_FILE.exists():
        SECRET_FILE.write_bytes(secrets.token_bytes(32))
    return SECRET_FILE.read_bytes()


def _db():
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute(
        "CREATE TABLE IF NOT EXISTS users ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, "
        "pw_salt BLOB NOT NULL, pw_hash BLOB NOT NULL, role TEXT DEFAULT 'user', "
        "created TEXT DEFAULT (datetime('now')))")
    # Leads von der oeffentlichen Landingpage (Probe / Pilot / Warteliste)
    con.execute(
        "CREATE TABLE IF NOT EXISTS leads ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT DEFAULT (datetime('now')), "
        "name TEXT NOT NULL, email TEXT NOT NULL, role TEXT DEFAULT '', interest TEXT DEFAULT '', "
        "message TEXT DEFAULT '', ip TEXT DEFAULT '', status TEXT DEFAULT 'new')")
    # Einwilligungen der abgebildeten Personen (Gesicht + Stimme), Nachweis nach DSGVO/revDSG
    con.execute(
        "CREATE TABLE IF NOT EXISTS consents ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT DEFAULT (datetime('now')), "
        "name TEXT NOT NULL, email TEXT NOT NULL, lang TEXT DEFAULT 'de', "
        "client TEXT DEFAULT '', scope TEXT DEFAULT '', checks TEXT DEFAULT '{}', "
        "version TEXT DEFAULT '', text_hash TEXT DEFAULT '', ip TEXT DEFAULT '', "
        "ua TEXT DEFAULT '', status TEXT DEFAULT 'active', revoked TEXT DEFAULT '')")
    return con


def _hash(pw: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, PBKDF2_ROUNDS)


# ---- Nutzerverwaltung ----
def create_user(username: str, password: str, role: str = "user") -> int:
    username = username.lower().strip()
    if not username or not password:
        raise ValueError("Nutzername und Passwort erforderlich.")
    salt = secrets.token_bytes(16)
    con = _db()
    try:
        cur = con.execute(
            "INSERT INTO users (username, pw_salt, pw_hash, role) VALUES (?,?,?,?)",
            (username, salt, _hash(password, salt), role))
        con.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        raise ValueError(f"Nutzer '{username}' existiert bereits.")
    finally:
        con.close()


def verify(username: str, password: str):
    con = _db()
    row = con.execute(
        "SELECT id, username, pw_salt, pw_hash, role FROM users WHERE username=?",
        (username.lower().strip(),)).fetchone()
    con.close()
    if not row:
        return None
    uid, uname, salt, h, role = row
    if hmac.compare_digest(_hash(password, salt), h):
        return {"id": uid, "username": uname, "role": role}
    return None


def get_user(uid: int):
    con = _db()
    row = con.execute("SELECT id, username, role FROM users WHERE id=?", (uid,)).fetchone()
    con.close()
    return {"id": row[0], "username": row[1], "role": row[2]} if row else None


def list_users():
    con = _db()
    rows = con.execute("SELECT id, username, role, created FROM users ORDER BY id").fetchall()
    con.close()
    return [{"id": r[0], "username": r[1], "role": r[2], "created": r[3]} for r in rows]


def set_password(username: str, password: str):
    salt = secrets.token_bytes(16)
    con = _db()
    con.execute("UPDATE users SET pw_salt=?, pw_hash=? WHERE username=?",
                (salt, _hash(password, salt), username.lower().strip()))
    con.commit()
    con.close()


# ---- Leads (Landingpage) ----
def add_lead(name: str, email: str, role: str = "", interest: str = "",
             message: str = "", ip: str = "") -> int:
    con = _db()
    cur = con.execute(
        "INSERT INTO leads (name, email, role, interest, message, ip) VALUES (?,?,?,?,?,?)",
        (name.strip(), email.strip().lower(), role.strip(), interest.strip(),
         message.strip(), ip))
    con.commit()
    lid = cur.lastrowid
    con.close()
    return lid


def list_leads(limit: int = 500):
    con = _db()
    rows = con.execute(
        "SELECT id, created, name, email, role, interest, message, ip, status "
        "FROM leads ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    con.close()
    keys = ("id", "created", "name", "email", "role", "interest", "message", "ip", "status")
    return [dict(zip(keys, r)) for r in rows]


def set_lead_status(lead_id: int, status: str):
    con = _db()
    con.execute("UPDATE leads SET status=? WHERE id=?", (status.strip(), lead_id))
    con.commit()
    con.close()


# ---- Einwilligungen (Consent) ----
def add_consent(name: str, email: str, lang: str, client: str, scope: str, checks: dict,
                version: str, text_hash: str, ip: str, ua: str) -> int:
    import json as _json
    con = _db()
    cur = con.execute(
        "INSERT INTO consents (name, email, lang, client, scope, checks, version, text_hash, ip, ua) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (name.strip(), email.strip().lower(), lang, client.strip(), scope,
         _json.dumps(checks, ensure_ascii=False), version, text_hash, ip, ua[:300]))
    con.commit()
    cid = cur.lastrowid
    con.close()
    return cid


def list_consents(limit: int = 500):
    con = _db()
    rows = con.execute(
        "SELECT id, created, name, email, lang, client, scope, checks, version, status, revoked, ip "
        "FROM consents ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    con.close()
    keys = ("id", "created", "name", "email", "lang", "client", "scope", "checks", "version",
            "status", "revoked", "ip")
    return [dict(zip(keys, r)) for r in rows]


def revoke_consent(consent_id: int):
    con = _db()
    con.execute("UPDATE consents SET status='revoked', revoked=datetime('now') WHERE id=?",
                (consent_id,))
    con.commit()
    con.close()


# ---- Session-Cookie (signiert) ----
def make_token(uid: int, days: int = 30) -> str:
    body = f"{uid}:{int(time.time()) + days * 86400}".encode()
    sig = hmac.new(_secret(), body, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(body + b"." + sig).decode()


def read_token(token: str):
    try:
        raw = base64.urlsafe_b64decode(token.encode())
        # Signatur = die letzten 32 Bytes (SHA-256); davor der Punkt, davor der Inhalt. Ein rsplit am
        # letzten Punkt trennte falsch, sobald die Signatur selbst ein Punkt-Byte enthielt (jede achte
        # Anmeldung ergab dann 401 trotz gueltigem Login; Fund 14.10.2026).
        body, sig = raw[:-33], raw[-32:]
        if not hmac.compare_digest(hmac.new(_secret(), body, hashlib.sha256).digest(), sig):
            return None
        uid_s, exp_s = body.decode().split(":")
        if int(exp_s) < time.time():
            return None
        return int(uid_s)
    except Exception:  # noqa: BLE001
        return None
