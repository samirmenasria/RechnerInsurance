"""Offertanfragen (Leads): prüfen, speichern und per E-Mail zustellen.

Ablauf:
1. Die Prämie des gewählten Angebots wird serverseitig neu berechnet – Werte
   aus dem Browser werden nicht übernommen.
2. Der Lead wird in `leads.sqlite` gespeichert (unabhängig von der jährlich
   ersetzten Prämiendatenbank).
3. Versand per SMTP an den konfigurierten Empfänger. Ist kein SMTP-Server
   konfiguriert oder schlägt der Versand fehl, wird die Nachricht als .eml im
   Outbox-Ordner abgelegt; der Lead geht nicht verloren.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import logging
import smtplib
import sqlite3
import ssl
import threading
import time
import uuid
from collections import defaultdict, deque
from email import policy
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

from app import rechner
from app.config import Settings

log = logging.getLogger("praemienrechner.leads")


# --------------------------------------------------------------------------
# Eingabe
# --------------------------------------------------------------------------

def _einzeilig(v: str | None) -> str | None:
    if v is None:
        return None
    v = " ".join(v.replace("\r", " ").replace("\n", " ").split())
    return v or None


class Kontakt(BaseModel):
    anrede: Literal["Frau", "Herr", "Keine Angabe"] = "Keine Angabe"
    vorname: str = Field(min_length=1, max_length=80)
    nachname: str = Field(min_length=1, max_length=80)
    email: EmailStr
    telefon: str = Field(min_length=6, max_length=40, pattern=r"^[0-9+()\/.\s-]+$")
    strasse: str | None = Field(default=None, max_length=120)
    geburtsdatum: dt.date | None = None
    erreichbarkeit: str | None = Field(default=None, max_length=120)
    bemerkung: str | None = Field(default=None, max_length=2000)

    @field_validator("vorname", "nachname", "strasse", "erreichbarkeit", "telefon")
    @classmethod
    def _keine_umbrueche(cls, v):
        return _einzeilig(v)


class PersonIn(BaseModel):
    """Eine versicherte Person im Rechner."""
    name: str = Field(default="", max_length=80)
    geburtsjahr: int = Field(ge=1890, le=2100)
    mit_unfall: bool
    franchise: str = Field(pattern=r"^FRA-\d+$")

    @field_validator("name")
    @classmethod
    def _name_einzeilig(cls, v):
        return _einzeilig(v) or ""


class PersonAnfrageIn(PersonIn):
    name: str = Field(min_length=1, max_length=80)
    tarif_id: int


class AnfrageIn(BaseModel):
    # Rechner-Eingaben (werden serverseitig neu berechnet)
    bfs_nr: int
    modell: str | None = None
    kinderrabatt: bool = True
    modus: Literal["gemeinsam", "kombination"] = "gemeinsam"
    personen: list[PersonAnfrageIn] = Field(min_length=1, max_length=rechner.MAX_PERSONEN)
    aktueller_versicherer: int | None = None
    aktuelle_praemie: float | None = Field(default=None, gt=0, lt=20000)
    kontakt: Kontakt
    einwilligung: bool
    website: str | None = None  # Honeypot – bleibt bei echten Nutzern leer


class LeadFehler(ValueError):
    pass


# --------------------------------------------------------------------------
# Rate-Limit (pro IP, im Speicher)
# --------------------------------------------------------------------------

class RateLimiter:
    def __init__(self, anzahl: int, sekunden: int):
        self.anzahl, self.sekunden = anzahl, sekunden
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def erlaubt(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.sekunden:
                q.popleft()
            if len(q) >= self.anzahl:
                return False
            q.append(now)
            return True


# --------------------------------------------------------------------------
# Speicherung
# --------------------------------------------------------------------------

LEAD_SCHEMA = """
CREATE TABLE IF NOT EXISTS lead (
    id          TEXT PRIMARY KEY,
    erstellt    TEXT NOT NULL,
    status      TEXT NOT NULL,          -- gesendet | outbox | fehler | verworfen
    empfaenger  TEXT NOT NULL,
    email       TEXT,
    name        TEXT,
    praemienjahr INTEGER,
    bag_nr      INTEGER,
    tarif_id    INTEGER,
    monat       REAL,
    daten       TEXT NOT NULL,          -- vollständige Anfrage als JSON
    fehler      TEXT,
    ip          TEXT
);
"""


def _leads_con(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(LEAD_SCHEMA)
    return con


def _speichern(settings: Settings, lead: dict, status: str, fehler: str | None, ip: str | None) -> None:
    erste = lead["personen"][0]["angebot"]
    with _leads_con(settings.leads_db) as con:
        con.execute(
            "INSERT OR REPLACE INTO lead VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (lead["id"], lead["erstellt"], status, settings.lead_empfaenger, lead["kontakt"]["email"],
             f"{lead['kontakt']['vorname']} {lead['kontakt']['nachname']}", lead["praemienjahr"],
             erste["bag_nr"], erste["tarif_id"], lead["total"]["monat"],
             json.dumps(lead, ensure_ascii=False, default=str), fehler, ip))


# --------------------------------------------------------------------------
# E-Mail
# --------------------------------------------------------------------------

def chf(v: float | None) -> str:
    if v is None:
        return "–"
    s = f"{abs(v):,.2f}".replace(",", "'")
    return f"{'-' if v < 0 else ''}CHF {s}"


MODUS_TEXT = {"gemeinsam": "alle beim gleichen Versicherer", "kombination": "günstigste Kombination"}


def _person_text(p: dict) -> str:
    a = p["angebot"]
    zeile1 = (f"Jahrgang {p['geburtsjahr']} ({p['alter']} J.), {p['altersklasse_name']} · "
              f"Franchise {chf(p['franchise_betrag']).replace('.00', '')} · "
              f"{'mit' if p['mit_unfall'] else 'ohne'} Unfall")
    zeile2 = f"{a['versicherer']} – {a['tarifbezeichnung']} ({a['modell']}): {chf(a['monat'])}/Monat"
    if a.get("rabatt"):
        zeile2 += f" [Kinderrabatt {a['altersuntergruppe']}]"
    return f"{zeile1}\n{zeile2}"


def _abschnitte(lead: dict) -> list[tuple[str, list[tuple[str, str]]]]:
    k, o, b, vgl, tot = lead["kontakt"], lead["ort"], lead["berechnung"], lead["vergleich"], lead["total"]
    kontakt = [
        ("Name", f"{k['anrede'] if k['anrede'] != 'Keine Angabe' else ''} {k['vorname']} {k['nachname']}".strip()),
        ("E-Mail", k["email"]),
        ("Telefon", k["telefon"]),
        ("Adresse", ", ".join(x for x in (k.get("strasse"), f"{o['plz']} {o['ort']}".strip()) if x)),
    ]
    if k.get("geburtsdatum"):
        kontakt.append(("Geburtsdatum", dt.date.fromisoformat(str(k["geburtsdatum"])).strftime("%d.%m.%Y")))
    if k.get("erreichbarkeit"):
        kontakt.append(("Erreichbarkeit", k["erreichbarkeit"]))
    if k.get("bemerkung"):
        kontakt.append(("Bemerkung", k["bemerkung"]))

    berechnung = [
        ("Wohngemeinde", f"{o['gemeinde']} (BFS {o['bfs_nr']}), Kanton {o['kanton']}"),
        ("Prämienregion", f"{o['kanton']} Region {o['region_nr']}"),
        ("Anzahl Personen", str(len(lead["personen"]))),
        ("Variante", MODUS_TEXT[b["modus"]]),
        ("Modellfilter", b["modellfilter"]),
    ]
    if b["geschwisterrabatt"]:
        berechnung.append(("Geschwisterrabatt", "berücksichtigt (ab dem 2. Kind, günstigste Rabattstufe)"))

    personen = [(p["name"], _person_text(p)) for p in lead["personen"]]

    total = [("Monatsprämie total", chf(tot["monat"])), ("Jahresprämie total", chf(tot["jahr"]))]
    if tot.get("rang"):
        total.append(("Rang", f"{tot['rang']} von {vgl['anzahl_gemeinsam']} gemeinsamen Angeboten"))

    vergleich = []
    if vgl.get("guenstigste_gemeinsam"):
        vergleich.append(("Günstigster gemeinsamer Versicherer", vgl["guenstigste_gemeinsam"]))
    if vgl.get("guenstigste_kombination"):
        vergleich.append(("Günstigste Kombination", vgl["guenstigste_kombination"]))
    if vgl.get("aktuell"):
        vergleich.append(("Aktueller Versicherer", vgl["aktuell"]))
        vergleich.append(("Ersparnis ggü. aktuell", vgl["ersparnis"]))
    return [("Kontaktangaben", kontakt), ("Wohnort & Berechnung", berechnung),
            ("Versicherte Personen & gewählte Prämien", personen), ("Total Haushalt", total),
            ("Vergleich", vergleich)]


def email_erstellen(settings: Settings, lead: dict) -> EmailMessage:
    k, ps, tot = lead["kontakt"], lead["personen"], lead["total"]
    if lead["berechnung"]["modus"] == "gemeinsam":
        wahl = f"{ps[0]['angebot']['versicherer']} {ps[0]['angebot']['tarifbezeichnung']}"
    else:
        wahl = f"Kombination ({len({p['angebot']['bag_nr'] for p in ps})} Versicherer)"
    anzahl = f" ({len(ps)} Personen)" if len(ps) > 1 else ""
    # Ohne Zeilenfaltung und mit ASCII-Trennern: sonst gehen Leerzeichen zwischen
    # kodierten Wörtern (Umlaute, Gedankenstrich) im Betreff verloren.
    msg = EmailMessage(policy=policy.SMTP.clone(max_line_length=998))
    msg["Subject"] = (f"Neue Offertanfrage OKP {lead['praemienjahr']}: {k['vorname']} {k['nachname']}{anzahl}, "
                      f"{lead['ort']['plz']} {lead['ort']['ort']} - {wahl}, {chf(tot['monat'])}/Monat")
    msg["From"] = formataddr(("Prämienrechner", settings.smtp_from))
    msg["To"] = settings.lead_empfaenger
    msg["Reply-To"] = formataddr((f"{k['vorname']} {k['nachname']}", k["email"]))
    msg["Message-ID"] = make_msgid(domain=settings.smtp_from.split("@")[-1])
    msg["X-Lead-ID"] = lead["id"]

    abschnitte = _abschnitte(lead)
    einzug = "\n" + " " * 38
    text = [f"Neue Offertanfrage aus dem Prämienrechner (Lead {lead['id']})",
            f"Eingegangen: {lead['erstellt_lokal']}", ""]
    for titel, zeilen in abschnitte:
        if not zeilen:
            continue
        text += [titel.upper(), "-" * len(titel)]
        text += [f"{label + ':':37s} {str(wert).replace(chr(10), einzug)}" for label, wert in zeilen]
        text.append("")
    if lead["alternativen"]:
        text += ["WEITERE GÜNSTIGE ANGEBOTE (ALLE BEIM GLEICHEN VERSICHERER)", "-" * 56]
        text += [f"{a['rang']:>3}. {a['versicherer']} – {a['tarifbezeichnung']} ({a['modell']}): "
                 f"{chf(a['monat'])}/Monat" for a in lead["alternativen"]]
        text.append("")
    text += [f"Datenquelle: {lead['quelle']}, Prämienjahr {lead['praemienjahr']}.",
             "Die Person hat der Weitergabe ihrer Angaben zur Kontaktaufnahme zugestimmt."]
    msg.set_content("\n".join(text))

    e = html.escape
    parts = [f"<h2 style='margin:0 0 4px;color:#1f95cc'>Neue Offertanfrage OKP {lead['praemienjahr']}</h2>",
             f"<p style='color:#555;margin:0 0 16px'>Lead {e(lead['id'])} · {e(lead['erstellt_lokal'])}</p>"]
    for titel, zeilen in abschnitte:
        if not zeilen:
            continue
        parts.append(f"<h3 style='margin:18px 0 6px'>{e(titel)}</h3><table cellpadding='4' style='border-collapse:collapse'>")
        parts += [f"<tr><td style='color:#555;vertical-align:top;padding-right:16px'>{e(l)}</td>"
                  f"<td><strong>{e(str(w)).replace(chr(10), '<br>')}</strong></td></tr>" for l, w in zeilen]
        parts.append("</table>")
    if lead["alternativen"]:
        parts.append("<h3 style='margin:18px 0 6px'>Weitere günstige Angebote (alle beim gleichen Versicherer)</h3><ol>")
        parts += [f"<li value='{a['rang']}'>{e(a['versicherer'])} – {e(a['tarifbezeichnung'])} ({e(a['modell'])}): "
                  f"{e(chf(a['monat']))}/Monat</li>" for a in lead["alternativen"]]
        parts.append("</ol>")
    parts.append(f"<p style='color:#555;font-size:12px'>Datenquelle: {e(lead['quelle'])}, Prämienjahr "
                 f"{lead['praemienjahr']}. Die Person hat der Weitergabe ihrer Angaben zur Kontaktaufnahme zugestimmt.</p>")
    msg.add_alternative("<div style='font-family:Arial,sans-serif;font-size:14px'>" + "".join(parts) + "</div>",
                        subtype="html")
    return msg


def _senden(settings: Settings, msg: EmailMessage) -> None:
    ctx = ssl.create_default_context()
    if settings.smtp_security == "ssl":
        smtp = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout, context=ctx)
    else:
        smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout)
    with smtp:
        if settings.smtp_security == "starttls":
            smtp.starttls(context=ctx)
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password or "")
        smtp.send_message(msg)


def _outbox(settings: Settings, lead_id: str, msg: EmailMessage) -> Path:
    settings.outbox_dir.mkdir(parents=True, exist_ok=True)
    path = settings.outbox_dir / f"{lead_id}.eml"
    path.write_bytes(bytes(msg))
    return path


# --------------------------------------------------------------------------
# Hauptfunktion
# --------------------------------------------------------------------------

def lead_verarbeiten(con: sqlite3.Connection, settings: Settings, anfrage: AnfrageIn, ip: str | None = None) -> dict:
    if not anfrage.einwilligung:
        raise LeadFehler("Die Einwilligung zur Kontaktaufnahme ist erforderlich.")
    lead_id = uuid.uuid4().hex[:12]
    if anfrage.website:  # Honeypot ausgefüllt -> Bot; still verwerfen, Antwort wie bei Erfolg
        log.warning("Lead verworfen (Honeypot) von %s", ip)
        return {"lead_id": lead_id, "status": "ok"}

    tarif_ids = [p.tarif_id for p in anfrage.personen]
    if anfrage.modus == "gemeinsam" and len(set(tarif_ids)) != 1:
        raise LeadFehler("Bei «alle beim gleichen Versicherer» muss für alle Personen dasselbe Angebot gewählt sein.")
    modell = anfrage.modell or None
    personen = [rechner.Person(p.name, p.geburtsjahr, p.mit_unfall, p.franchise) for p in anfrage.personen]
    info, gewaehlt = rechner.haushalt_auswahl(con, anfrage.bfs_nr, personen, tarif_ids, modell, anfrage.kinderrabatt)
    res = rechner.haushalt(con, anfrage.bfs_nr, personen, modell, anfrage.kinderrabatt,
                           anfrage.aktueller_versicherer, anfrage.aktuelle_praemie)

    total_monat = round(sum(a["monat"] for a in gewaehlt), 2)
    total = {"monat": total_monat, "jahr": round(total_monat * 12, 2)}
    gemeinsam = res["gemeinsam"]["angebote"]
    if anfrage.modus == "gemeinsam":
        total["rang"] = next((x["rang"] for x in gemeinsam if x["tarif_id"] == tarif_ids[0]), None)
    alternativen = [x for x in gemeinsam if x["tarif_id"] != tarif_ids[0] or anfrage.modus != "gemeinsam"][:5]

    vergleich: dict = {"anzahl_gemeinsam": len(gemeinsam)}
    if gemeinsam:
        g0 = gemeinsam[0]
        vergleich["guenstigste_gemeinsam"] = f"{g0['versicherer']} – {g0['tarifbezeichnung']}: {chf(g0['monat'])}/Monat"
    if res["kombination"] and len(personen) > 1:
        kb = res["kombination"]
        vergleich["guenstigste_kombination"] = f"{chf(kb['monat'])}/Monat ({kb['anzahl_versicherer']} Versicherer)"
    aktuell = res["aktuell"]
    if aktuell:
        if aktuell["quelle"] == "eingegeben":
            vergleich["aktuell"] = f"eigene Angabe: {chf(aktuell['monat'])}/Monat"
        else:
            vergleich["aktuell"] = f"{aktuell['versicherer']} – {aktuell['tarifbezeichnung']}: {chf(aktuell['monat'])}/Monat"
        diff = round(aktuell["monat"] - total_monat, 2)
        vergleich["ersparnis"] = f"{chf(diff)}/Monat, {chf(round(diff * 12, 2))}/Jahr"

    g = res["kontext"]["gemeinde"]
    plz_ort = rechner.orte_suchen(con, str(g["plz"][0])) if g["plz"] else {"gemeinden": []}
    ort_label = next((o for x in plz_ort["gemeinden"] if x["bfs_nr"] == g["bfs_nr"] for o in x["orte"]), None)
    plz, ort = (ort_label.split(" ", 1) if ort_label else ("", g["gemeinde"]))
    modell_namen = {m["code"]: m["name_de"] for m in rechner.stammdaten(con)["modelle"]}

    now = dt.datetime.now().astimezone()
    lead = {
        "id": lead_id,
        "erstellt": now.isoformat(timespec="seconds"),
        "erstellt_lokal": now.strftime("%d.%m.%Y %H:%M"),
        "praemienjahr": res["kontext"]["praemienjahr"],
        "quelle": "Bundesamt für Gesundheit BAG, Prämien OKP",
        "kontakt": anfrage.kontakt.model_dump(mode="json"),
        "ort": {"bfs_nr": g["bfs_nr"], "gemeinde": g["gemeinde"], "kanton": g["kanton"],
                "region_nr": g["region_nr"], "plz": plz, "ort": ort},
        "berechnung": {"modus": anfrage.modus, "modellfilter": modell_namen.get(modell, "alle Modelle"),
                       "geschwisterrabatt": any(p["geschwisterrabatt"] for p in info)},
        "personen": [{**p, "angebot": a} for p, a in zip(info, gewaehlt)],
        "total": total,
        "vergleich": vergleich,
        "alternativen": alternativen,
    }

    msg = email_erstellen(settings, lead)
    status, fehler = "gesendet", None
    if settings.smtp_konfiguriert:
        try:
            _senden(settings, msg)
        except (OSError, smtplib.SMTPException) as exc:
            status, fehler = "fehler", f"{type(exc).__name__}: {exc}"
            _outbox(settings, lead_id, msg)
            log.error("Lead %s: Versand fehlgeschlagen (%s), in Outbox abgelegt", lead_id, fehler)
    else:
        status = "outbox"
        path = _outbox(settings, lead_id, msg)
        log.warning("Lead %s: kein SMTP konfiguriert, abgelegt unter %s", lead_id, path)
    _speichern(settings, lead, status, fehler, ip)
    return {"lead_id": lead_id, "status": "ok", "zustellung": status}
