"""Persona-Plattform als Paket (31. Oktober 2026). Reihenfolge des Ladens: kern zuerst, dann die vier
Fachmodule; sie binden Namen der jeweils anderen am Ende ihrer Datei (siehe dort)."""

from . import kern  # noqa: F401
from . import konten  # noqa: F401
from . import render  # noqa: F401
from . import redaktion  # noqa: F401
from . import website  # noqa: F401

app = kern.app
