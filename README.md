# Prämienrechner Grundversicherung (OKP)

Webanwendung zum Vergleich der monatlichen Prämien der obligatorischen
Krankenpflegeversicherung aller Schweizer Krankenversicherer, analog zum
Prämienrechner des BAG (priminfo.admin.ch). Mit Offertanfrage: gewählte Prämie
und Kontaktangaben werden als Lead per E-Mail zugestellt.

```
data/  (BAG-Excel)  ──►  etl/build_db.py  ──►  db/praemien_<jahr>.sqlite  ──►  app/ (FastAPI)  ──►  frontend/ (HTML/JS)
                              │                                                   │
                              └─► db/validierung_<jahr>.md                         └─► POST /api/anfrage ─► SMTP ─► Lead-Empfänger
```

## Technik und Begründung

| Teil | Wahl | Warum |
|---|---|---|
| ETL | Python + pandas | Die BAG-Dateien sind Excel mit Freitext-Feldern. pandas mit der Engine `calamine` liest die 217'000 Zeilen in rund 7 s. |
| Datenbank | SQLite, **eine Datei pro Prämienjahr** | Die Daten werden nur gelesen. Die Datei ist rund 27 MB gross und lässt sich atomar ersetzen. Es braucht keinen DB-Server. |
| Backend | FastAPI | Typisierte REST-API, OpenAPI-Doku unter `/api/docs`, Validierung mit pydantic. |
| Frontend | Vanilla JS (ES-Module) + CSS, ohne Build-Schritt | Es gibt nur eine Seite. So braucht es kein Node-Tooling, und FastAPI liefert das Frontend direkt aus. |
| Design | angelehnt an finanu.ch | Schrift Raleway (Google Fonts), Farben #50B8E7/#11A3E6/#34ACE3, runde Ecken und hellblaue Flächen. Alle Farben sind als CSS-Variablen oben in `frontend/styles.css` definiert, inkl. Dark Mode. |
| Leads | eigene SQLite-Datei `leads.sqlite` + SMTP | Leads bleiben unabhängig von der jährlich ersetzten Prämien-DB erhalten. |

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# Datenbank bauen (Dateien werden in ./data gesucht)
python -m etl.build_db --jahr 2026

# Server starten
uvicorn app.main:create_app --factory --reload
# -> http://localhost:8000          Rechner
# -> http://localhost:8000/api/docs API-Dokumentation

