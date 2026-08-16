#!/usr/bin/env python3
"""Esporta lo snapshot per MetaTrader 5 come file JSON.

Modalita' (b) del bridge MQL5 (SEZIONE 6.2): l'indicatore legge
`MQL5\\Files\\macro_snapshot.json` senza bisogno di autorizzare alcun URL.

Uso:
    python export_mt5_file.py              # una sola esportazione
    python export_mt5_file.py --loop 60    # riesporta ogni 60 secondi
    python export_mt5_file.py --refresh    # ricalcola prima di esportare

La cartella di destinazione e' `MSE_MT5_FILES_DIR` (.env); se vuota viene usata
`data/mt5/`. La scrittura e' atomica: MT5 non legge mai un file a meta'.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# Consente l'esecuzione diretta dello script dalla cartella backend/.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import get_settings  # noqa: E402
from app.pipeline import refresh_in_new_session  # noqa: E402
from app.scheduler.jobs import export_snapshot_file  # noqa: E402
from app.storage.database import init_db  # noqa: E402

logger = logging.getLogger("export_mt5")


def main() -> int:
    parser = argparse.ArgumentParser(description="Esporta macro_snapshot.json per MT5")
    parser.add_argument(
        "--loop",
        type=int,
        default=0,
        metavar="SECONDI",
        help="riesporta a intervalli regolari (0 = una sola volta)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="ricalcola score e scenari prima di esportare",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s"
    )
    init_db(seed=True)
    settings = get_settings()
    logger.info("Cartella di destinazione: %s", settings.mt5_export_dir)

    while True:
        try:
            if args.refresh:
                report = refresh_in_new_session()
                logger.info("Ricalcolo: %s", report)
            path = export_snapshot_file()
            logger.info("Snapshot scritto in %s", path)
        except Exception:
            logger.exception("Esportazione fallita")
            if args.loop <= 0:
                return 1
        if args.loop <= 0:
            return 0
        time.sleep(args.loop)


if __name__ == "__main__":
    raise SystemExit(main())
