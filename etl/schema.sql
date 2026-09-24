-- Datenmodell Prämienrechner OKP (eine Datenbank pro Prämienjahr)
PRAGMA foreign_keys = ON;

CREATE TABLE meta (
    schluessel TEXT PRIMARY KEY,
    wert       TEXT
);

-- ---------------------------------------------------------------- Geografie
CREATE TABLE kanton (
    code         TEXT PRIMARY KEY,          -- 'ZH'
    name_de      TEXT NOT NULL,
    name_fr      TEXT,
    name_it      TEXT,
    hat_regionen INTEGER NOT NULL DEFAULT 0 -- 1 = in Prämienregionen 1..3 unterteilt
);

CREATE TABLE region (
    id     INTEGER PRIMARY KEY,
    kanton TEXT NOT NULL REFERENCES kanton(code),
    nr     INTEGER NOT NULL CHECK (nr BETWEEN 0 AND 3),
    code   TEXT NOT NULL,                   -- 'PR-REG CH1'
    UNIQUE (kanton, nr)
);

CREATE TABLE gemeinde (
    bfs_nr    INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    kanton    TEXT NOT NULL REFERENCES kanton(code),
    bezirk    TEXT,
    region_id INTEGER NOT NULL REFERENCES region(id)
);
CREATE INDEX ix_gemeinde_name ON gemeinde(name COLLATE NOCASE);

-- n:m – eine PLZ kann mehrere Gemeinden (auch in verschiedenen Regionen/Kantonen) umfassen
CREATE TABLE plz_gemeinde (
    plz             INTEGER NOT NULL,
    ortsbezeichnung TEXT NOT NULL,
    bfs_nr          INTEGER NOT NULL REFERENCES gemeinde(bfs_nr),
    PRIMARY KEY (plz, ortsbezeichnung, bfs_nr)
);
CREATE INDEX ix_plz_gemeinde_plz ON plz_gemeinde(plz);
CREATE INDEX ix_plz_gemeinde_ort ON plz_gemeinde(ortsbezeichnung COLLATE NOCASE);
CREATE INDEX ix_plz_gemeinde_bfs ON plz_gemeinde(bfs_nr);

-- ---------------------------------------------------------------- Versicherer
CREATE TABLE versicherer (
    bag_nr        INTEGER PRIMARY KEY,
    uid           TEXT,
    name_kurz     TEXT NOT NULL,
    name_de       TEXT,
    name_fr       TEXT,
    name_it       TEXT,
    strasse       TEXT,
    postfach      TEXT,
    plz           TEXT,
    ort           TEXT,
    tel           TEXT,
    fax           TEXT,
    email         TEXT,
    web           TEXT,
    adresse_raw   TEXT,
    rechtsform    TEXT,
    gruppe        TEXT,
    taetigkeitsgebiet_raw TEXT,
    nur_taggeld   INTEGER NOT NULL DEFAULT 0,
    aktiv         INTEGER NOT NULL DEFAULT 1
);

-- Geparstes Tätigkeitsgebiet: region_nr NULL = ganzer Kanton
CREATE TABLE versicherer_taetigkeit (
    bag_nr    INTEGER NOT NULL REFERENCES versicherer(bag_nr),
    kanton    TEXT NOT NULL REFERENCES kanton(code),
    region_nr INTEGER,
    UNIQUE (bag_nr, kanton, region_nr)
);

CREATE TABLE versicherer_anpassung (
    bag_nr      INTEGER NOT NULL,
    name        TEXT,
    art         TEXT NOT NULL,              -- 'fusion' | 'entzug' | 'bisherige_angaben'
    datum       TEXT,
    ziel_bag_nr INTEGER,
    text_raw    TEXT
);

-- ---------------------------------------------------------------- Codes
CREATE TABLE altersklasse (
    code      TEXT PRIMARY KEY,             -- 'AKL-KIN'
    alter_von INTEGER NOT NULL,
    alter_bis INTEGER,                      -- NULL = offen
    name_de   TEXT NOT NULL,
    name_fr   TEXT,
    name_it   TEXT,
    sort      INTEGER NOT NULL
);

CREATE TABLE altersuntergruppe (
    code         TEXT PRIMARY KEY,          -- 'K1'
    altersklasse TEXT NOT NULL REFERENCES altersklasse(code),
    name_de      TEXT NOT NULL,
    name_fr      TEXT,
    name_it      TEXT,
    ist_standard INTEGER NOT NULL DEFAULT 0 -- K1 = Kinderprämie ohne Rabatt
);