# Tests (bauen die DB selbst in ein temporäres Verzeichnis)
pytest
# schneller mit vorhandener DB:  PRAEMIEN_DB_TEST=db/praemien_2026.sqlite pytest
```

## Konfiguration (Umgebungsvariablen)

| Variable | Standard | Bedeutung |
|---|---|---|
| `PRAEMIEN_DB` | neueste `db/praemien_*.sqlite` | Welche Prämiendatenbank die API verwendet |
| `LEAD_EMPFAENGER` | `malik.gobbi@dl-finance.ch` | Empfänger der Offertanfragen |
| `SMTP_HOST` | – | SMTP-Server. **Ohne diese Variable werden Leads nicht versendet**, sondern als `.eml` in `LEAD_OUTBOX` abgelegt. |
| `SMTP_PORT` | `587` | |
| `SMTP_SECURITY` | `starttls` | `starttls`, `ssl` (meist Port 465) oder `none` |
| `SMTP_USER` / `SMTP_PASSWORD` | – | Zugangsdaten SMTP |
| `SMTP_FROM` | `praemienrechner@dl-finance.ch` | Absender. Er muss vom SMTP-Server für den Versand zugelassen sein (SPF/DKIM). |
| `LEADS_DB` | `db/leads.sqlite` | Ablage aller Leads, inkl. Zustellstatus |
| `LEAD_OUTBOX` | `db/outbox` | `.eml`-Dateien, wenn SMTP fehlt oder fehlschlägt |
| `LEAD_RATE_LIMIT` / `LEAD_RATE_WINDOW` | `5` / `600` | Max. Anfragen pro IP-Adresse und Zeitfenster (Sekunden) |

Hinter einem Reverse-Proxy startet man uvicorn mit `--proxy-headers`. Sonst
zählt das Rate-Limit alle Anfragen unter der IP-Adresse des Proxys.

## Jährliche Datenaktualisierung

1. Die neuen BAG-Dateien herunterladen und nach `data/` legen, am besten in einen
   Unterordner pro Jahr, z.B. `data/2027/`:
   - Prämien CH (`gesamtbericht_ch.xlsx` oder `Prämien_CH.csv`) – opendata.swiss, Datensatz *health-insurance-premiums*
   - Verzeichnis der zugelassenen Krankenversicherer per 1.1. – bag.admin.ch
   - Prämienregionen (`praemienregionen.xlsx`) – bag.admin.ch
   - optional: *Tarife <Jahr>* und *Einzugsgebiete <Jahr>* aus demselben opendata-Datensatz
2. Die Datenbank bauen:
   ```bash
   python -m etl.build_db --jahr 2027 --data data/2027
   ```
   Die Dateien werden per Muster gefunden (`*gesamtbericht*`, `*Krankenversicherer*`,
   `*praemienregionen*`, `*Tarife*`, `*Einzugsgebiete*`). Einzelne Dateien lassen sich
   mit `--praemien`, `--versicherer`, `--regionen`, `--tarife` und `--einzugsgebiete`
   explizit angeben.
3. `db/validierung_2027.md` prüfen. Bei kritischen Fehlern bricht das Skript mit
   Exit-Code 1 ab und schreibt keine Datenbank. Eine bestehende DB bleibt unverändert.
4. `PRAEMIEN_DB=db/praemien_2027.sqlite` setzen (oder die neueste Datei wird
   automatisch genommen) und die API neu starten.
5. Die Stichprobentests laufen gegen die Excel-Datei in `data/`. Für das neue Jahr
   passt man `JAHR` in `tests/conftest.py` an und führt `pytest` aus.

### Validierung im ETL

- **Kritisch, führt zum Abbruch:**
  - Geschäftsjahr ≠ `--jahr`
  - Prämie ≤ 0 oder leer
  - Duplikate
  - unbekannte Codes (Kanton, Altersklasse, Unfall, Tariftyp, Region, Franchise, Altersuntergruppe)
  - Pflichtfelder leer
  - Tarif mit widersprüchlichem Typ oder Namen
  - Versicherer mit Prämien, aber ohne Stammdaten
  - Region mit Gemeinden, aber ohne Prämien (oder umgekehrt)
  - Gemeinde in mehreren Regionen
  - Versicherer mit Prämien trotz Fusion oder Bewilligungsentzug
  - Fremdschlüsselverletzungen
- **Warnung:**
  - fehlende optionale Dateien
  - Prämien ausserhalb des Tätigkeitsgebiets
  - Abweichungen zwischen den Sheets A_COM und B_NPA
  - Prämienregionen-Datei eines anderen Jahres

## Datenquellen

| Datei | Herkunft | Verwendung |
|---|---|---|
| `gesamtbericht_ch.xlsx` (Sheets Export, Wertebereiche) | BAG, Prämien OKP 2026 | Faktentabelle `praemie`, Tarife, Codes |
| `Zugelassene_Krankenversicherer_1_1_2026.xlsx` | BAG, Verzeichnis der zugelassenen Krankenversicherer | Stammdaten, Kontakt, Tätigkeitsgebiet, Fusionen |
| `praemienregionen.xlsx` (Release 2026-08-17) | BAG, Prämienregionen 2026 | Gemeinde → Region, PLZ-Suche |
| `Erlaeuterungen_zu_den_Praemiendaten.xlsx` | BAG | Beschreibung der Spalten und Codes |

## Datenmodell

Das Schema steht in `etl/schema.sql`. Die wichtigsten Tabellen:

- `kanton`, `region (kanton, nr 0–3)`, `gemeinde (bfs_nr → region)`
- `plz_gemeinde`: n:m-Beziehung. Eine PLZ kann mehrere Gemeinden umfassen, auch in
  verschiedenen Regionen oder Kantonen (2026: 270 PLZ).
- `versicherer`: Namen DE/FR/IT und Adresse in Strasse, Postfach, PLZ, Ort, Tel., Fax,
  E-Mail und Web aufgeteilt. Das Rohfeld bleibt in `adresse_raw` erhalten.
- `versicherer_taetigkeit`: Kanton, optional mit Region, z.B. «VS: Region 2».
- `versicherer_anpassung`: Fusionen und Bewilligungsentzüge.
- `tarif (bag_nr, tarif_code)`: Die Tarif-IDs sind nur pro Versicherer eindeutig.
- `franchise_stufe`: `FRAST2` ist bei Kindern CHF 100, bei Erwachsenen CHF 500.
  Die Stufe ist also nur zusammen mit der Altersklasse eindeutig.
- `einzugsgebiet`, `einzugsgebiet_gemeinde`, `versicherer_altersuntergruppe`: Diese
  Tabellen sind vorbereitet und werden befüllt, sobald *Einzugsgebiete* bzw. *Tarife*
  vorliegen.
- `praemie`: Faktentabelle mit Index `(region_id, altersklasse, mit_unfall, franchise, altersuntergruppe)`.

### Fachliche Regeln

- **Wohnort:** Massgebend ist die Gemeinde (BFS-Nr.), nicht die PLZ. Liegen die
  Treffer einer PLZ in verschiedenen Prämienregionen, muss die Nutzerin oder der
  Nutzer die Gemeinde wählen.
- **Alter:** Altersklasse = Prämienjahr − Geburtsjahr.
  - ≤ 18: Kinder (`AKL-KIN`)
  - 19–25: junge Erwachsene (`AKL-JUG`)
  - ab 26: Erwachsene (`AKL-ERW`)
- **Franchisen:** nur die für die Altersklasse zulässigen Werte.
  - Kinder: 0–600, ordentlich 0
  - Erwachsene: 300–2500, ordentlich 300
  - Nicht jeder Versicherer bietet jede Franchise an (z.B. Helsana für Kinder nur 0 und 500).
- **`isBaseP`** ist nur bei `BASE` + `MIT-UNF` gleich 1. Es ist die Referenzprämie
  des BAG. Für den Modellfilter wird deshalb der Tariftyp verwendet.
- **Angebote in einer Gemeinde:** Ein Angebot erscheint nur, wenn alle drei
  Bedingungen erfüllt sind:
  - Es gibt eine Prämie für die Region.
  - Die Region liegt im Tätigkeitsgebiet des Versicherers.
  - Falls Einzugsgebiete vorhanden sind, liegt die Gemeinde im Einzugsgebiet des Tarifs.
- **Gesamtkosten:** Jahresprämie + min(Kosten, Franchise) + min(10 % × (Kosten − Franchise), 700).
  Für Kinder ist der Selbstbehalt auf 350 begrenzt.
- **Entsandte (ZE) und Rheinschiffer (ZR):** Diese Prämienzeilen der BAG-Datei
  (2026: 164 Zeilen) werden bewusst nicht importiert. Sie haben keine Wohngemeinde
  und sind im Rechner nicht auswählbar. Der Validierungsreport weist die Anzahl aus.

### Altersuntergruppen (K1–K5, J1, E1)

Befund in den Prämiendaten 2026:

| Code | Vorkommen | Verhältnis zu K1 | Verwendung |
|---|---|---|---|
| leer | alle Zeilen von Jugendlichen und Erwachsenen | – | Standard |
| J1, E1, K2 | kommen nicht vor | – | – |
| K1 | alle 34 Versicherer | 1.00 | Kinderprämie ohne Rabatt (**Standard im Rechner**) |
| K3 | 17 Versicherer (z.B. Visana, SWICA, CONCORDIA) | 0.27–0.97, oft genau 0.50 | Kinderrabatt |
| K5 | Groupe Mutuel (343, 1479, 1507, 1535): genau 0.75; Assura: ≈ 0.96 | 0.75 bzw. ≈ 0.96 | Kinderrabatt |
| K4 | nur Assura (1542) | ≈ 0.98 | Kinderrabatt |

K3–K5 sind tiefere, versichererspezifische Kinderprämien, typischerweise für weitere
Kinder derselben Familie. Welche Bedingung genau gilt (z.B. «ab dem 3. Kind»), steht
nur in der Datei *Tarife <Jahr>* (Kategorie `ALT`), die 2026 nicht vorliegt.

Umsetzung im Rechner:
- Standardmässig wird K1 verwendet.
- Die Option «Kinderrabatt berücksichtigen» zeigt je Angebot die günstigste
  Rabattstufe des Versicherers. Sie ist in der Liste als «Rabattstufe K3» usw.
  gekennzeichnet.
- Über die API kann mit `altersuntergruppe=K3` eine bestimmte Stufe abgefragt werden.
- Liegt die Tarife-Datei vor, speichert das ETL die Bezeichnungen je Versicherer in
  `versicherer_altersuntergruppe`.

## API

| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/api/orte?q=1700` | Ortssuche nach PLZ oder Name. Liefert Gemeinden mit Kanton, Region und `mehrere_regionen`. |
| GET | `/api/gemeinden/{bfs}` | Gemeinde mit Region und PLZ |
| GET | `/api/stammdaten` | Altersklassen mit zulässigen Franchisen, Modelle, Unfall, Versicherer, Prämienjahr |
| GET | `/api/altersklasse?geburtsjahr=1985` | Altersklasse und Alter im Prämienjahr |
| GET | `/api/versicherer`, `/api/versicherer/{bag_nr}` | Liste; Detail mit Kontakt, Tätigkeitsgebiet, Modellen, Fusionen |
| GET | `/api/praemien?bfs=261&geburtsjahr=1985&unfall=true&franchise=FRA-300[&modell=TAR-HMO][&kinderrabatt=true][&aktueller_versicherer=1509 \| &aktueller_tarif=… \| &aktuelle_praemie=480]` | Alle Angebote, aufsteigend nach Monatsprämie, mit Differenz zum günstigsten und zum aktuellen Versicherer |
| GET | `/api/gesamtkosten?bfs=261&geburtsjahr=1985&unfall=true&kosten=3000[&tarif_id=…]` | Gesamtkosten für alle Franchisen, günstigste Franchise markiert |
| GET | `/api/meta` | Prämienjahr, Quelle, Datenstand |
| POST | `/api/anfrage` | Offertanfrage (Lead) |

