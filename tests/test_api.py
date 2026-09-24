"""API-Tests gegen die aus ./data gebaute Datenbank."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import make_settings

GEBURTSJAHR = {"AKL-KIN": 2016, "AKL-JUG": 2004, "AKL-ERW": 1980}


def test_etl_kennzahlen(praemien_db, excel_praemien):
    con = sqlite3.connect(praemien_db)
    n_excel = int((~excel_praemien["Kanton"].isin(["ZE", "ZR"])).sum())
    assert con.execute("SELECT COUNT(*) FROM praemie").fetchone()[0] == n_excel
    assert con.execute("SELECT COUNT(*) FROM kanton").fetchone()[0] == 26
    assert con.execute("SELECT COUNT(*) FROM region WHERE kanton IN ('ZE','ZR')").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(DISTINCT bag_nr) FROM tarif").fetchone()[0] == 34
    # jede Gemeinde hat eine Region mit Prämien
    assert con.execute("""SELECT COUNT(*) FROM gemeinde g WHERE NOT EXISTS
                          (SELECT 1 FROM praemie p WHERE p.region_id = g.region_id)""").fetchone()[0] == 0


def _bfs_fuer_region(con, kanton, region_nr):
    return con.execute("""SELECT g.bfs_nr FROM gemeinde g JOIN region r ON r.id = g.region_id
                          WHERE r.kanton = ? AND r.nr = ? ORDER BY g.bfs_nr LIMIT 1""",
                       (kanton, region_nr)).fetchone()[0]


def test_stichproben_praemien_gegen_excel(client, praemien_db, excel_praemien):
    """200 zufällige Prämienzeilen aus der Excel-Datei müssen exakt über die API kommen."""
    con = sqlite3.connect(praemien_db)
    df = excel_praemien[~excel_praemien["Kanton"].isin(["ZE", "ZR"])]
    sample = df.sample(200, random_state=2026)
    for r in sample.itertuples(index=False):
        bfs = _bfs_fuer_region(con, r.Kanton, int(r.Region[-1]))
        params = dict(bfs=bfs, geburtsjahr=GEBURTSJAHR[r.Altersklasse], unfall=r.Unfalleinschluss == "MIT-UNF",
                      franchise=r.Franchise, modell=r.Tariftyp)
        if isinstance(r.Altersuntergruppe, str):
            params["altersuntergruppe"] = r.Altersuntergruppe
        res = client.get("/api/praemien", params=params)
        assert res.status_code == 200, res.text
        treffer = [a for a in res.json()["angebote"] if a["bag_nr"] == r.Versicherer and a["tarif_code"] == r.Tarif]
        assert len(treffer) == 1, (r, params)
        assert treffer[0]["monat"] == pytest.approx(r.Prämie, abs=0.001)
        assert treffer[0]["jahr"] == pytest.approx(round(r.Prämie * 12, 2), abs=0.001)


def test_bekannte_praemie_zuerich(client, excel_praemien):
    """Fixe Stichprobe: CSS Grundversicherung, Zürich (Region 1), Erwachsene, Franchise 300, mit Unfall."""
    x = excel_praemien
    erwartet = x[(x.Versicherer == 8) & (x.Kanton == "ZH") & (x.Region == "PR-REG CH1") & (x.Altersklasse == "AKL-ERW")
                 & (x.Unfalleinschluss == "MIT-UNF") & (x.Tarif == "BASE") & (x.Franchise == "FRA-300")]["Prämie"].item()
    res = client.get("/api/praemien", params=dict(bfs=261, geburtsjahr=1985, unfall=True, franchise="FRA-300",
                                                  modell="TAR-BASE")).json()
    css = next(a for a in res["angebote"] if a["bag_nr"] == 8)
    assert css["monat"] == erwartet
    assert res["kontext"]["gemeinde"]["region_nr"] == 1


def test_plz_mit_mehreren_praemienregionen(client, excel_praemien):
    """PLZ 1700: Fribourg (FR Region 1) sowie Düdingen/Tafers (FR Region 2)."""
    res = client.get("/api/orte", params={"q": "1700"}).json()
    gem = {g["bfs_nr"]: g for g in res["gemeinden"]}
    assert res["mehrere_regionen"] is True
    assert gem[2196]["gemeinde"] == "Fribourg" and gem[2196]["region_nr"] == 1
    assert gem[2293]["gemeinde"] == "Düdingen" and gem[2293]["region_nr"] == 2

    params = dict(geburtsjahr=1985, unfall=True, franchise="FRA-300", modell="TAR-BASE")
    r1 = client.get("/api/praemien", params={**params, "bfs": 2196}).json()
    r2 = client.get("/api/praemien", params={**params, "bfs": 2293}).json()
    x = excel_praemien
    sel = x[(x.Versicherer == 1562) & (x.Kanton == "FR") & (x.Altersklasse == "AKL-ERW") & (x.Tarif == "BASE")
            & (x.Unfalleinschluss == "MIT-UNF") & (x.Franchise == "FRA-300")].set_index("Region")["Prämie"]
    h1 = next(a for a in r1["angebote"] if a["bag_nr"] == 1562)["monat"]
    h2 = next(a for a in r2["angebote"] if a["bag_nr"] == 1562)["monat"]
    assert h1 == sel["PR-REG CH1"] and h2 == sel["PR-REG CH2"] and h1 != h2


def test_plz_mehrere_gemeinden_gleiche_region(client):
    res = client.get("/api/orte", params={"q": "8914"}).json()
    assert len(res["gemeinden"]) >= 1
    assert res["mehrere_regionen"] is False


def test_ortssuche_nach_name(client):
    res = client.get("/api/orte", params={"q": "Sion"}).json()
    assert any(g["bfs_nr"] == 6266 for g in res["gemeinden"])


@pytest.mark.parametrize("jahrgang,klasse", [(2026, "AKL-KIN"), (2008, "AKL-KIN"), (2007, "AKL-JUG"),
                                             (2001, "AKL-JUG"), (2000, "AKL-ERW"), (1940, "AKL-ERW")])
def test_altersklasse_nach_jahrgang(client, jahrgang, klasse):
    assert client.get("/api/altersklasse", params={"geburtsjahr": jahrgang}).json()["code"] == klasse


def test_unzulaessige_franchise(client):
    r = client.get("/api/praemien", params=dict(bfs=261, geburtsjahr=1985, unfall=True, franchise="FRA-0"))
    assert r.status_code == 422
    r = client.get("/api/praemien", params=dict(bfs=261, geburtsjahr=2016, unfall=True, franchise="FRA-2500"))
    assert r.status_code == 422


def test_unbekannte_gemeinde(client):
    r = client.get("/api/praemien", params=dict(bfs=99999, geburtsjahr=1985, unfall=True, franchise="FRA-300"))
    assert r.status_code == 404


def test_sortierung_und_differenzen(client):
    res = client.get("/api/praemien", params=dict(bfs=261, geburtsjahr=1985, unfall=False, franchise="FRA-2500",
                                                  aktueller_versicherer=1509)).json()
    monate = [a["monat"] for a in res["angebote"]]
    assert monate == sorted(monate)
    assert res["angebote"][0]["diff_guenstigste_monat"] == 0
    assert res["aktuell"]["bag_nr"] == 1509 and res["aktuell"]["tariftyp"] == "TAR-BASE"
    for a in res["angebote"]:
        assert a["diff_aktuell_monat"] == pytest.approx(a["monat"] - res["aktuell"]["monat"], abs=0.005)


def test_aktueller_versicherer_mit_modellfilter(client):
    """Referenz bleibt das Standardmodell, auch wenn nur HMO angezeigt wird."""
    res = client.get("/api/praemien", params=dict(bfs=261, geburtsjahr=1985, unfall=True, franchise="FRA-300",
                                                  modell="TAR-HMO", aktueller_versicherer=8)).json()
    assert all(a["tariftyp"] == "TAR-HMO" for a in res["angebote"])
    assert res["aktuell"]["bag_nr"] == 8 and res["aktuell"]["tariftyp"] == "TAR-BASE"


def test_taetigkeitsgebiet_region(client, praemien_db):
    """Visperterminen (1040) und SLKK (923) sind im Wallis nur in Region 2 tätig."""
    con = sqlite3.connect(praemien_db)
    vs1, vs2 = _bfs_fuer_region(con, "VS", 1), _bfs_fuer_region(con, "VS", 2)
    p = dict(geburtsjahr=1985, unfall=True, franchise="FRA-300")
    in_r1 = {a["bag_nr"] for a in client.get("/api/praemien", params={**p, "bfs": vs1}).json()["angebote"]}
    in_r2 = {a["bag_nr"] for a in client.get("/api/praemien", params={**p, "bfs": vs2}).json()["angebote"]}
    assert 1040 not in in_r1 and 923 not in in_r1
    assert 1040 in in_r2 and 923 in in_r2
    # Einsiedler Krankenkasse (134) ist in Zürich tätig, nicht aber in Genf
    ge = {a["bag_nr"] for a in client.get("/api/praemien", params={**p, "bfs": 6621}).json()["angebote"]}
    assert 134 not in ge


def test_kinderrabatt(client):
    p = dict(bfs=261, geburtsjahr=2016, unfall=True, franchise="FRA-0", modell="TAR-BASE")
    ohne = {a["bag_nr"]: a for a in client.get("/api/praemien", params=p).json()["angebote"]}
    mit = {a["bag_nr"]: a for a in client.get("/api/praemien", params={**p, "kinderrabatt": True}).json()["angebote"]}
    assert all(a["altersuntergruppe"] == "K1" for a in ohne.values())
    assert mit[1555]["rabatt"] and mit[1555]["monat"] < ohne[1555]["monat"]      # Visana: K3
    assert not mit[1509]["rabatt"] and mit[1509]["monat"] == ohne[1509]["monat"]  # Sanitas: nur K1


def test_gesamtkosten(client):
    res = client.get("/api/gesamtkosten", params=dict(bfs=261, geburtsjahr=1985, unfall=True, kosten=3000)).json()
    zeilen = {z["franchise"]: z for z in res["zeilen"]}
    assert set(zeilen) == {"FRA-300", "FRA-500", "FRA-1000", "FRA-1500", "FRA-2000", "FRA-2500"}
    z = zeilen["FRA-300"]
    assert z["franchise_anteil"] == 300 and z["selbstbehalt"] == 270
    assert z["total"] == pytest.approx(z["jahrespraemie"] + 570, abs=0.01)
    assert sum(1 for z in res["zeilen"] if z["optimal"]) == 1
    kind = client.get("/api/gesamtkosten", params=dict(bfs=261, geburtsjahr=2016, unfall=True, kosten=20000)).json()
    assert kind["max_selbstbehalt"] == 350 and all(z["selbstbehalt"] == 350 for z in kind["zeilen"] if z["angebot"])


def test_gesamtkosten_fuer_tarif(client):
    res = client.get("/api/praemien", params=dict(bfs=261, geburtsjahr=1985, unfall=True, franchise="FRA-300",
                                                  modell="TAR-BASE")).json()
    kpt = next(a for a in res["angebote"] if a["bag_nr"] == 376)  # KPT bietet keine Franchise 2000 an
    k = client.get("/api/gesamtkosten", params=dict(bfs=261, geburtsjahr=1985, unfall=True, kosten=500,
                                                    tarif_id=kpt["tarif_id"])).json()
    zeilen = {z["franchise"]: z for z in k["zeilen"]}
    assert zeilen["FRA-2000"]["angebot"] is None
    assert zeilen["FRA-300"]["angebot"]["tarif_id"] == kpt["tarif_id"]


def test_stammdaten_und_versicherer(client):
    s = client.get("/api/stammdaten").json()
    assert s["praemienjahr"] == 2026
    kin = next(a for a in s["altersklassen"] if a["code"] == "AKL-KIN")
    assert [f["betrag"] for f in kin["franchisen"]] == [0, 100, 200, 300, 400, 500, 600]
    assert len(s["versicherer"]) == 34
    v = client.get("/api/versicherer/8").json()
    assert v["name_kurz"] == "CSS" and v["plz"] == "6002" and v["email"] == "css.info@css.ch"
    assert v["name_fr"] == "CSS Assurance-maladie SA"
    galenos = client.get("/api/versicherer/1386").json()
    assert any(a["bag_nr"] == 1570 for a in galenos["anpassungen"])  # vivacare fusioniert


def test_einzugsgebiet_filter(db_kopie, tmp_path):
    """Ein auf Gemeinden eingeschränktes HMO wird nur dort angezeigt."""
    con = sqlite3.connect(db_kopie)
    tarif_id = con.execute("SELECT id FROM tarif WHERE bag_nr = 1384 AND tarif_code = 'HMO'").fetchone()[0]
    region = con.execute("SELECT id FROM region WHERE kanton = 'ZH' AND nr = 1").fetchone()[0]
    cur = con.execute("INSERT INTO einzugsgebiet (tarif_id, kanton, region_id, eingeschraenkt) VALUES (?, 'ZH', ?, 1)",
                      (tarif_id, region))
    con.execute("INSERT INTO einzugsgebiet_gemeinde VALUES (?, 261)", (cur.lastrowid,))
    con.commit()
    con.close()
    with TestClient(create_app(make_settings(db_kopie, tmp_path))) as c:
        p = dict(geburtsjahr=1985, unfall=True, franchise="FRA-300", modell="TAR-HMO")
        zh = [a["tarif_id"] for a in c.get("/api/praemien", params={**p, "bfs": 261}).json()["angebote"]]
        andere = [a["tarif_id"] for a in c.get("/api/praemien", params={**p, "bfs": 230}).json()["angebote"]]  # Winterthur
        assert tarif_id in zh and tarif_id not in andere


FAMILIE = [
    {"name": "Anna", "geburtsjahr": 1985, "mit_unfall": True, "franchise": "FRA-2500"},
    {"name": "Marco", "geburtsjahr": 1983, "mit_unfall": False, "franchise": "FRA-2500"},
    {"name": "Lea", "geburtsjahr": 2014, "mit_unfall": True, "franchise": "FRA-0"},
    {"name": "Tim", "geburtsjahr": 2018, "mit_unfall": True, "franchise": "FRA-0"},
]


def _einzeln(client, p, **kw):
    return client.get("/api/praemien", params=dict(bfs=261, geburtsjahr=p["geburtsjahr"], unfall=p["mit_unfall"],
                                                   franchise=p["franchise"], **kw)).json()["angebote"]


def test_haushalt_einzelperson_gleich_wie_praemien(client):
    p = FAMILIE[0]
    h = client.post("/api/haushalt", json={"bfs_nr": 261, "personen": [p]}).json()
    einzeln = _einzeln(client, p)
    assert [a["monat"] for a in h["gemeinsam"]["angebote"]] == [a["monat"] for a in einzeln]
    assert h["kombination"]["monat"] == einzeln[0]["monat"]


def test_haushalt_gemeinsam_ist_summe(client):
    h = client.post("/api/haushalt", json={"bfs_nr": 261, "personen": FAMILIE, "kinderrabatt": False}).json()
    assert [p["name"] for p in h["personen"]] == ["Anna", "Marco", "Lea", "Tim"]
    einzeln = [{a["tarif_id"]: a["monat"] for a in _einzeln(client, p)} for p in FAMILIE]
    gemeinsame = set(einzeln[0]).intersection(*einzeln[1:])
    assert h["gemeinsam"]["anzahl"] == len(gemeinsame)
    monate = [a["monat"] for a in h["gemeinsam"]["angebote"]]
    assert monate == sorted(monate)
    for a in h["gemeinsam"]["angebote"][:20]:
        assert a["monat"] == pytest.approx(sum(e[a["tarif_id"]] for e in einzeln), abs=0.005)
        assert [z["name"] for z in a["personen"]] == ["Anna", "Marco", "Lea", "Tim"]
        assert len({z["tarif_id"] for z in a["personen"]}) == 1


def test_haushalt_kombination_ist_minimum_pro_person(client):
    h = client.post("/api/haushalt", json={"bfs_nr": 261, "personen": FAMILIE, "kinderrabatt": False}).json()
    kb = h["kombination"]
    for p, z in zip(FAMILIE, kb["personen"]):
        assert z["monat"] == _einzeln(client, p)[0]["monat"]
    assert kb["monat"] == pytest.approx(sum(z["monat"] for z in kb["personen"]), abs=0.005)
    assert kb["monat"] <= h["gemeinsam"]["angebote"][0]["monat"]
    assert kb["diff_gemeinsam_monat"] <= 0


def test_haushalt_geschwisterrabatt(client):
    """Ältestes Kind zahlt K1, weitere Kinder erhalten die günstigste Rabattstufe."""
    ohne = client.post("/api/haushalt", json={"bfs_nr": 261, "personen": FAMILIE, "kinderrabatt": False}).json()
    mit = client.post("/api/haushalt", json={"bfs_nr": 261, "personen": FAMILIE, "kinderrabatt": True}).json()
    assert [p["geschwisterrabatt"] for p in mit["personen"]] == [False, False, False, True]
    visana_ohne = next(a for a in ohne["gemeinsam"]["angebote"] if a["bag_nr"] == 1555 and a["tarif_code"] == "BASE")
    visana_mit = next(a for a in mit["gemeinsam"]["angebote"] if a["tarif_id"] == visana_ohne["tarif_id"])
    lea_mit, tim_mit = visana_mit["personen"][2], visana_mit["personen"][3]
    assert lea_mit["altersuntergruppe"] == "K1" and tim_mit["altersuntergruppe"] == "K3"
    assert visana_mit["monat"] < visana_ohne["monat"]


def test_haushalt_aktueller_versicherer(client):
    h = client.post("/api/haushalt", json={"bfs_nr": 261, "personen": FAMILIE, "aktueller_versicherer": 1562,
                                           "modell": "TAR-HMO"}).json()
    assert h["aktuell"]["bag_nr"] == 1562 and len(h["aktuell"]["personen"]) == 4
    assert all(z["tariftyp"] == "TAR-BASE" for z in h["aktuell"]["personen"])
    a = h["gemeinsam"]["angebote"][0]
    assert a["diff_aktuell_monat"] == pytest.approx(a["monat"] - h["aktuell"]["monat"], abs=0.005)


def test_haushalt_validierung(client):
    zu_viele = [FAMILIE[0]] * 11
    assert client.post("/api/haushalt", json={"bfs_nr": 261, "personen": zu_viele}).status_code == 422
    falsch = [{**FAMILIE[2], "franchise": "FRA-2500"}]
    assert client.post("/api/haushalt", json={"bfs_nr": 261, "personen": falsch}).status_code == 422
