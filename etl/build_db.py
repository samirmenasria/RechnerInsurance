"""ETL: BAG-Prämiendaten -> SQLite.

Aufruf (Prämienjahr als Parameter, Dateien werden im Datenordner gesucht):

    python -m etl.build_db --jahr 2026 --data data --out db/praemien_2026.sqlite

Die Datenbank wird zuerst in eine temporäre Datei geschrieben und erst nach
erfolgreicher Validierung an den Zielort verschoben. Bei kritischen Fehlern
bricht das Skript mit Exit-Code 1 ab; der Validierungsreport wird in jedem
Fall geschrieben.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from etl.parsers import (
    KANTONE,
    clean_text,
    lines_of,
    parse_anpassung,
    parse_kontakt,
    parse_taetigkeit,
    split_names,
)

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = Path(__file__).with_name("schema.sql")

# Sonderkantone der Prämiendaten ohne Gemeinden (Entsandte / Rheinschiffer).
# Sie sind über die Wohngemeinde nicht erreichbar und werden bewusst nicht importiert.
SONDERKANTONE = {"ZE": "Entsandte", "ZR": "Rheinschiffer"}

KANTON_NAMEN = {
    "AG": ("Aargau", "Argovie", "Argovia"), "AI": ("Appenzell Innerrhoden", "Appenzell Rhodes-Intérieures", "Appenzello Interno"),
    "AR": ("Appenzell Ausserrhoden", "Appenzell Rhodes-Extérieures", "Appenzello Esterno"), "BE": ("Bern", "Berne", "Berna"),
    "BL": ("Basel-Landschaft", "Bâle-Campagne", "Basilea Campagna"), "BS": ("Basel-Stadt", "Bâle-Ville", "Basilea Città"),
    "FR": ("Freiburg", "Fribourg", "Friburgo"), "GE": ("Genf", "Genève", "Ginevra"), "GL": ("Glarus", "Glaris", "Glarona"),
    "GR": ("Graubünden", "Grisons", "Grigioni"), "JU": ("Jura", "Jura", "Giura"), "LU": ("Luzern", "Lucerne", "Lucerna"),
    "NE": ("Neuenburg", "Neuchâtel", "Neuchâtel"), "NW": ("Nidwalden", "Nidwald", "Nidvaldo"), "OW": ("Obwalden", "Obwald", "Obvaldo"),
    "SG": ("St. Gallen", "Saint-Gall", "San Gallo"), "SH": ("Schaffhausen", "Schaffhouse", "Sciaffusa"),
    "SO": ("Solothurn", "Soleure", "Soletta"), "SZ": ("Schwyz", "Schwytz", "Svitto"), "TG": ("Thurgau", "Thurgovie", "Turgovia"),
    "TI": ("Tessin", "Tessin", "Ticino"), "UR": ("Uri", "Uri", "Uri"), "VD": ("Waadt", "Vaud", "Vaud"),
    "VS": ("Wallis", "Valais", "Vallese"), "ZG": ("Zug", "Zoug", "Zugo"), "ZH": ("Zürich", "Zurich", "Zurigo"),
}

ALTERSKLASSEN = {
    # code: (von, bis, DE, FR, IT, sort)
    "AKL-KIN": (0, 18, "Kinder (0–18)", "Enfants (0–18)", "Bambini (0–18)", 1),
    "AKL-JUG": (19, 25, "Junge Erwachsene (19–25)", "Jeunes adultes (19–25)", "Giovani adulti (19–25)", 2),
    "AKL-ERW": (26, None, "Erwachsene (ab 26)", "Adultes (dès 26)", "Adulti (da 26)", 3),
}

# Altersuntergruppen gemäss Erläuterungen. In den Prämiendaten 2026 kommen nur
# K1, K3, K4 und K5 vor (J1/E1 nie; Jugendliche/Erwachsene haben keine Untergruppe).
# K1 ist die reguläre Kinderprämie; K3–K5 sind tiefere, versichererspezifische
# Kinderprämien (Rabattstufen, z.B. für weitere Kinder derselben Familie). Die
# genaue Bedeutung liefert die Datei «Tarife <Jahr>» (Kategorie ALT) je Versicherer.
ALTERSUNTERGRUPPEN = {
    "K1": ("AKL-KIN", "Kind", "Enfant", "Bambino", 1),
    "K2": ("AKL-KIN", "Kind, Rabattstufe K2", "Enfant, niveau de rabais K2", "Bambino, livello di sconto K2", 0),
    "K3": ("AKL-KIN", "Kind, Rabattstufe K3", "Enfant, niveau de rabais K3", "Bambino, livello di sconto K3", 0),
    "K4": ("AKL-KIN", "Kind, Rabattstufe K4", "Enfant, niveau de rabais K4", "Bambino, livello di sconto K4", 0),
    "K5": ("AKL-KIN", "Kind, Rabattstufe K5", "Enfant, niveau de rabais K5", "Bambino, livello di sconto K5", 0),
    "J1": ("AKL-JUG", "Junge Erwachsene", "Jeunes adultes", "Giovani adulti", 1),
    "E1": ("AKL-ERW", "Erwachsene", "Adultes", "Adulti", 1),
}

TARIFTYPEN = {
    "TAR-BASE": ("Standardmodell", "Modèle standard", "Modello standard", 1),
    "TAR-HAM": ("Hausarztmodell", "Modèle du médecin de famille", "Modello del medico di famiglia", 2),
    "TAR-HMO": ("HMO", "HMO", "HMO", 3),
    "TAR-DIV": ("Telmed / andere Modelle", "Télémédecine / autres modèles", "Telemedicina / altri modelli", 4),
}

UNFALL = {
    "MIT-UNF": (1, "Mit Unfalldeckung", "Avec couverture accidents", "Con copertura infortuni"),
    "OHN-UNF": (0, "Ohne Unfalldeckung", "Sans couverture accidents", "Senza copertura infortuni"),
}

# Einträge, bei denen die Namensheuristik wegen zerschnittener Excel-Zellen versagt.
NAME_OVERRIDES = {
    1507: {"de": "AMB Versicherungen AG", "fr": "AMB Assurances SA", "it": "AMB Assicurazioni SA"},
}

PRAEMIEN_SPALTEN = [
    "Versicherer", "Kanton", "Hoheitsgebiet", "Geschäftsjahr", "Erhebungsjahr", "Region",
    "Altersklasse", "Unfalleinschluss", "Tarif", "Tariftyp", "Altersuntergruppe",
    "Franchisestufe", "Franchise", "Prämie", "isBaseP", "isBaseF", "Tarifbezeichnung",
]


# ==========================================================================
# Validierungsreport
# ==========================================================================

@dataclass
class Report:
    jahr: int
    kritisch: list[str] = field(default_factory=list)
    warnungen: list[str] = field(default_factory=list)
    hinweise: list[str] = field(default_factory=list)
    kennzahlen: dict[str, object] = field(default_factory=dict)

    def critical(self, msg: str) -> None:
        self.kritisch.append(msg)

    def warn(self, msg: str) -> None:
        self.warnungen.append(msg)

    def info(self, msg: str) -> None:
        self.hinweise.append(msg)

    def to_markdown(self) -> str:
        out = [f"# Validierungsreport Prämiendaten {self.jahr}", "",
               f"Erstellt: {dt.datetime.now().isoformat(timespec='seconds')}", "",
               f"**Status: {'ABGEBROCHEN – kritische Fehler' if self.kritisch else 'OK'}**", "",
               "## Kennzahlen", "", "| Kennzahl | Wert |", "|---|---|"]
        out += [f"| {k} | {v} |" for k, v in self.kennzahlen.items()]
        for title, items in (("Kritische Fehler", self.kritisch), ("Warnungen", self.warnungen), ("Hinweise", self.hinweise)):
            out += ["", f"## {title} ({len(items)})", ""]
            out += [f"- {m}" for m in items] or ["- keine"]
        return "\n".join(out) + "\n"


# ==========================================================================
# Einlesen
# ==========================================================================

def _engine() -> str:
    try:
        import python_calamine  # noqa: F401
        return "calamine"
    except ImportError:
        return "openpyxl"


def read_excel(path: Path, **kw) -> pd.DataFrame | dict[str, pd.DataFrame]:
    return pd.read_excel(path, engine=_engine(), **kw)


def find_file(data_dir: Path, explicit: str | None, patterns: list[str], required: bool = True) -> Path | None:
    if explicit:
        p = Path(explicit)
        if not p.exists():
            raise SystemExit(f"Datei nicht gefunden: {p}")
        return p
    for pattern in patterns:
        hits = sorted(p for p in data_dir.glob(pattern) if not p.name.startswith("~$"))
        if hits:
            return hits[0]
    if required:
        raise SystemExit(f"Keine Datei für {patterns} in {data_dir} gefunden")
    return None


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def load_praemien(path: Path) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path, dtype={"Tarif": str, "Altersuntergruppe": str}), None
    sheets = read_excel(path, sheet_name=None)
    export = sheets.get("Export")
    if export is None:
        export = next(iter(sheets.values()))
    return export, sheets.get("Wertebereiche")


def _header_row(df: pd.DataFrame, first_cell_prefix: str) -> int:
    for i, v in enumerate(df.iloc[:, 0].map(str)):
        if v.strip().startswith(first_cell_prefix):
            return i
    raise ValueError(f"Kopfzeile «{first_cell_prefix}» nicht gefunden")


def load_regionen(path: Path) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    raw = read_excel(path, sheet_name=None, header=None)
    a = raw["A_COM"]
    h = _header_row(a, "BFS")
    a = a.iloc[h + 1:, :7].copy()
    a.columns = ["bfs_nr", "kanton", "gemeinde", "region_nr", "bezirk", "plz", "ort"]
    a = a.dropna(subset=["bfs_nr"])
    a[["bfs_nr", "region_nr", "plz"]] = a[["bfs_nr", "region_nr", "plz"]].astype(int)
    for c in ("kanton", "gemeinde", "bezirk", "ort"):
        a[c] = a[c].astype(str).str.strip()

    b = raw.get("B_NPA")
    if b is not None:
        h = _header_row(b.iloc[:, 1:].rename(columns=lambda c: c - 1), "PLZ")
        b = b.iloc[h + 1:, 1:8].copy()
        b.columns = ["plz", "ort", "kanton", "region_nr", "bfs_nr", "gemeinde", "bezirk"]
        b = b.dropna(subset=["bfs_nr"])
        b[["bfs_nr", "region_nr", "plz"]] = b[["bfs_nr", "region_nr", "plz"]].astype(int)

    info = raw.get("Informationen")
    titel = str(info.iloc[0, 0]) + " " + str(info.iloc[1, 0]) if info is not None else ""
    return a, b, titel


def load_versicherer(path: Path) -> tuple[list[dict], dict[int, str], list[dict]]:
    sheets = read_excel(path, sheet_name=None, header=None)
    main_name = next(n for n in sheets if n.lower().startswith("zugelassene"))
    df = sheets[main_name]
    h = next(i for i, v in enumerate(df.iloc[:, 0].map(str)) if v.startswith("Nummer"))
    records: list[list[list[str]]] = []
    for _, row in df.iloc[h + 1:, :7].iterrows():
        vals = list(row.values)
        if pd.notna(vals[0]) and str(vals[0]).strip():
            records.append([[] if pd.isna(v) else [str(v)] for v in vals])
        elif records:  # Folgezeile eines über mehrere Excel-Zeilen verteilten Eintrags
            for i, v in enumerate(vals):
                if pd.notna(v) and str(v).strip():
                    records[-1][i].append(str(v))

    result = []
    for rec in records:
        cols = ["\n".join(c) for c in rec]
        nr_raw = cols[0].strip()
        nr = int(re.match(r"\d+", nr_raw).group())
        names = NAME_OVERRIDES.get(nr) or split_names(cols[2])
        kontakt = parse_kontakt(cols[3])
        taet = parse_taetigkeit(cols[6])
        gruppe = clean_text(cols[5])
        gruppe = None if gruppe in ("", "---") else re.sub(r"\s*/.*?\)", ")", gruppe.replace("\n", " "))
        rechtsform = lines_of(cols[4])
        result.append({
            "bag_nr": nr,
            "uid": cols[1].strip() or None,
            "name_de": names["de"], "name_fr": names["fr"], "name_it": names["it"],
            "strasse": kontakt.strasse, "postfach": kontakt.postfach, "plz": kontakt.plz, "ort": kontakt.ort,
            "tel": kontakt.tel, "fax": kontakt.fax, "email": kontakt.email, "web": kontakt.web,
            "adresse_raw": clean_text(cols[3]),
            "rechtsform": rechtsform[0] if rechtsform else None,
            "gruppe": gruppe,
            "taetigkeitsgebiet_raw": clean_text(cols[6]),
            "nur_taggeld": int("x" in nr_raw.lower() or taet.nur_beruf),
            "_taetigkeit": taet,
        })

    # Kurznamen aus dem Index
    kurz: dict[int, str] = {}
    idx_name = next((n for n in sheets if n.lower().startswith("index")), None)
    if idx_name:
        idx = sheets[idx_name]
        for _, row in idx.iterrows():
            nr, name = row.iloc[1], row.iloc[3] if len(row) > 3 else None
            if isinstance(nr, (int, float)) and pd.notna(nr) and pd.notna(name):
                kurz[int(nr)] = clean_text(name)

    anpassungen = []
    anp_name = next((n for n in sheets if n.lower().startswith("anpassung")), None)
    if anp_name:
        anp = sheets[anp_name]
        h = next(i for i, v in enumerate(anp.iloc[:, 0].map(str)) if v.startswith("Nummer"))
        for _, row in anp.iloc[h + 1:].iterrows():
            if pd.isna(row.iloc[0]):
                continue
            parsed = parse_anpassung(row.iloc[3])
            anpassungen.append({"bag_nr": int(row.iloc[0]), "name": clean_text(row.iloc[2]).replace("\n", " "),
                                "text_raw": clean_text(row.iloc[3]), **parsed})
    return result, kurz, anpassungen


def load_optional_table(path: Path | None) -> pd.DataFrame | None:
    if path is None:
        return None
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path, dtype=str)
    return read_excel(path, dtype=str)


# ==========================================================================
# Transformation + Validierung
# ==========================================================================

def _region_nr(code) -> int | None:
    m = re.search(r"(\d)\s*$", str(code))
    return int(m.group(1)) if m else None


def validate_praemien(df: pd.DataFrame, jahr: int, rep: Report) -> pd.DataFrame:
    missing_cols = [c for c in PRAEMIEN_SPALTEN if c not in df.columns]
    if missing_cols:
        rep.critical(f"Prämiendatei: Spalten fehlen: {missing_cols}")
        return df
    rep.kennzahlen["Prämienzeilen (Datei)"] = len(df)

    jahre = sorted(df["Geschäftsjahr"].dropna().unique().tolist())
    if jahre != [jahr]:
        rep.critical(f"Geschäftsjahr in der Prämiendatei ist {jahre}, erwartet {jahr}")
    rep.kennzahlen["Erhebungsjahr"] = ",".join(str(int(j)) for j in df["Erhebungsjahr"].dropna().unique())

    ausland = df["Hoheitsgebiet"] != "CH"
    if ausland.any():
        rep.info(f"{int(ausland.sum())} Zeilen mit Hoheitsgebiet ≠ CH ignoriert")
        df = df[~ausland]

    sonder = df["Kanton"].isin(SONDERKANTONE)
    if sonder.any():
        detail = ", ".join(f"{k} ({SONDERKANTONE[k]}): {int((df['Kanton'] == k).sum())}" for k in SONDERKANTONE)
        rep.info(f"Sonderkantone nicht importiert (keine Wohngemeinde, im Rechner nicht auswählbar) – {detail} Zeilen")
        df = df[~sonder]

    required = ["Versicherer", "Kanton", "Region", "Altersklasse", "Unfalleinschluss", "Tarif",
                "Tariftyp", "Franchise", "Prämie", "Tarifbezeichnung"]
    for col in required:
        n = int(df[col].isna().sum())
        if n:
            rep.critical(f"{n} Zeilen ohne Wert in Spalte «{col}»")

    checks = {
        "Kanton": set(KANTONE),
        "Altersklasse": set(ALTERSKLASSEN),
        "Unfalleinschluss": set(UNFALL),
        "Tariftyp": set(TARIFTYPEN),
    }
    for col, allowed in checks.items():
        unknown = sorted(set(df[col].dropna()) - allowed)
        if unknown:
            rep.critical(f"Unbekannte Codes in «{col}»: {unknown}")
    unk = sorted(set(df["Altersuntergruppe"].dropna()) - set(ALTERSUNTERGRUPPEN))
    if unk:
        rep.critical(f"Unbekannte Codes in «Altersuntergruppe»: {unk}")
    bad_reg = sorted(r for r in df["Region"].dropna().unique() if not re.fullmatch(r"PR-REG CH[0-3]", str(r)))
    if bad_reg:
        rep.critical(f"Unbekannte Codes in «Region»: {bad_reg}")
    bad_fra = sorted(f for f in df["Franchise"].dropna().unique() if not re.fullmatch(r"FRA-\d+", str(f)))
    if bad_fra:
        rep.critical(f"Unbekannte Codes in «Franchise»: {bad_fra}")

    nonpos = df["Prämie"].isna() | (df["Prämie"] <= 0)
    if nonpos.any():
        rep.critical(f"{int(nonpos.sum())} Prämien ≤ 0 oder leer")

    key = ["Versicherer", "Kanton", "Region", "Altersklasse", "Altersuntergruppe", "Unfalleinschluss", "Tarif", "Franchise"]
    dup = df.duplicated(key, keep=False)
    if dup.any():
        rep.critical(f"{int(dup.sum())} doppelte Prämienzeilen (Schlüssel {key})")

    tcons = df.groupby(["Versicherer", "Tarif"]).agg(t=("Tariftyp", "nunique"), b=("Tarifbezeichnung", "nunique"))
    bad = tcons[(tcons.t > 1) | (tcons.b > 1)]
    if len(bad):
        rep.critical(f"Tarife mit widersprüchlichem Tariftyp/Bezeichnung: {bad.index.tolist()}")

    stufen = df.groupby(["Altersklasse", "Franchise"])["Franchisestufe"].nunique()
    if (stufen > 1).any():
        rep.critical(f"Franchisestufe nicht eindeutig je Altersklasse/Franchise: {stufen[stufen > 1].index.tolist()}")

    rep.kennzahlen["Prämienzeilen (importiert)"] = len(df)
    rep.kennzahlen["Versicherer mit Prämien"] = df["Versicherer"].nunique()
    ug = df.groupby("Altersklasse")["Altersuntergruppe"].apply(lambda s: sorted(s.dropna().unique()))
    rep.info("Vorkommende Altersuntergruppen: " + "; ".join(f"{k}: {v or 'keine'}" for k, v in ug.items()))
    return df


def build(args: argparse.Namespace) -> int:
    jahr = args.jahr
    data_dir = Path(args.data)
    out = Path(args.out or ROOT / "db" / f"praemien_{jahr}.sqlite")
    report_path = Path(args.report or out.with_name(f"validierung_{jahr}.md"))
    rep = Report(jahr)

    f_praemien = find_file(data_dir, args.praemien, [f"*gesamtbericht*{jahr}*.xlsx", "*gesamtbericht*.xlsx", "*gesamtbericht*.csv", f"*Praemien*CH*{jahr}*.csv"])
    f_versicherer = find_file(data_dir, args.versicherer, [f"*Krankenversicherer*{jahr}*.xlsx", "*Krankenversicherer*.xlsx"])
    f_regionen = find_file(data_dir, args.regionen, [f"*praemienregionen*{jahr}*.xlsx", "*praemienregionen*.xlsx", "*Praemienregionen*.xlsx"])
    f_tarife = find_file(data_dir, args.tarife, [f"*Tarife*{jahr}*.xlsx", f"*Tarife*{jahr}*.csv", "*Tarife*.xlsx", "*Tarife*.csv"], required=False)
    f_einzug = find_file(data_dir, args.einzugsgebiete, [f"*Einzugsgebiete*{jahr}*.xlsx", f"*Einzugsgebiete*{jahr}*.csv", "*Einzugsgebiete*.xlsx", "*Einzugsgebiete*.csv"], required=False)

    print(f"[ETL] Prämienjahr {jahr}")
    for label, f in (("Prämien", f_praemien), ("Versicherer", f_versicherer), ("Regionen", f_regionen),
                     ("Tarife", f_tarife), ("Einzugsgebiete", f_einzug)):
        print(f"  {label:15s} {f if f else '— (nicht vorhanden)'}")
    if f_tarife is None:
        rep.warn("Optionale Datei «Tarife» fehlt – Tarifnamen nur DE (Tarifbezeichnung), Rabattstufen der Kinder ohne Bezeichnung")
    if f_einzug is None:
        rep.warn("Optionale Datei «Einzugsgebiete» fehlt – auf Gemeindeebene eingeschränkte HMO-/Hausarztmodelle werden nicht gefiltert")

    # ---------------------------------------------------------------- Prämien
    praemien, wertebereiche = load_praemien(f_praemien)
    praemien = validate_praemien(praemien, jahr, rep)

    # ---------------------------------------------------------------- Regionen / Gemeinden
    acom, bnpa, reg_titel = load_regionen(f_regionen)
    if str(jahr) not in reg_titel:
        rep.warn(f"Prämienregionen-Datei scheint nicht für {jahr} zu gelten: «{reg_titel.strip()}»")
    rep.kennzahlen["Prämienregionen-Datei"] = reg_titel.replace("\n", " ").strip()
    if bnpa is not None:
        s_a = set(map(tuple, acom[["plz", "bfs_nr"]].drop_duplicates().values))
        s_b = set(map(tuple, bnpa[["plz", "bfs_nr"]].drop_duplicates().values))
        if s_a != s_b:
            rep.warn(f"A_COM und B_NPA unterscheiden sich in {len(s_a ^ s_b)} PLZ-Gemeinde-Paaren; verwendet wird A_COM")

    gem = acom.drop_duplicates("bfs_nr")[["bfs_nr", "gemeinde", "kanton", "bezirk", "region_nr"]]
    inkons = acom.groupby("bfs_nr")[["region_nr", "kanton"]].nunique()
    if (inkons > 1).any().any():
        rep.critical(f"Gemeinden mit mehreren Regionen/Kantonen: {inkons[(inkons > 1).any(axis=1)].index.tolist()}")
    unk_kt = sorted(set(gem.kanton) - set(KANTONE))
    if unk_kt:
        rep.critical(f"Unbekannte Kantone in Prämienregionen: {unk_kt}")
    rep.kennzahlen["Gemeinden"] = len(gem)
    rep.kennzahlen["PLZ"] = acom["plz"].nunique()
    plz_multi = acom.drop_duplicates(["plz", "bfs_nr"]).groupby("plz").agg(
        g=("bfs_nr", "nunique"), r=("region_nr", "nunique"), k=("kanton", "nunique"))
    rep.kennzahlen["PLZ mit mehreren Gemeinden"] = int((plz_multi.g > 1).sum())
    rep.kennzahlen["PLZ mit mehreren Prämienregionen/Kantonen"] = int(((plz_multi.r > 1) | (plz_multi.k > 1)).sum())

    gem_regions = set(zip(gem.kanton, gem.region_nr))
    praemien = praemien.assign(region_nr=praemien["Region"].map(_region_nr))
    prem_regions = set(zip(praemien.Kanton, praemien.region_nr))
    for kt, nr in sorted(gem_regions - prem_regions):
        n = int(((gem.kanton == kt) & (gem.region_nr == nr)).sum())
        rep.critical(f"Region {kt} {nr} hat {n} Gemeinden, aber keine Prämien")
    for kt, nr in sorted(prem_regions - gem_regions):
        rep.critical(f"Region {kt} {nr} hat Prämien, aber keine Gemeindezuordnung")
    kt_ohne = sorted(set(KANTONE) - set(gem.kanton))
    if kt_ohne:
        rep.critical(f"Kantone ohne Gemeindezuordnung: {kt_ohne}")

    # ---------------------------------------------------------------- Versicherer
    versicherer, kurz, anpassungen = load_versicherer(f_versicherer)
    vers_nrs = {v["bag_nr"] for v in versicherer}
    ohne_stamm = sorted(set(praemien["Versicherer"].unique()) - vers_nrs)
    if ohne_stamm:
        rep.critical(f"Versicherer mit Prämien, aber ohne Stammdaten: {ohne_stamm}")
    ohne_praemien = sorted(v["bag_nr"] for v in versicherer if v["bag_nr"] not in set(praemien["Versicherer"]) and not v["nur_taggeld"])
    if ohne_praemien:
        rep.warn(f"OKP-Versicherer ohne Prämien: {ohne_praemien}")
    for v in versicherer:
        v["name_kurz"] = kurz.get(v["bag_nr"]) or v["name_de"]
        if v["bag_nr"] not in kurz:
            rep.warn(f"Versicherer {v['bag_nr']} fehlt im Index – Kurzname = voller Name")
    rep.kennzahlen["Versicherer (Stammdaten)"] = len(versicherer)
    rep.kennzahlen["davon reine Taggeldversicherer"] = sum(v["nur_taggeld"] for v in versicherer)
    for a in anpassungen:
        if a["art"] in ("fusion", "entzug") and a["bag_nr"] in set(praemien["Versicherer"]):
            rep.critical(f"Versicherer {a['bag_nr']} ({a['name']}) hat Prämien, ist aber per {a['datum']} {a['art']}")
        rep.info(f"Anpassung: {a['bag_nr']} {a['name']} – {a['art']} {a['datum'] or ''}"
                 + (f" → {a['ziel_bag_nr']}" if a["ziel_bag_nr"] else ""))

    taet_rows = []
    for v in versicherer:
        for kt, nr in v["_taetigkeit"].eintraege():
            taet_rows.append((v["bag_nr"], kt, nr))
    taet_set = {(b, k, n) for b, k, n in taet_rows}
    ausserhalb = set()
    for b, kt, nr in praemien[["Versicherer", "Kanton", "region_nr"]].drop_duplicates().itertuples(index=False):
        if (b, kt, None) not in taet_set and (b, kt, nr) not in taet_set:
            ausserhalb.add((int(b), kt, int(nr)))
    if ausserhalb:
        rep.warn(f"Prämien ausserhalb des Tätigkeitsgebiets (werden im Rechner ausgeblendet): {sorted(ausserhalb)}")

    # ---------------------------------------------------------------- Tarife
    tarife = (praemien.groupby(["Versicherer", "Tarif"])
              .agg(tariftyp=("Tariftyp", "first"), bez=("Tarifbezeichnung", "first")).reset_index())
    rep.kennzahlen["Tarife (Versicherer × Tarif-ID)"] = len(tarife)
    tarif_namen: dict[tuple[int, str], tuple[str | None, str | None]] = {}
    ug_namen: list[tuple] = []
    tdf = load_optional_table(f_tarife)
    if tdf is not None:
        tdf.columns = [c.strip() for c in tdf.columns]
        for _, r in tdf.iterrows():
            try:
                key = (int(r["Versicherer"]), str(r["Tarif"]).strip())
            except (KeyError, ValueError):
                continue
            if str(r.get("Kategorie", "")).strip() == "ALT":
                ug_namen.append((key[0], key[1], r.get("Name_DE"), r.get("Name_FR"), r.get("Name_IT")))
            else:
                tarif_namen[key] = (r.get("Name_FR"), r.get("Name_IT"))
        rep.info(f"Tarife-Datei: {len(tarif_namen)} Modellnamen, {len(ug_namen)} Altersuntergruppen-Bezeichnungen")

    # ---------------------------------------------------------------- Schreiben
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    cur = con.cursor()

    # Kantonsnamen DE/FR aus Wertebereichen (falls vorhanden), IT aus fester Liste
    wb_kt = {}
    if wertebereiche is not None:
        for _, r in wertebereiche[wertebereiche["DimensionDE"] == "Kantone OKP CH"].iterrows():
            wb_kt[r["Akro"]] = (clean_text(r["BezeichnungDE"]), clean_text(r["BezeichnungFR"]))
    hat_regionen = gem.groupby("kanton")["region_nr"].max().gt(0).to_dict()
    cur.executemany("INSERT INTO kanton VALUES (?,?,?,?,?)", [
        (kt, KANTON_NAMEN[kt][0], wb_kt.get(kt, KANTON_NAMEN[kt])[1], KANTON_NAMEN[kt][2], int(hat_regionen.get(kt, False)))
        for kt in KANTONE])

    region_ids: dict[tuple[str, int], int] = {}
    for i, (kt, nr) in enumerate(sorted(gem_regions | prem_regions), start=1):
        region_ids[(kt, nr)] = i
        cur.execute("INSERT INTO region VALUES (?,?,?,?)", (i, kt, int(nr), f"PR-REG CH{nr}"))

    cur.executemany("INSERT INTO gemeinde VALUES (?,?,?,?,?)", [
        (int(r.bfs_nr), r.gemeinde, r.kanton, r.bezirk, region_ids[(r.kanton, r.region_nr)])
        for r in gem.itertuples()])
    cur.executemany("INSERT OR IGNORE INTO plz_gemeinde VALUES (?,?,?)",
                    [(int(r.plz), r.ort, int(r.bfs_nr)) for r in acom.itertuples()])

    cur.executemany(
        "INSERT INTO versicherer VALUES (:bag_nr,:uid,:name_kurz,:name_de,:name_fr,:name_it,:strasse,:postfach,:plz,:ort,"
        ":tel,:fax,:email,:web,:adresse_raw,:rechtsform,:gruppe,:taetigkeitsgebiet_raw,:nur_taggeld,1)",
        [{k: v for k, v in rec.items() if not k.startswith("_")} for rec in versicherer])
    cur.executemany("INSERT OR IGNORE INTO versicherer_taetigkeit VALUES (?,?,?)", taet_rows)
    cur.executemany("INSERT INTO versicherer_anpassung VALUES (:bag_nr,:name,:art,:datum,:ziel_bag_nr,:text_raw)", anpassungen)

    # Codes
    for code, (von, bis, de, fr, it, sort) in ALTERSKLASSEN.items():
        if wertebereiche is not None:
            wb = wertebereiche[(wertebereiche["DimensionDE"] == "Altersklassen") & (wertebereiche["Akro"] == code)]
            if len(wb):
                m = re.match(r"\s*(\d+)\s*-\s*(\d+)?", str(wb.iloc[0]["BezeichnungDE"]))
                if m and (int(m.group(1)), int(m.group(2)) if m.group(2) else None) != (von, bis):
                    rep.warn(f"Altersgrenzen {code} in Wertebereichen weichen ab: {wb.iloc[0]['BezeichnungDE']}")
        cur.execute("INSERT INTO altersklasse VALUES (?,?,?,?,?,?,?)", (code, von, bis, de, fr, it, sort))
    for code, (akl, de, fr, it, std) in ALTERSUNTERGRUPPEN.items():
        cur.execute("INSERT INTO altersuntergruppe VALUES (?,?,?,?,?,?)", (code, akl, de, fr, it, std))
    fra = praemien[["Altersklasse", "Franchise", "Franchisestufe", "isBaseF"]].drop_duplicates(["Altersklasse", "Franchise"])
    cur.executemany("INSERT INTO franchise VALUES (?,?)",
                    [(f, int(f.split("-")[1])) for f in sorted(fra["Franchise"].unique(), key=lambda f: int(f.split("-")[1]))])
    ord_f = praemien[praemien.isBaseF == 1].groupby("Altersklasse")["Franchise"].unique()
    cur.executemany("INSERT INTO franchise_stufe VALUES (?,?,?,?)", [
        (r.Altersklasse, r.Franchise, r.Franchisestufe, int(r.Franchise in set(ord_f.get(r.Altersklasse, []))))
        for r in fra.itertuples()])
    for code, (mu, de, fr, it) in UNFALL.items():
        cur.execute("INSERT INTO unfalleinschluss VALUES (?,?,?,?,?)", (code, mu, de, fr, it))
    for code, (de, fr, it, sort) in TARIFTYPEN.items():
        cur.execute("INSERT INTO tariftyp VALUES (?,?,?,?,?)", (code, de, fr, it, sort))

    tarif_ids: dict[tuple[int, str], int] = {}
    for i, r in enumerate(tarife.itertuples(), start=1):
        key = (int(r.Versicherer), str(r.Tarif))
        tarif_ids[key] = i
        fr, it = tarif_namen.get(key, (None, None))
        cur.execute("INSERT INTO tarif VALUES (?,?,?,?,?,?,?)", (i, key[0], key[1], r.tariftyp, clean_text(r.bez), fr, it))

    if ug_namen:
        cur.executemany("INSERT OR REPLACE INTO versicherer_altersuntergruppe VALUES (?,?,?,?,?)", ug_namen)

    # Einzugsgebiete
    edf = load_optional_table(f_einzug)
    if edf is not None:
        edf.columns = [c.strip() for c in edf.columns]
        n_eg = n_eg_gem = 0
        for _, r in edf.iterrows():
            key = (int(r["Versicherer"]), str(r["Tarif"]).strip())
            if key not in tarif_ids:
                rep.warn(f"Einzugsgebiet für unbekannten Tarif {key}")
                continue
            kt = str(r["Kanton"]).strip()
            nr = _region_nr(r.get("Region", ""))
            rid = region_ids.get((kt, nr)) if nr is not None else None
            eing = int(str(r.get("Eingeschränkt", "N")).strip().upper() == "Y")
            cur.execute("INSERT INTO einzugsgebiet (tarif_id, kanton, region_id, eingeschraenkt) VALUES (?,?,?,?)",
                        (tarif_ids[key], kt, rid, eing))
            n_eg += 1
            eg_id = cur.lastrowid
            bfs = [int(x) for x in re.findall(r"\d+", str(r.get("Gemeinden-BFS", "") or "")) if x]
            cur.executemany("INSERT OR IGNORE INTO einzugsgebiet_gemeinde VALUES (?,?)", [(eg_id, b) for b in bfs])
            n_eg_gem += len(bfs)
        rep.kennzahlen["Einzugsgebiete"] = n_eg
        rep.kennzahlen["Einzugsgebiete: Gemeindezuordnungen"] = n_eg_gem

    # Faktentabelle
    rows = []
    for r in praemien.itertuples(index=False):
        ug = r.Altersuntergruppe if isinstance(r.Altersuntergruppe, str) else None
        bef = getattr(r, "befristet", None)
        rows.append((
            tarif_ids[(int(r.Versicherer), str(r.Tarif))], region_ids[(r.Kanton, int(r.region_nr))],
            r.Altersklasse, ug, UNFALL[r.Unfalleinschluss][0], r.Franchise, round(float(r.Prämie), 2),
            int(r.isBaseP), int(r.isBaseF), None if bef is None or pd.isna(bef) else str(bef)))
    try:
        cur.executemany("INSERT INTO praemie (tarif_id, region_id, altersklasse, altersuntergruppe, mit_unfall, franchise,"
                        " betrag, is_base_p, is_base_f, befristet) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    except sqlite3.IntegrityError as exc:
        rep.critical(f"Integritätsfehler beim Schreiben der Prämien: {exc}")

    fk = con.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        rep.critical(f"{len(fk)} Fremdschlüsselverletzungen, z.B. {fk[:3]}")

    meta = {
        "praemienjahr": jahr,
        "erhebungsjahr": rep.kennzahlen.get("Erhebungsjahr"),
        "quelle": "Bundesamt für Gesundheit BAG, Prämien der obligatorischen Krankenpflegeversicherung",
        "build_zeit": dt.datetime.now().isoformat(timespec="seconds"),
        "regionen_stand": rep.kennzahlen.get("Prämienregionen-Datei"),
        "einzugsgebiete_vorhanden": int(edf is not None),
        "tarifnamen_vorhanden": int(tdf is not None),
    }
    for label, f in (("datei_praemien", f_praemien), ("datei_versicherer", f_versicherer), ("datei_regionen", f_regionen),
                     ("datei_tarife", f_tarife), ("datei_einzugsgebiete", f_einzug)):
        if f:
            meta[label] = f"{f.name} (sha256 {sha256(f)})"
    cur.executemany("INSERT INTO meta VALUES (?,?)", [(k, None if v is None else str(v)) for k, v in meta.items()])
    con.commit()
    con.execute("ANALYZE")
    con.close()

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(rep.to_markdown(), encoding="utf-8")
    print(f"[ETL] Report: {report_path}")
    if rep.kritisch:
        tmp.unlink(missing_ok=True)
        print(f"[ETL] ABBRUCH – {len(rep.kritisch)} kritische Fehler:", file=sys.stderr)
        for m in rep.kritisch:
            print(f"  - {m}", file=sys.stderr)
        return 1
    os.replace(tmp, out)
    print(f"[ETL] OK – {len(rows)} Prämien, {len(gem)} Gemeinden, {len(versicherer)} Versicherer → {out}")
    for m in rep.warnungen:
        print(f"  Warnung: {m}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="BAG-Prämiendaten in SQLite laden")
    p.add_argument("--jahr", type=int, required=True, help="Prämienjahr, z.B. 2026")
    p.add_argument("--data", default=str(ROOT / "data"), help="Ordner mit den BAG-Dateien")
    p.add_argument("--out", help="Ziel-Datenbank (Standard: db/praemien_<jahr>.sqlite)")
    p.add_argument("--report", help="Pfad Validierungsreport (Standard: neben der DB)")
    p.add_argument("--praemien", help="Prämiendatei explizit (gesamtbericht_ch.xlsx / Prämien CH csv)")
    p.add_argument("--versicherer", help="Verzeichnis der zugelassenen Krankenversicherer")
    p.add_argument("--regionen", help="Prämienregionen-Datei")
    p.add_argument("--tarife", help="optional: Tarife <Jahr> (xlsx/csv)")
    p.add_argument("--einzugsgebiete", help="optional: Einzugsgebiete <Jahr> (xlsx/csv)")
    return build(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