## Offertanfrage (Lead)

Ablauf:
1. In der Resultatliste klickt die Person bei einem Angebot auf **Anfragen** und
   füllt das Formular aus: Anrede, Name, E-Mail, Telefon, optional Adresse,
   Geburtsdatum, Erreichbarkeit und Bemerkung.
2. Die Einwilligung zur Weitergabe ist Pflicht.
3. Der Server rechnet das gewählte Angebot aus den Rechner-Eingaben neu. Preise aus
   dem Browser werden nie übernommen.
4. Die E-Mail an `LEAD_EMPFAENGER` enthält:
   - Kontaktangaben
   - Wohngemeinde mit Kanton und Region
   - Geburtsjahr und Altersklasse
   - Unfalldeckung, Franchise und Modellfilter
   - die gewählte Prämie (Versicherer, Modell, Tarif, Monat, Jahr, Rang)
   - das günstigste Angebot, den aktuellen Versicherer und die Ersparnis
   - die fünf nächsten Alternativen

   `Reply-To` ist die Adresse der anfragenden Person.
5. Jeder Lead wird in `leads.sqlite` mit Status `gesendet`, `outbox` oder `fehler`
   gespeichert. Scheitert der Versand, liegt die Nachricht zusätzlich als `.eml` in
   der Outbox. Es geht also kein Lead verloren.

