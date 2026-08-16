"""Punto d'ingresso FastAPI del Macro Scenario Engine."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import pipeline
from .api import admin_router, api_router
from .config import get_config, get_settings
from .scheduler.jobs import start_scheduler, stop_scheduler
from .storage.database import init_db, session_scope
from .web import WEB_DIR, web_router

logger = logging.getLogger(__name__)

DESCRIPTION = """
Analisi fondamentale FX con scenari probabilistici.

* **Score valutari** con sei sotto-score spiegabili (SEZIONE 3.2)
* **Catene causali esplicite** dal framework macro di riferimento (SEZIONE 3.1)
* **Tre scenari per coppia**, con probabilita', confidenza, invalidazioni e scadenza (SEZIONE 3.5)
* **Bridge MQL5** via endpoint `/api/v1/mt5/snapshot` o file `macro_snapshot.json`

Le probabilita' sono STIME DI MODELLO: non costituiscono consulenza finanziaria.
"""


def configure_logging() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()
    config = get_config()

    init_db(seed=True)
    logger.info(
        "Configurazione: %d coppie, %d valute, orizzonte %s",
        len(config.pairs),
        len(config.all_currencies),
        config.default_horizon,
    )

    # Primo calcolo: la dashboard e l'endpoint MT5 non devono mai partire vuoti.
    try:
        with session_scope() as session:
            pipeline.refresh(session)
    except Exception:  # pragma: no cover - non deve impedire l'avvio
        logger.exception("Calcolo iniziale fallito: il sistema parte comunque")

    scheduler = start_scheduler()
    if scheduler is not None:
        logger.info("Scheduler attivo (%s)", settings.timezone)

    yield

    stop_scheduler()


app = FastAPI(
    title=get_settings().app_name,
    description=DESCRIPTION,
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# La dashboard e il terminale MT5 girano sulla stessa macchina: CORS aperto solo
# in locale, per non complicare l'uso da un browser diverso dalla porta del server.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost", "http://127.0.0.1", "*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
app.include_router(api_router)
app.include_router(admin_router)
app.include_router(web_router)


def run() -> None:  # pragma: no cover - avvio manuale
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":  # pragma: no cover
    run()