-- Versichererspezifische Bezeichnung der Altersuntergruppen (aus «Tarife <Jahr>», Kategorie ALT)
CREATE TABLE versicherer_altersuntergruppe (
    bag_nr  INTEGER NOT NULL REFERENCES versicherer(bag_nr),
    code    TEXT NOT NULL REFERENCES altersuntergruppe(code),
    name_de TEXT,
    name_fr TEXT,
    name_it TEXT,
    PRIMARY KEY (bag_nr, code)
);

CREATE TABLE franchise (
    code   TEXT PRIMARY KEY,                -- 'FRA-300'
    betrag INTEGER NOT NULL
);

-- Zulässige Franchisen je Altersklasse; Franchisestufe ist nur zusammen mit der Altersklasse eindeutig
CREATE TABLE franchise_stufe (
    altersklasse   TEXT NOT NULL REFERENCES altersklasse(code),
    franchise      TEXT NOT NULL REFERENCES franchise(code),
    stufe          TEXT NOT NULL,           -- 'FRAST1'
    ist_ordentlich INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (altersklasse, franchise)
);

CREATE TABLE unfalleinschluss (
    code       TEXT PRIMARY KEY,            -- 'MIT-UNF'
    mit_unfall INTEGER NOT NULL,
    name_de    TEXT NOT NULL,
    name_fr    TEXT,
    name_it    TEXT
);

CREATE TABLE tariftyp (
    code    TEXT PRIMARY KEY,               -- 'TAR-BASE'
    name_de TEXT NOT NULL,
    name_fr TEXT,
    name_it TEXT,
    sort    INTEGER NOT NULL
);

CREATE TABLE tarif (
    id             INTEGER PRIMARY KEY,
    bag_nr         INTEGER NOT NULL REFERENCES versicherer(bag_nr),
    tarif_code     TEXT NOT NULL,           -- kassenspezifische ID
    tariftyp       TEXT NOT NULL REFERENCES tariftyp(code),
    bezeichnung_de TEXT NOT NULL,
    name_fr        TEXT,                    -- aus «Tarife <Jahr>», falls vorhanden
    name_it        TEXT,
    UNIQUE (bag_nr, tarif_code)
);
CREATE INDEX ix_tarif_typ ON tarif(tariftyp);

-- Einzugsgebiete (opendata.swiss «Einzugsgebiete <Jahr>»); leer, solange die Datei fehlt
CREATE TABLE einzugsgebiet (
    id             INTEGER PRIMARY KEY,
    tarif_id       INTEGER NOT NULL REFERENCES tarif(id),
    kanton         TEXT NOT NULL REFERENCES kanton(code),
    region_id      INTEGER REFERENCES region(id),
    eingeschraenkt INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_einzugsgebiet_tarif ON einzugsgebiet(tarif_id, region_id);

CREATE TABLE einzugsgebiet_gemeinde (
    einzugsgebiet_id INTEGER NOT NULL REFERENCES einzugsgebiet(id),
    bfs_nr           INTEGER NOT NULL,
    PRIMARY KEY (einzugsgebiet_id, bfs_nr)
);
CREATE INDEX ix_einzugsgebiet_gemeinde_bfs ON einzugsgebiet_gemeinde(bfs_nr);

-- ---------------------------------------------------------------- Fakten
CREATE TABLE praemie (
    id                INTEGER PRIMARY KEY,
    tarif_id          INTEGER NOT NULL REFERENCES tarif(id),
    region_id         INTEGER NOT NULL REFERENCES region(id),
    altersklasse      TEXT NOT NULL REFERENCES altersklasse(code),
    altersuntergruppe TEXT REFERENCES altersuntergruppe(code),
    mit_unfall        INTEGER NOT NULL CHECK (mit_unfall IN (0, 1)),
    franchise         TEXT NOT NULL REFERENCES franchise(code),
    betrag            REAL NOT NULL CHECK (betrag > 0),
    is_base_p         INTEGER NOT NULL,
    is_base_f         INTEGER NOT NULL,
    befristet         TEXT
);
-- Eindeutigkeit (NULL-Untergruppe via COALESCE mitberücksichtigt)
CREATE UNIQUE INDEX ux_praemie ON praemie(
    tarif_id, region_id, altersklasse, COALESCE(altersuntergruppe, ''), mit_unfall, franchise);
-- Hauptabfrage des Rechners: alle Angebote einer Region für eine Person
CREATE INDEX ix_praemie_suche ON praemie(region_id, altersklasse, mit_unfall, franchise, altersuntergruppe);
CREATE INDEX ix_praemie_tarif ON praemie(tarif_id);
