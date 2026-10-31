# Persona-Plattform

Backend und Website von persona-ainews.ch (FastAPI). Läuft auf dem Server als `persona.service`
(uvicorn, 127.0.0.1:8020, hinter Caddy). Die Redaktionskette (`kette`) spricht die API an.

## Aufbau

| Datei oder Ordner | Inhalt |
|---|---|
| `platform_app.py` | Einstieg für uvicorn; lädt das Paket und stellt alle Namen bereit (Werkzeuge importieren weiter `platform_app`) |
| `persona/kern.py` | App-Objekt, Pfade, Nutzerprüfung, Ausgaben (Mandanten), Beitragsspeicher, Suchindex |
| `persona/konten.py` | Login, Sitzung, Rollen, Nutzerverwaltung, Einwilligung, Leads |
| `persona/render.py` | Stimmen, Hintergründe, Skript, Videoerzeugung, Clips, Framing, Branding |
| `persona/redaktion.py` | Kennzahlen, Blog-API der Kette, Profile, Ticker, Score, Medien-Check, Statistik, Ausgaben-Register, Newsroom |
| `persona/website.py` | Alle Seiten unter `/news` und die statischen Seiten |
| `auth.py`, `herkunft.py`, `heygen.py`, `suche.py`, `newsletter.py`, `konstruktiv.py`, `grafiken.py`, … | Eigenständige Module und Werkzeuge |
| `static/` | HTML der Plattform (Login, Redaktions-App, Landingpages) |
| `tests/` | Lesende Tests gegen die echten Daten; laufen vor jedem Umschalten |
| `deploy.sh` | Abruf aus Git, Tests, Neustart, Rückfall |
| `data/` | Beiträge, Nutzer, Register, Datenbanken; nicht im Repo |

Die Module dürfen sich gegenseitig nutzen. Namen, die nur in Funktionskörpern gebraucht werden, binden sie
am Ende ihrer Datei (späte Bindung), damit beim Laden keine Kreisabhängigkeit entsteht; `kern` hängt von
keinem anderen Modul ab. Laden in der Reihenfolge aus `persona/__init__.py`.

## Ausspielen

```bash
ssh root@116.203.122.171 '/opt/persona/Twin\ Platform/deploy.sh'
```

`deploy.sh` holt `origin/main`, lädt den Code einmal, lässt die Tests laufen, schaltet nicht um, solange ein
Render läuft, startet den Dienst neu und prüft die Startseite. Scheitert ein Schritt, kommt der vorige Stand
zurück. `deploy.sh --lokal` prüft und startet den Stand, der schon im Ordner liegt.

Vor einem Neustart nie `systemctl restart persona` von Hand während ein Render läuft; das Skript prüft das.

## Tests

```bash
cd "/opt/persona/Twin Platform" && /opt/persona/venv/bin/python -m pytest -q tests
```
