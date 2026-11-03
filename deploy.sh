#!/bin/bash
# deploy.sh: neuen Stand der Persona-Plattform auf den Server bringen (Bestandsaufnahme 30.10.2026, Punkt 8).
#
#   ./deploy.sh            holt origin/main, prüft, schaltet um, startet neu
#   ./deploy.sh --lokal    prüft und startet den Stand, der schon im Ordner liegt (ohne Git-Abruf)
#
# Ablauf: Abruf, Tests gegen den neuen Code (nur lesend), kein Umschalten während ein Render läuft,
# Neustart, Probe auf 127.0.0.1:8020. Scheitert ein Schritt, kommt der vorige Stand zurück und der
# Dienst läuft weiter wie vorher. Jeder Schritt darf wiederholt werden (idempotent).
set -u -o pipefail   # pipefail (3.11.2026): ein Testfehler vor dem tail darf nicht als Erfolg gelten
cd "/opt/persona/Twin Platform" || exit 1
PY=/opt/persona/venv/bin/python
ALT=$(git rev-parse HEAD 2>/dev/null || echo "")

zurueck() {
    [ -n "$ALT" ] && git reset -q --hard "$ALT" && echo "zurück auf $ALT"
}

if [ "${1:-}" != "--lokal" ]; then
    if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
        echo "!! Arbeitskopie hat lokale Änderungen; erst einchecken oder --lokal nutzen"; exit 2
    fi
    git fetch -q origin || { echo "!! Abruf von origin fehlgeschlagen"; exit 2; }
    git reset -q --hard origin/main || exit 2
    echo "Stand: $(git log --oneline -1)"
fi

P1="twin_pipe"; P1="${P1}line.py"
if pgrep -f "$P1" >/dev/null; then
    echo "!! Ein Render läuft; nicht umgeschaltet. Später noch einmal: ./deploy.sh"
    [ "${1:-}" != "--lokal" ] && zurueck
    exit 5
fi

if ! $PY -c "import platform_app" 2>&1 | tail -3; then
    echo "!! Der neue Stand lädt nicht"; [ "${1:-}" != "--lokal" ] && zurueck; exit 3
fi
if ! $PY -m pytest -q tests -p no:cacheprovider 2>&1 | tail -15; then
    echo "!! Tests gescheitert; nicht umgeschaltet"; [ "${1:-}" != "--lokal" ] && zurueck; exit 3
fi

systemctl restart persona
sleep 4
if ! systemctl is-active --quiet persona || [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8020/news/)" != "200" ]; then
    echo "!! Dienst antwortet nicht; zurück auf den vorigen Stand"
    [ "${1:-}" != "--lokal" ] && zurueck
    systemctl restart persona; sleep 3; systemctl is-active persona
    exit 4
fi
echo "OK: persona läuft mit $(git log --oneline -1 2>/dev/null)"
