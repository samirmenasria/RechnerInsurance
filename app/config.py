"""Konfiguration über Umgebungsvariablen (siehe README, Abschnitt «Konfiguration»)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _latest_db() -> Path:
    """Neueste db/praemien_<jahr>.sqlite, falls PRAEMIEN_DB nicht gesetzt ist."""
    candidates = sorted((ROOT / "db").glob("praemien_*.sqlite"))
    return candidates[-1] if candidates else ROOT / "db" / "praemien.sqlite"


@dataclass(frozen=True)
class Settings:
    praemien_db: Path
    leads_db: Path
    outbox_dir: Path
    lead_empfaenger: str
    smtp_host: str | None
    smtp_port: int
    smtp_user: str | None
    smtp_password: str | None
    smtp_security: str  # starttls | ssl | none
    smtp_from: str
    smtp_timeout: float
    rate_limit_anzahl: int
    rate_limit_sekunden: int

    @property
    def smtp_konfiguriert(self) -> bool:
        return bool(self.smtp_host)


def load_settings() -> Settings:
    env = os.environ.get
    return Settings(
        praemien_db=Path(env("PRAEMIEN_DB") or _latest_db()),
        leads_db=Path(env("LEADS_DB") or ROOT / "db" / "leads.sqlite"),
        outbox_dir=Path(env("LEAD_OUTBOX") or ROOT / "db" / "outbox"),
        lead_empfaenger=env("LEAD_EMPFAENGER") or "malik.gobbi@dl-finance.ch",
        smtp_host=env("SMTP_HOST") or None,
        smtp_port=int(env("SMTP_PORT") or 587),
        smtp_user=env("SMTP_USER") or None,
        smtp_password=env("SMTP_PASSWORD") or None,
        smtp_security=(env("SMTP_SECURITY") or "starttls").lower(),
        smtp_from=env("SMTP_FROM") or "praemienrechner@dl-finance.ch",
        smtp_timeout=float(env("SMTP_TIMEOUT") or 20),
        rate_limit_anzahl=int(env("LEAD_RATE_LIMIT") or 5),
        rate_limit_sekunden=int(env("LEAD_RATE_WINDOW") or 600),
    )
