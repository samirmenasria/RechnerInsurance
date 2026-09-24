import pandas as pd

from app.rechner import kostenbeteiligung
from etl import build_db
from etl.parsers import clean_text, parse_anpassung, parse_kontakt, parse_taetigkeit, split_names


def test_clean_text_trennungen():
    assert clean_text("Atupri Gesundheits-\nversicherung AG") == "Atupri Gesundheitsversicherung AG"
    assert clean_text("CSS Kranken-\nVersicherung AG") == "CSS Kranken-Versicherung AG"
    assert clean_text("Unfallver-sicherung") == "Unfallversicherung"
    assert clean_text("CSS Assurance-maladie SA") == "CSS Assurance-maladie SA"
    assert clean_text("info@ksm-versicherung.ch") == "info@ksm-versicherung.ch"


def test_split_names_mehrsprachig():
    n = split_names("CSS Kranken-\nVersicherung AG\nCSS Assurance-maladie SA\nCSS Assicurazione malattie SA")
    assert n == {"de": "CSS Kranken-Versicherung AG", "fr": "CSS Assurance-maladie SA",
                 "it": "CSS Assicurazione malattie SA"}


def test_split_names_ueber_mehrere_excelzeilen():
    # Groupe Mutuel: Name über mehrere Excel-Zeilen verteilt
    raw = "Avenir Assurance\n\nMaladie SA\nAvenir Krankenver-\n\nsicherung AG\nAvenir Assicurazione\n\nMalattia SA"
    n = split_names(raw)
    assert n["de"] == "Avenir Krankenversicherung AG"
    assert n["fr"] == "Avenir Assurance Maladie SA"
    assert n["it"] == "Avenir Assicurazione Malattia SA"


def test_split_names_einsprachig():
    assert split_names("Krankenkasse Birchmeier") == {"de": "Krankenkasse Birchmeier"} | {
        "fr": "Krankenkasse Birchmeier", "it": "Krankenkasse Birchmeier"}


def test_parse_kontakt():
    k = parse_kontakt("Tribschenstrasse 21\nPostfach 2568\n6002 Luzern\nTel. 058 277 11 11\n"
                      "Fax 058 277 12 12\ncss.info@css.ch\nwww.css.ch\n")
    assert (k.strasse, k.postfach, k.plz, k.ort) == ("Tribschenstrasse 21", "Postfach 2568", "6002", "Luzern")
    assert (k.tel, k.fax, k.email, k.web) == ("058 277 11 11", "058 277 12 12", "css.info@css.ch", "https://www.css.ch")


def test_parse_kontakt_groupe_mutuel_ohne_email():
    k = parse_kontakt("Groupe Mutuel\n\nRue des Cèdres 5\n\n1919 Martigny\nTel. 0848 803 111\n"
                      "Kontakt / Contact / Contatto\nwww.groupemutuel.ch")
    assert k.strasse == "Rue des Cèdres 5" and k.plz == "1919" and k.email is None
    assert k.web == "https://www.groupemutuel.ch"


def test_parse_taetigkeit():
    t = parse_taetigkeit("CH, FL\n\nEU, EFTA, UK\nUE, AELE, UK")
    assert t.ganze_schweiz and len(t.eintraege()) == 26 and "FL" in t.ausland
    t = parse_taetigkeit("AG, AI, BE; GL\nVS: Region 2 / Région 2 / Regione 2\n")
    assert t.eintraege() == [("AG", None), ("AI", None), ("BE", None), ("GL", None), ("VS", 2)]
    t = parse_taetigkeit("AT, DE, ES, FR, GB")  # Ausland, obwohl FR auch ein Kanton ist
    assert not t.kantone and t.ausland
    assert parse_taetigkeit("Beruf\nProfession\n").nur_beruf


def test_parse_anpassung():
    assert parse_anpassung("01.01.2026 Zusammenschluss mit \nFusion avec\nFusione con\n1386 Galenos") == {
        "art": "fusion", "datum": "2026-01-01", "ziel_bag_nr": 1386}
    assert parse_anpassung("31.12.2025\nEntzug der Bewilligung")["art"] == "entzug"


def test_kostenbeteiligung():
    assert kostenbeteiligung(0, 300, 700) == (0, 0)
    assert kostenbeteiligung(200, 300, 700) == (200, 0)
    assert kostenbeteiligung(3000, 300, 700) == (300, 270)
    assert kostenbeteiligung(50_000, 2500, 700) == (2500, 700)       # Selbstbehalt gedeckelt
    assert kostenbeteiligung(50_000, 600, 350) == (600, 350)         # Kinder: max. 350


def _mini_df(**override):
    row = {"Versicherer": 8, "Kanton": "AG", "Hoheitsgebiet": "CH", "Geschäftsjahr": 2026, "Erhebungsjahr": 2025,
           "Region": "PR-REG CH0", "Altersklasse": "AKL-ERW", "Unfalleinschluss": "MIT-UNF", "Tarif": "BASE",
           "Tariftyp": "TAR-BASE", "Altersuntergruppe": None, "Franchisestufe": "FRAST1", "Franchise": "FRA-300",
           "Prämie": 400.0, "isBaseP": 1, "isBaseF": 1, "Tarifbezeichnung": "Grundversicherung"}
    row.update(override)
    return pd.DataFrame([row])


def test_validierung_kritische_fehler():
    rep = build_db.Report(2026)
    build_db.validate_praemien(_mini_df(), 2026, rep)
    assert rep.kritisch == []

    for df, erwartet in [
        (_mini_df(**{"Prämie": 0.0}), "Prämien ≤ 0"),
        (_mini_df(Geschäftsjahr=2025), "Geschäftsjahr"),
        (_mini_df(Altersklasse="AKL-XXX"), "Unbekannte Codes in «Altersklasse»"),
        (_mini_df(Region="PR-REG CH9"), "Unbekannte Codes in «Region»"),
        (pd.concat([_mini_df(), _mini_df()]), "doppelte Prämienzeilen"),
    ]:
        rep = build_db.Report(2026)
        build_db.validate_praemien(df, 2026, rep)
        assert any(erwartet in m for m in rep.kritisch), (erwartet, rep.kritisch)


def test_validierung_sonderkantone_werden_entfernt():
    rep = build_db.Report(2026)
    out = build_db.validate_praemien(pd.concat([_mini_df(), _mini_df(Kanton="ZE")]), 2026, rep)
    assert list(out["Kanton"]) == ["AG"]
    assert any("Sonderkantone" in m for m in rep.hinweise)
