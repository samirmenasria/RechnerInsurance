"""Offertanfrage: serverseitige Nachberechnung, Speicherung und E-Mail-Versand."""
import dataclasses
import email
import json
import smtplib
import sqlite3
from email import policy

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


class FakeSMTP:
    gesendet: list = []
    fehler: Exception | None = None

    def __init__(self, host, port, timeout=None, **kw):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, pw):
        self.user = user

    def send_message(self, msg):
        if FakeSMTP.fehler:
            raise FakeSMTP.fehler
        FakeSMTP.gesendet.append(msg)


@pytest.fixture()
def smtp(monkeypatch):
    FakeSMTP.gesendet, FakeSMTP.fehler = [], None
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


@pytest.fixture()
def smtp_client(settings, smtp):
    s = dataclasses.replace(settings, smtp_host="smtp.example.ch", smtp_user="u", smtp_password="p")
    with TestClient(create_app(s)) as c:
        c.settings = s
        yield c


def _angebot(client, rang=3):
    res = client.get("/api/praemien", params=dict(bfs=2196, geburtsjahr=1985, unfall=True, franchise="FRA-300",
                                                  aktueller_versicherer=1562)).json()
    return res["angebote"][rang - 1], res


def _body(tarif_id, **kw):
    b = {
        "bfs_nr": 2196, "geburtsjahr": 1985, "mit_unfall": True, "franchise": "FRA-300", "modell": None,
        "kinderrabatt": False, "tarif_id": tarif_id, "aktueller_versicherer": 1562, "einwilligung": True,
        "kontakt": {"anrede": "Frau", "vorname": "Anna", "nachname": "Muster", "email": "anna.muster@example.ch",
                    "telefon": "079 123 45 67", "strasse": "Bahnhofstrasse 1", "bemerkung": "Bitte abends anrufen"},
    }
    b.update(kw)
    return b


def test_lead_wird_an_empfaenger_gesendet(smtp_client, smtp):
    angebot, res = _angebot(smtp_client)
    r = smtp_client.post("/api/anfrage", json=_body(angebot["tarif_id"]))
    assert r.status_code == 201, r.text
    assert r.json()["zustellung"] == "gesendet"
    assert len(smtp.gesendet) == 1
    msg = email.message_from_bytes(bytes(smtp.gesendet[0]), policy=policy.default)
    assert msg["To"] == "malik.gobbi@dl-finance.ch"
    assert "anna.muster@example.ch" in msg["Reply-To"]
    text = msg.get_body(("plain",)).get_content()
    html = msg.get_body(("html",)).get_content()
    for inhalt in ("Anna Muster", "079 123 45 67", "Bahnhofstrasse 1", "Fribourg (BFS 2196)", "FR Region 1",
                   angebot["versicherer"], angebot["tarifbezeichnung"], f"{angebot['monat']:.2f}",
                   "Aktueller Versicherer", "Bitte abends anrufen", "Franchise"):
        assert inhalt in text, inhalt
    assert angebot["tarifbezeichnung"] in html

    con = sqlite3.connect(smtp_client.settings.leads_db)
    row = con.execute("SELECT status, empfaenger, tarif_id, monat, daten FROM lead").fetchone()
    assert row[:4] == ("gesendet", "malik.gobbi@dl-finance.ch", angebot["tarif_id"], angebot["monat"])
    assert json.loads(row[4])["kontakt"]["email"] == "anna.muster@example.ch"


def test_praemie_wird_serverseitig_berechnet(smtp_client, smtp):
    angebot, _ = _angebot(smtp_client)
    body = _body(angebot["tarif_id"])
    body["monat"] = 1.00  # manipulierter Wert aus dem Browser wird ignoriert
    assert smtp_client.post("/api/anfrage", json=body).status_code == 201
    text = email.message_from_bytes(bytes(smtp.gesendet[0]), policy=policy.default).get_body(("plain",)).get_content()
    assert f"{angebot['monat']:.2f}" in text and "CHF 1.00" not in text


def test_ohne_einwilligung_abgelehnt(smtp_client, smtp):
    angebot, _ = _angebot(smtp_client)
    r = smtp_client.post("/api/anfrage", json=_body(angebot["tarif_id"], einwilligung=False))
    assert r.status_code == 422 and not smtp.gesendet


def test_ungueltige_kontaktangaben(smtp_client, smtp):
    angebot, _ = _angebot(smtp_client)
    b = _body(angebot["tarif_id"])
    b["kontakt"]["email"] = "keine-mail"
    assert smtp_client.post("/api/anfrage", json=b).status_code == 422
    b = _body(angebot["tarif_id"])
    b["kontakt"]["telefon"] = "ruf mich an"
    assert smtp_client.post("/api/anfrage", json=b).status_code == 422
    assert not smtp.gesendet


def test_header_injection_wird_neutralisiert(smtp_client, smtp):
    angebot, _ = _angebot(smtp_client)
    b = _body(angebot["tarif_id"])
    b["kontakt"]["nachname"] = "Muster\r\nBcc: boese@example.com"
    assert smtp_client.post("/api/anfrage", json=b).status_code == 201
    msg = email.message_from_bytes(bytes(smtp.gesendet[0]), policy=policy.default)
    assert msg["Bcc"] is None


def test_nicht_verfuegbares_angebot(smtp_client, smtp):
    # Tarif der Visperterminen (nur VS) ist in Fribourg nicht erhältlich
    con_tarif = smtp_client.get("/api/versicherer/1040").json()["tarife"][0]["tarif_id"]
    r = smtp_client.post("/api/anfrage", json=_body(con_tarif))
    assert r.status_code == 422 and not smtp.gesendet


def test_honeypot(smtp_client, smtp):
    angebot, _ = _angebot(smtp_client)
    r = smtp_client.post("/api/anfrage", json=_body(angebot["tarif_id"], website="http://spam"))
    assert r.status_code == 201 and not smtp.gesendet


def test_ohne_smtp_in_outbox(client, settings):
    angebot, _ = _angebot(client)
    r = client.post("/api/anfrage", json=_body(angebot["tarif_id"]))
    assert r.status_code == 201 and r.json()["zustellung"] == "outbox"
    emls = list(settings.outbox_dir.glob("*.eml"))
    assert len(emls) == 1 and b"malik.gobbi@dl-finance.ch" in emls[0].read_bytes()


def test_smtp_fehler_geht_nicht_verloren(smtp_client, smtp):
    smtp.fehler = smtplib.SMTPServerDisconnected("weg")
    angebot, _ = _angebot(smtp_client)
    r = smtp_client.post("/api/anfrage", json=_body(angebot["tarif_id"]))
    assert r.status_code == 201 and r.json()["zustellung"] == "fehler"
    assert list(smtp_client.settings.outbox_dir.glob("*.eml"))
    con = sqlite3.connect(smtp_client.settings.leads_db)
    assert con.execute("SELECT status, fehler FROM lead").fetchone()[0] == "fehler"


def test_rate_limit(settings, smtp):
    s = dataclasses.replace(settings, rate_limit_anzahl=2)
    with TestClient(create_app(s)) as c:
        angebot, _ = _angebot(c)
        codes = [c.post("/api/anfrage", json=_body(angebot["tarif_id"])).status_code for _ in range(3)]
    assert codes == [201, 201, 429]
