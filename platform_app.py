"""Einstieg der Persona-Plattform: uvicorn platform_app:app (persona.service).

Seit dem 31. Oktober 2026 liegt der Code im Paket persona/ (kern, konten, render, redaktion, website).
Diese Datei lädt das Paket und stellt alle Namen weiter unter platform_app bereit, damit die
Werkzeuge daneben (admin.py, mail_stats.py, aufraeumen.py, Skripte) und `kette` unverändert
weiterlaufen: `import platform_app; platform_app._ntfy(...)` funktioniert wie zuvor.
"""

from persona import dolmetscher, kern, konten, redaktion, render, website  # noqa: F401
from persona.kern import app  # noqa: F401

for _m in (kern, konten, render, redaktion, website, dolmetscher):
    globals().update({k: v for k, v in vars(_m).items()
                      if not k.startswith("__") and k not in ("kern", "konten", "render", "redaktion", "website", "dolmetscher")})
del _m
