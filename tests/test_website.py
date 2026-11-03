"""Öffentliche Seiten und die Blog-API: was ein Besucher und die Kette sehen. Nur GET, keine Änderung."""

def test_startseite(client, html):
    r = client.get("/news/", headers=html)
    assert r.status_code == 200
    assert "<nav>" in r.text and 'class="lead' in r.text
    assert "og:image" in r.text and 'rel="icon"' in r.text


def test_artikel_und_alle(P, client, html):
    ents = P._blog_entries()
    assert ents, "keine Beiträge im Blog-Ordner"
    pid = ents[0]["id"]
    r = client.get(f"/news/{pid}", headers=html)
    assert r.status_code == 200 and ents[0]["titel"][:20] in r.text
    assert "application/ld+json" in r.text
    r = client.get("/news/alle?page=2", headers=html)
    assert r.status_code == 200


def test_ressort_und_dossiers(P, client, html):
    site = P._site()
    slug = P._slug(site["ressorts"][0])
    r = client.get(f"/news/r/{slug}", headers=html)
    assert r.status_code == 200
    r = client.get("/news/dossiers", headers=html)
    assert r.status_code == 200
    r = client.get("/news/r/gibt-es-nicht", headers=html)
    assert r.status_code == 404


def test_suche(client, html):
    r = client.get("/news/suche?q=Harz", headers=html)
    assert r.status_code == 200 and 'class="such"' in r.text
    r = client.get("/api/suche?q=Harz&limit=2")
    assert r.status_code == 200 and "treffer" in r.json()


def test_englische_ausgabe(client, html):
    r = client.get("/news/en/", headers=html)
    assert r.status_code == 200 and 'lang="en"' in r.text
    r = client.get("/news/en/search?q=Ukraine", headers=html)
    assert r.status_code == 200
    r = client.get("/news/en/all", headers=html)
    assert r.status_code == 200


def test_404_als_seite(client, html):
    r = client.get("/news/gibt-es-nicht-xyz", headers=html)
    assert r.status_code == 404 and 'class="such"' in r.text
    r = client.get("/api/gibt-es-nicht")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")


def test_feeds_und_robots(client):
    assert client.get("/news/feed.xml").status_code == 200
    assert client.get("/news/sitemap.xml").status_code == 200
    # robots.txt haengt am Host: auf der eigenen Domain mit Sitemap, auf fremden Hosts gesperrt
    assert "Sitemap" in client.get("/news/robots.txt", headers={"host": "persona-ainews.ch"}).text
    assert "Disallow: /" in client.get("/news/robots.txt").text
    assert client.get("/news/podcast").status_code == 200
    assert client.get("/news/llms.txt").status_code == 200


def test_register_und_kennzeichen(client, html):
    r = client.get("/news/register", headers=html)
    assert r.status_code == 200
    assert client.get("/news/register/schluessel.pem").status_code == 200
    r = client.get("/api/herkunft?limit=1")
    assert r.status_code == 200
    r = client.get("/news/k/XXXXX", headers=html)
    assert r.status_code in (200, 404)


def test_team_und_medien(client, html):
    assert client.get("/news/team", headers=html).status_code == 200
    assert client.get("/news/so-bewerten-wir", headers=html).status_code == 200
    assert client.get("/news/korrekturen", headers=html).status_code == 200
    assert client.get("/news/ueber", headers=html).status_code == 200


def test_video_mit_range(P, client):
    ents = [e for e in P._blog_entries() if e.get("video")]
    assert ents
    r = client.get(f"/news/media/{ents[0]['id']}.mp4", headers={"range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100


def test_api_ohne_anmeldung(client):
    assert client.get("/api/blog/posts").status_code == 401
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/metrics/summary").status_code in (401, 403)
    r = client.get("/api/blog/posts", headers={"authorization": "Bearer falsch"})
    assert r.status_code == 401


def test_plattform_seiten(client, html):
    for pfad in ("/", "/login", "/impressum", "/datenschutz", "/nutzungsbedingungen", "/en"):
        assert client.get(pfad, headers=html).status_code == 200, pfad
    assert client.get("/static/app.html").status_code == 200


def test_marke_aktuell_unter_schwelle(P):
    """Spur Aktuell und jeder Beitrag mit Score unter 60 tragen "Aktuell" statt des Scores (2.11.2026)."""
    ist = P._ist_aktuell
    assert ist({"spur": "aktuell", "konstruktiv": {"score": 80}})
    assert ist({"konstruktiv": {"score": 59}})
    assert ist({"konstruktiv": {"score": 22}})
    assert not ist({"konstruktiv": {"score": 60}})
    assert not ist({"konstruktiv": {"score": 74}})
    assert not ist({})
    assert not ist({"konstruktiv": None})


def test_dolmetscher(client):
    """Seite ohne Anmeldung, API nur mit Zugangscode (2.11.2026)."""
    r = client.get("/dolmetscher")
    assert r.status_code == 200 and "Dolmetscher" in r.text and "noindex" in r.headers.get("x-robots-tag", "")
    r = client.post("/api/dolmetscher/uebersetzen", json={"text": "Guten Tag", "von": "de", "nach": "sk", "code": "falsch"})
    assert r.status_code == 401
    r = client.post("/api/dolmetscher/sprechen", json={"text": "Dobrý deň", "sprache": "sk", "code": "falsch"})
    assert r.status_code == 401
    assert client.get("/api/dolmetscher/status?code=falsch").json()["code_ok"] is False


def test_archiv_nicht_oeffentlich(P, client, html):
    """Archivierte Beiträge (3.11.2026) fehlen in den öffentlichen Listen, stehen aber in der Redaktionsliste;
    ihre Seite antwortet 410 ohne Anmeldung."""
    with P._lang("de"):
        oeffentlich = P._blog_entries("de")
        alle = P._blog_entries("de", archiv=True)
    assert not any(e.get("archiviert") for e in oeffentlich)
    assert len(alle) >= len(oeffentlich)
    archiviert = [e for e in alle if e.get("archiviert")]
    if not archiviert:
        pytest.skip("noch nichts archiviert")
    r = client.get(f"/news/{archiviert[0]['id']}", headers=html)
    assert r.status_code == 410
    assert archiviert[0]["id"] not in client.get("/news/sitemap.xml").text


def test_paket_stellt_alle_namen_bereit(P):
    """Die Werkzeuge daneben greifen über platform_app auf Funktionen zu (admin.py, mail_stats.py)."""
    for name in ("notify_lead", "_ntfy", "DATA", "_blog_entries", "_save_entry", "app", "_t", "require_user"):
        assert hasattr(P, name), name
