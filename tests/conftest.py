"""Tests der Persona-Plattform laufen gegen die echten Daten des Servers, nur lesend (Bestandsaufnahme 30.10.2026,
Punkt 8). Start aus dem Plattform-Ordner: /opt/persona/venv/bin/python -m pytest -q tests

PERSONA_MODUL=platform_app_neu prüft einen noch nicht umgeschalteten Stand (deploy.sh braucht das nicht,
dort liegt der neue Stand schon unter platform_app.py)."""

import importlib
import os
import sys
from pathlib import Path

import pytest

HIER = Path(__file__).resolve().parent.parent
if str(HIER) not in sys.path:
    sys.path.insert(0, str(HIER))


@pytest.fixture(scope="session")
def P():
    return importlib.import_module(os.environ.get("PERSONA_MODUL", "platform_app"))


@pytest.fixture(scope="session")
def client(P):
    from starlette.testclient import TestClient
    return TestClient(P.app)


@pytest.fixture(scope="session")
def html():
    return {"accept": "text/html"}
