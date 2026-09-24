"""Rechnerlogik: Ortssuche, Stammdaten, Prämienabfrage und Gesamtkosten.

Alle Funktionen arbeiten auf einer read-only SQLite-Verbindung der
Prämiendatenbank und sind unabhängig von FastAPI testbar.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

# Maximaler Selbstbehalt pro Jahr (Art. 103 KVV)
MAX_SELBSTBEHALT = {"AKL-KIN": 350.0, "AKL-JUG": 700.0, "AKL-ERW": 700.0}
SELBSTBEHALT_SATZ = 0.10
MODELLE = ("TAR-BASE", "TAR-HAM", "TAR-HMO", "TAR-DIV")


class RechnerFehler(ValueError):
    """Ungültige Eingabe (führt zu HTTP 422)."""


class NichtGefunden(LookupError):
    """Ressource existiert nicht (führt zu HTTP 404)."""


def connect(path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def meta(con: sqlite3.Connection) -> dict:
    return {r["schluessel"]: r["wert"] for r in con.execute("SELECT schluessel, wert FROM meta")}


def praemienjahr(con: sqlite3.Connection) -> int:
    return int(meta(con)["praemienjahr"])


# --------------------------------------------------------------------------
# Alter
# --------------------------------------------------------------------------

def altersklasse_fuer(con: sqlite3.Connection, geburtsjahr: int) -> dict:
    """Altersklasse nach Jahrgang: massgebend ist das Alter, das im Prämienjahr erreicht wird."""
    jahr = praemienjahr(con)
    alter = jahr - geburtsjahr
    if alter < 0 or alter > 130:
        raise RechnerFehler(f"Geburtsjahr {geburtsjahr} ist für das Prämienjahr {jahr} nicht plausibel")
    row = con.execute(
        "SELECT * FROM altersklasse WHERE alter_von <= ? AND (alter_bis IS NULL OR alter_bis >= ?)",
        (alter, alter)).fetchone()
    return {"code": row["code"], "name": row["name_de"], "alter": alter, "praemienjahr": jahr}


# --------------------------------------------------------------------------
# Ortssuche
# --------------------------------------------------------------------------

def orte_suchen(con: sqlite3.Connection, q: str, limit: int = 30) -> dict:
    """PLZ oder Ortsname -> Gemeinden.

    Massgebend für die Prämienregion ist die Gemeinde (BFS-Nr.), nicht die PLZ.
    `mehrere_regionen` zeigt an, dass die Treffer in verschiedenen
    Prämienregionen liegen und der Nutzer die Gemeinde wählen muss.
    """
    q = (q or "").strip()
    if len(q) < 2:
        return {"query": q, "gemeinden": [], "mehrere_regionen": False}
    if re.fullmatch(r"\d{2,4}", q):
        where, params = "CAST(pg.plz AS TEXT) LIKE ?", (f"{q}%",)
    else:
        like = f"{q}%"
        where = "(pg.ortsbezeichnung LIKE ? OR g.name LIKE ? OR pg.ortsbezeichnung LIKE ? OR g.name LIKE ?)"
        params = (like, like, f"% {q}%", f"% {q}%")
    rows = con.execute(f"""
        SELECT g.bfs_nr, g.name AS gemeinde, g.kanton, g.bezirk, r.nr AS region_nr, r.code AS region_code,
               pg.plz, pg.ortsbezeichnung
        FROM plz_gemeinde pg
        JOIN gemeinde g ON g.bfs_nr = pg.bfs_nr
        JOIN region r ON r.id = g.region_id
        WHERE {where}
        ORDER BY pg.plz, g.name, pg.ortsbezeichnung
    """, params).fetchall()

    gemeinden: dict[int, dict] = {}
    for r in rows:
        g = gemeinden.setdefault(r["bfs_nr"], {
            "bfs_nr": r["bfs_nr"], "gemeinde": r["gemeinde"], "kanton": r["kanton"], "bezirk": r["bezirk"],
            "region_nr": r["region_nr"], "region_code": r["region_code"], "plz": [], "orte": [],
        })
        if r["plz"] not in g["plz"]:
            g["plz"].append(r["plz"])
        label = f"{r['plz']} {r['ortsbezeichnung']}"
        if label not in g["orte"]:
            g["orte"].append(label)
    result = list(gemeinden.values())[:limit]
    regionen = {(g["kanton"], g["region_nr"]) for g in result}
    return {"query": q, "gemeinden": result, "mehrere_regionen": len(regionen) > 1}


def gemeinde(con: sqlite3.Connection, bfs_nr: int) -> dict:
    r = con.execute("""
        SELECT g.bfs_nr, g.name AS gemeinde, g.kanton, k.name_de AS kanton_name, g.bezirk,
               r.id AS region_id, r.nr AS region_nr, r.code AS region_code
        FROM gemeinde g JOIN region r ON r.id = g.region_id JOIN kanton k ON k.code = g.kanton
        WHERE g.bfs_nr = ?""", (bfs_nr,)).fetchone()
    if r is None:
        raise NichtGefunden(f"Gemeinde mit BFS-Nr. {bfs_nr} nicht gefunden")
    d = dict(r)
    d["plz"] = [x[0] for x in con.execute(
        "SELECT DISTINCT plz FROM plz_gemeinde WHERE bfs_nr = ? ORDER BY plz", (bfs_nr,))]
    return d


# --------------------------------------------------------------------------
# Stammdaten
# --------------------------------------------------------------------------

def stammdaten(con: sqlite3.Connection) -> dict:
    m = meta(con)
    akl = []
    for a in con.execute("SELECT * FROM altersklasse ORDER BY sort"):
        franchisen = [dict(f) for f in con.execute("""
            SELECT f.code, f.betrag, fs.stufe, fs.ist_ordentlich
            FROM franchise_stufe fs JOIN franchise f ON f.code = fs.franchise
            WHERE fs.altersklasse = ? ORDER BY f.betrag""", (a["code"],))]
        akl.append({**dict(a), "franchisen": franchisen, "max_selbstbehalt": MAX_SELBSTBEHALT[a["code"]]})
    return {
        "praemienjahr": int(m["praemienjahr"]),
        "quelle": m.get("quelle"),
        "stand": m.get("build_zeit"),
        "einzugsgebiete_vorhanden": m.get("einzugsgebiete_vorhanden") == "1",
        "altersklassen": akl,
        "modelle": [dict(r) for r in con.execute("SELECT * FROM tariftyp ORDER BY sort")],
        "unfalleinschluss": [dict(r) for r in con.execute("SELECT * FROM unfalleinschluss")],
        "altersuntergruppen": [dict(r) for r in con.execute("SELECT * FROM altersuntergruppe ORDER BY code")],
        "versicherer": versicherer_liste(con),
    }


def versicherer_liste(con: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in con.execute("""
        SELECT bag_nr, name_kurz, name_de, gruppe FROM versicherer
        WHERE nur_taggeld = 0 AND bag_nr IN (SELECT DISTINCT bag_nr FROM tarif)
        ORDER BY name_kurz COLLATE NOCASE""")]


def versicherer_detail(con: sqlite3.Connection, bag_nr: int) -> dict:
    r = con.execute("SELECT * FROM versicherer WHERE bag_nr = ?", (bag_nr,)).fetchone()
    if r is None:
        raise NichtGefunden(f"Versicherer {bag_nr} nicht gefunden")
    d = dict(r)
    d["taetigkeit"] = [dict(x) for x in con.execute(
        "SELECT kanton, region_nr FROM versicherer_taetigkeit WHERE bag_nr = ? ORDER BY kanton, region_nr", (bag_nr,))]
    d["tarife"] = [dict(x) for x in con.execute("""
        SELECT t.id AS tarif_id, t.tarif_code, t.tariftyp, tt.name_de AS modell, t.bezeichnung_de
        FROM tarif t JOIN tariftyp tt ON tt.code = t.tariftyp
        WHERE t.bag_nr = ? ORDER BY tt.sort, t.bezeichnung_de""", (bag_nr,))]
    d["anpassungen"] = [dict(x) for x in con.execute(
        "SELECT * FROM versicherer_anpassung WHERE ziel_bag_nr = ?", (bag_nr,))]
    return d


# --------------------------------------------------------------------------
# Prämienabfrage
# --------------------------------------------------------------------------

@dataclass
class Anfrage:
    bfs_nr: int
    altersklasse: str
    mit_unfall: bool
    franchise: str
    modell: str | None = None           # None = alle Modelle
    kinderrabatt: bool = False           # günstigste Rabattstufe (K3–K5) statt K1
    altersuntergruppe: str | None = None  # explizite Untergruppe (übersteuert kinderrabatt)


def _pruefe(con: sqlite3.Connection, a: Anfrage) -> dict:
    g = gemeinde(con, a.bfs_nr)
    if a.modell is not None and a.modell not in MODELLE:
        raise RechnerFehler(f"Unbekanntes Versicherungsmodell {a.modell!r}")
    ok = con.execute("SELECT 1 FROM franchise_stufe WHERE altersklasse = ? AND franchise = ?",
                     (a.altersklasse, a.franchise)).fetchone()
    if not ok:
        zul = [r[0] for r in con.execute(
            "SELECT f.betrag FROM franchise_stufe fs JOIN franchise f ON f.code = fs.franchise "
            "WHERE fs.altersklasse = ? ORDER BY f.betrag", (a.altersklasse,))]
        raise RechnerFehler(f"Franchise {a.franchise} ist für {a.altersklasse} nicht zulässig (zulässig: {zul})")
    if a.altersuntergruppe is not None:
        ug = con.execute("SELECT altersklasse FROM altersuntergruppe WHERE code = ?", (a.altersuntergruppe,)).fetchone()
        if ug is None or ug[0] != a.altersklasse:
            raise RechnerFehler(f"Altersuntergruppe {a.altersuntergruppe} passt nicht zu {a.altersklasse}")
    return g


def _angebote_roh(con: sqlite3.Connection, a: Anfrage, g: dict, franchise: str | None = None) -> list[sqlite3.Row]:
    return con.execute("""
        SELECT p.betrag, p.altersuntergruppe, p.franchise, t.id AS tarif_id, t.tarif_code, t.tariftyp,
               t.bezeichnung_de, tt.name_de AS modell, tt.sort AS modell_sort, v.bag_nr, v.name_kurz, v.gruppe
        FROM praemie p
        JOIN tarif t      ON t.id = p.tarif_id
        JOIN tariftyp tt  ON tt.code = t.tariftyp
        JOIN versicherer v ON v.bag_nr = t.bag_nr
        WHERE p.region_id = :region_id
          AND p.altersklasse = :akl
          AND p.mit_unfall = :unf
          AND (:franchise IS NULL OR p.franchise = :franchise)
          AND (:modell IS NULL OR t.tariftyp = :modell)
          AND v.aktiv = 1 AND v.nur_taggeld = 0
          -- Tätigkeitsgebiet des Versicherers (Kanton bzw. Prämienregion)
          AND EXISTS (SELECT 1 FROM versicherer_taetigkeit vt
                      WHERE vt.bag_nr = v.bag_nr AND vt.kanton = :kanton
                        AND (vt.region_nr IS NULL OR vt.region_nr = :region_nr))
          -- Einzugsgebiet: Tarife mit Einträgen für den Kanton sind nur dort verfügbar,
          -- wo die Region passt und (bei Einschränkung) die Gemeinde aufgeführt ist
          AND (NOT EXISTS (SELECT 1 FROM einzugsgebiet e WHERE e.tarif_id = t.id AND e.kanton = :kanton)
               OR EXISTS (SELECT 1 FROM einzugsgebiet e
                          WHERE e.tarif_id = t.id AND e.kanton = :kanton
                            AND (e.region_id IS NULL OR e.region_id = :region_id)
                            AND (e.eingeschraenkt = 0 OR EXISTS (
                                 SELECT 1 FROM einzugsgebiet_gemeinde eg
                                 WHERE eg.einzugsgebiet_id = e.id AND eg.bfs_nr = :bfs))))
    """, {
        "region_id": g["region_id"], "region_nr": g["region_nr"], "kanton": g["kanton"], "bfs": g["bfs_nr"],
        "akl": a.altersklasse, "unf": int(a.mit_unfall), "franchise": franchise, "modell": a.modell,
    }).fetchall()


def _waehle_untergruppe(rows: list[sqlite3.Row], a: Anfrage) -> dict[tuple[int, str], sqlite3.Row]:
    """Pro Tarif und Franchise genau eine Prämie wählen (Altersuntergruppe)."""
    best: dict[tuple[int, str], sqlite3.Row] = {}
    for r in rows:
        ug = r["altersuntergruppe"]
        if a.altersuntergruppe is not None:
            if ug != a.altersuntergruppe:
                continue
        elif not a.kinderrabatt and ug not in (None, "K1"):
            continue
        key = (r["tarif_id"], r["franchise"])
        if key not in best or r["betrag"] < best[key]["betrag"]:
            best[key] = r
    return best


def _angebot(r: sqlite3.Row) -> dict:
    monat = round(r["betrag"], 2)
    return {
        "tarif_id": r["tarif_id"], "bag_nr": r["bag_nr"], "versicherer": r["name_kurz"], "gruppe": r["gruppe"],
        "tarif_code": r["tarif_code"], "tarifbezeichnung": r["bezeichnung_de"], "tariftyp": r["tariftyp"],
        "modell": r["modell"], "altersuntergruppe": r["altersuntergruppe"],
        "rabatt": r["altersuntergruppe"] not in (None, "K1"),
        "franchise": r["franchise"], "monat": monat, "jahr": round(monat * 12, 2),
    }


def praemien(con: sqlite3.Connection, a: Anfrage, aktueller_versicherer: int | None = None,
             aktueller_tarif: int | None = None, aktuelle_praemie: float | None = None) -> dict:
    g = _pruefe(con, a)
    best = _waehle_untergruppe(_angebote_roh(con, a, g, a.franchise), a)
    angebote = sorted((_angebot(r) for r in best.values()),
                      key=lambda x: (x["monat"], x["versicherer"].lower(), x["tarifbezeichnung"]))

    # Referenz «aktueller Versicherer»: gewählter Tarif, sonst dessen Standardmodell
    aktuell = None
    hinweis = None
    if aktuelle_praemie is not None:
        aktuell = {"quelle": "eingegeben", "monat": round(aktuelle_praemie, 2), "jahr": round(aktuelle_praemie * 12, 2)}
    elif aktueller_versicherer is not None or aktueller_tarif is not None:
        ref = None
        if aktueller_tarif is not None:
            ref = next((x for x in angebote if x["tarif_id"] == aktueller_tarif), None)
        if ref is None and aktueller_versicherer is not None:
            ref = next((x for x in angebote if x["bag_nr"] == aktueller_versicherer and x["tariftyp"] == "TAR-BASE"), None)
            if ref is None:
                # Standardmodell ausgefiltert (Modellfilter) -> separat nachschlagen
                a_base = Anfrage(**{**a.__dict__, "modell": "TAR-BASE"})
                base = _waehle_untergruppe(_angebote_roh(con, a_base, g, a.franchise), a_base)
                ref = next((_angebot(r) for r in base.values() if r["bag_nr"] == aktueller_versicherer), None)
        if ref is None:
            hinweis = "Für den aktuellen Versicherer gibt es mit diesen Angaben kein Angebot in Ihrer Gemeinde."
        else:
            aktuell = {"quelle": "tarif", **ref}

    guenstigste = angebote[0]["monat"] if angebote else None
    for i, x in enumerate(angebote, start=1):
        x["rang"] = i
        x["diff_guenstigste_monat"] = round(x["monat"] - guenstigste, 2)
        x["diff_guenstigste_jahr"] = round(x["diff_guenstigste_monat"] * 12, 2)
        if aktuell is not None:
            x["diff_aktuell_monat"] = round(x["monat"] - aktuell["monat"], 2)
            x["diff_aktuell_jahr"] = round(x["diff_aktuell_monat"] * 12, 2)
            x["ist_aktuell"] = aktuell.get("tarif_id") == x["tarif_id"]
        else:
            x["diff_aktuell_monat"] = x["diff_aktuell_jahr"] = None
            x["ist_aktuell"] = False

    return {
        "kontext": {
            "gemeinde": g, "altersklasse": a.altersklasse, "mit_unfall": a.mit_unfall,
            "franchise": a.franchise, "franchise_betrag": int(a.franchise.split("-")[1]),
            "modell": a.modell, "kinderrabatt": a.kinderrabatt, "praemienjahr": praemienjahr(con),
        },
        "anzahl": len(angebote),
        "guenstigste_monat": guenstigste,
        "aktuell": aktuell,
        "hinweis_aktuell": hinweis,
        "angebote": angebote,
    }


def angebot_finden(con: sqlite3.Connection, a: Anfrage, tarif_id: int) -> tuple[dict | None, dict]:
    """Ein einzelnes Angebot serverseitig nachrechnen (z.B. für Leads)."""
    res = praemien(con, a)
    return next((x for x in res["angebote"] if x["tarif_id"] == tarif_id), None), res


# --------------------------------------------------------------------------
# Gesamtkosten
# --------------------------------------------------------------------------

def kostenbeteiligung(kosten: float, franchise: float, max_selbstbehalt: float) -> tuple[float, float]:
    """(Franchiseanteil, Selbstbehalt) bei erwarteten Gesundheitskosten."""
    kosten = max(0.0, kosten)
    fr = min(kosten, franchise)
    sb = min(SELBSTBEHALT_SATZ * max(0.0, kosten - franchise), max_selbstbehalt)
    return round(fr, 2), round(sb, 2)


def gesamtkosten(con: sqlite3.Connection, a: Anfrage, kosten: float, tarif_id: int | None = None) -> dict:
    """Jahresprämie + Franchise + Selbstbehalt für alle zulässigen Franchisen.

    Ohne `tarif_id` wird je Franchise das günstigste Angebot genommen,
    sonst der gewählte Tarif.
    """
    if kosten < 0 or kosten > 1_000_000:
        raise RechnerFehler("Erwartete Gesundheitskosten müssen zwischen 0 und 1'000'000 liegen")
    g = _pruefe(con, a)
    max_sb = MAX_SELBSTBEHALT[a.altersklasse]
    best = _waehle_untergruppe(_angebote_roh(con, a, g, None), a)
    franchisen = [dict(r) for r in con.execute("""
        SELECT f.code, f.betrag FROM franchise_stufe fs JOIN franchise f ON f.code = fs.franchise
        WHERE fs.altersklasse = ? ORDER BY f.betrag""", (a.altersklasse,))]
    zeilen = []
    for f in franchisen:
        kandidaten = [_angebot(r) for (tid, fr), r in best.items()
                      if fr == f["code"] and (tarif_id is None or tid == tarif_id)]
        if not kandidaten:
            zeilen.append({"franchise": f["code"], "franchise_betrag": f["betrag"], "angebot": None})
            continue
        ang = min(kandidaten, key=lambda x: (x["monat"], x["versicherer"]))
        fr_anteil, sb = kostenbeteiligung(kosten, f["betrag"], max_sb)
        zeilen.append({
            "franchise": f["code"], "franchise_betrag": f["betrag"], "angebot": ang,
            "jahrespraemie": ang["jahr"], "franchise_anteil": fr_anteil, "selbstbehalt": sb,
            "total": round(ang["jahr"] + fr_anteil + sb, 2),
        })
    mit = [z for z in zeilen if z["angebot"]]
    optimum = min(mit, key=lambda z: z["total"])["franchise"] if mit else None
    for z in zeilen:
        z["optimal"] = z["franchise"] == optimum
    return {"kosten": kosten, "max_selbstbehalt": max_sb, "tarif_id": tarif_id, "zeilen": zeilen, "optimal": optimum}