Schutzmassnahmen:
- Honeypot-Feld
- Rate-Limit pro IP-Adresse
- Zeilenumbrüche in Kopfzeilenfeldern werden entfernt
- HTML-Escaping in der E-Mail

**Datenschutz:** Der Einwilligungstext steht in `frontend/i18n/de.json`
(`anfrage.einwilligung`). Er nennt derzeit neutral «unseren Beratungspartner» und
sollte vor dem Livegang an die Datenschutzerklärung angepasst werden (nDSG).

## Mehrsprachigkeit

- Alle UI-Texte stehen in `frontend/i18n/de.json`. Für Französisch oder Italienisch
  legt man `fr.json` bzw. `it.json` mit denselben Schlüsseln an; die Sprache wird mit
  `?lang=fr` gewählt.
- Codes (Kantone, Altersklassen, Modelle usw.) haben in der DB die Spalten
  `name_de`, `name_fr` und `name_it`.
- Die Versicherernamen sind nach Sprache getrennt.
- Tarifnamen in FR und IT kommen aus der optionalen Datei *Tarife*.

## Bekannte Einschränkungen

- Die Einzugsgebiete 2026 lagen nicht vor. Auf Gemeinden eingeschränkte HMO- und
  Hausarztmodelle werden deshalb noch angezeigt; der Footer weist darauf hin.
  Sobald die Datei in `data/` liegt, filtert das ETL automatisch.
- Die Bedeutung der Kinder-Rabattstufen ist ohne *Tarife*-Datei nicht benannt
  (siehe oben).
- Prämien für Versicherte mit Wohnsitz in der EU/EFTA sind nicht Teil dieses Rechners.
