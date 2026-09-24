"""REST-API und Auslieferung des Frontends.

Start:  uvicorn app.main:create_app --factory --reload
Doku:   http://localhost:8000/api/docs
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import leads, rechner
from app.config import ROOT, Settings, load_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
FRONTEND = ROOT / "frontend"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    if not Path(settings.praemien_db).exists():
        raise RuntimeError(f"Prämiendatenbank fehlt: {settings.praemien_db} – zuerst `python -m etl.build_db --jahr <Jahr>` ausführen")
    limiter = leads.RateLimiter(settings.rate_limit_anzahl, settings.rate_limit_sekunden)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.con = rechner.connect(settings.praemien_db)
        yield
        app.state.con.close()

    app = FastAPI(title="Prämienrechner OKP", version="1.0", lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.settings = settings

    def db(request: Request):
        return request.app.state.con

    def handle(fn, *args, **kw):
        try:
            return fn(*args, **kw)
        except rechner.NichtGefunden as exc:
            raise HTTPException(404, str(exc)) from exc
        except (rechner.RechnerFehler, leads.LeadFehler) as exc:
            raise HTTPException(422, str(exc)) from exc

    def anfrage(con, bfs: int, geburtsjahr: int, unfall: bool, franchise: str, modell: str | None,
                kinderrabatt: bool, altersuntergruppe: str | None = None) -> rechner.Anfrage:
        akl = handle(rechner.altersklasse_fuer, con, geburtsjahr)
        return rechner.Anfrage(bfs_nr=bfs, altersklasse=akl["code"], mit_unfall=unfall, franchise=franchise,
                               modell=None if modell in (None, "", "alle") else modell,
                               kinderrabatt=kinderrabatt, altersuntergruppe=altersuntergruppe)

    # ------------------------------------------------------------------ Endpunkte
    @app.get("/api/meta", tags=["Stammdaten"])
    def get_meta(con=Depends(db)):
        m = rechner.meta(con)
        return {"praemienjahr": int(m["praemienjahr"]), "erhebungsjahr": m.get("erhebungsjahr"),
                "quelle": m.get("quelle"), "stand": m.get("build_zeit"),
                "regionen_stand": m.get("regionen_stand")}

    @app.get("/api/orte", tags=["Ortssuche"])
    def get_orte(q: str = Query(..., min_length=2, max_length=60, description="PLZ oder Ortsname"), con=Depends(db)):
        return rechner.orte_suchen(con, q)

    @app.get("/api/gemeinden/{bfs_nr}", tags=["Ortssuche"])
    def get_gemeinde(bfs_nr: int, con=Depends(db)):
        return handle(rechner.gemeinde, con, bfs_nr)

    @app.get("/api/stammdaten", tags=["Stammdaten"])
    def get_stammdaten(con=Depends(db)):
        return rechner.stammdaten(con)

    @app.get("/api/altersklasse", tags=["Stammdaten"])
    def get_altersklasse(geburtsjahr: int, con=Depends(db)):
        return handle(rechner.altersklasse_fuer, con, geburtsjahr)

    @app.get("/api/versicherer", tags=["Stammdaten"])
    def get_versicherer(con=Depends(db)):
        return rechner.versicherer_liste(con)

    @app.get("/api/versicherer/{bag_nr}", tags=["Stammdaten"])
    def get_versicherer_detail(bag_nr: int, con=Depends(db)):
        return handle(rechner.versicherer_detail, con, bag_nr)

    @app.get("/api/praemien", tags=["Prämien"])
    def get_praemien(
        bfs: int = Query(..., description="BFS-Nr. der Wohngemeinde"),
        geburtsjahr: int = Query(..., ge=1890, le=2100),
        unfall: bool = Query(..., description="Unfalldeckung eingeschlossen"),
        franchise: str = Query(..., pattern=r"^FRA-\d+$"),
        modell: str | None = Query(None, description="alle | TAR-BASE | TAR-HAM | TAR-HMO | TAR-DIV"),
        kinderrabatt: bool = Query(False, description="Kinder: günstigste Rabattstufe statt K1"),
        altersuntergruppe: str | None = Query(None, pattern=r"^[KJE]\d$"),
        aktueller_versicherer: int | None = None,
        aktueller_tarif: int | None = None,
        aktuelle_praemie: float | None = Query(None, gt=0, lt=5000),
        con=Depends(db),
    ):
        a = anfrage(con, bfs, geburtsjahr, unfall, franchise, modell, kinderrabatt, altersuntergruppe)
        res = handle(rechner.praemien, con, a, aktueller_versicherer, aktueller_tarif, aktuelle_praemie)
        res["kontext"]["geburtsjahr"] = geburtsjahr
        return res

    @app.get("/api/gesamtkosten", tags=["Prämien"])
    def get_gesamtkosten(
        bfs: int, geburtsjahr: int = Query(..., ge=1890, le=2100), unfall: bool = True,
        kosten: float = Query(..., ge=0, le=1_000_000, description="Erwartete Gesundheitskosten pro Jahr (CHF)"),
        modell: str | None = None, kinderrabatt: bool = False, tarif_id: int | None = None,
        con=Depends(db),
    ):
        # Franchise ist hier nur Platzhalter für die Validierung; berechnet werden alle zulässigen Franchisen
        akl = handle(rechner.altersklasse_fuer, con, geburtsjahr)
        ord_f = con.execute("SELECT franchise FROM franchise_stufe WHERE altersklasse = ? AND ist_ordentlich = 1",
                            (akl["code"],)).fetchone()[0]
        a = anfrage(con, bfs, geburtsjahr, unfall, ord_f, modell, kinderrabatt)
        return handle(rechner.gesamtkosten, con, a, kosten, tarif_id)

    @app.post("/api/anfrage", tags=["Offertanfrage"], status_code=201)
    def post_anfrage(body: leads.AnfrageIn, request: Request, con=Depends(db)):
        ip = request.client.host if request.client else "unbekannt"
        if not limiter.erlaubt(ip):
            raise HTTPException(429, "Zu viele Anfragen – bitte versuchen Sie es später erneut.")
        return handle(leads.lead_verarbeiten, con, settings, body, ip)

    # ------------------------------------------------------------------ Frontend
    if FRONTEND.exists():
        app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(FRONTEND / "index.html")

    return app
