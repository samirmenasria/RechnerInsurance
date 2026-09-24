"""Gemeinsame Fixtures: Prämiendatenbank wird einmal pro Testlauf aus ./data gebaut."""
from __future__ import annotations

import dataclasses
import os
import shutil
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app
from etl import build_db

ROOT = Path(__file__).resolve().parent.parent
JAHR = 2026


@pytest.fixture(scope="session")
def praemien_db(tmp_path_factory) -> Path:
    """Baut die DB frisch (oder verwendet PRAEMIEN_DB_TEST, um Zeit zu sparen)."""
    vorhanden = os.environ.get("PRAEMIEN_DB_TEST")
    if vorhanden:
        return Path(vorhanden)
    out = tmp_path_factory.mktemp("db") / f"praemien_{JAHR}.sqlite"
    rc = build_db.main(["--jahr", str(JAHR), "--data", str(ROOT / "data"), "--out", str(out)])
    assert rc == 0, "ETL mit kritischen Fehlern abgebrochen"
    return out


@pytest.fixture(scope="session")
def excel_praemien() -> pd.DataFrame:
    return pd.read_excel(ROOT / "data" / "gesamtbericht_ch.xlsx", sheet_name="Export", engine=build_db._engine())


def make_settings(db: Path, tmp: Path, **kw):
    return dataclasses.replace(
        load_settings(), praemien_db=db, leads_db=tmp / "leads.sqlite", outbox_dir=tmp / "outbox",
        smtp_host=None, lead_empfaenger="malik.gobbi@dl-finance.ch", rate_limit_anzahl=100, **kw)


@pytest.fixture()
def settings(praemien_db, tmp_path):
    return make_settings(praemien_db, tmp_path)


@pytest.fixture()
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


@pytest.fixture()
def db_kopie(praemien_db, tmp_path) -> Path:
    """Beschreibbare Kopie für Tests, die Daten ergänzen (z.B. Einzugsgebiete)."""
    ziel = tmp_path / "kopie.sqlite"
    shutil.copy(praemien_db, ziel)
    return ziel
