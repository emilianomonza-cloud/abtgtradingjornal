"""Caricamento della configurazione: variabili d'ambiente (.env) + file YAML.

Due livelli:
  * `Settings`  → parametri d'infrastruttura, da .env (prefisso MSE_).
  * `AppConfig` → parametri di dominio, da YAML in questa cartella, ricaricabili
                  a caldo senza riavviare il processo (POST /api/v1/admin/reload-config).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parent
APP_DIR = CONFIG_DIR.parent
BACKEND_DIR = APP_DIR.parent
PROJECT_ROOT = BACKEND_DIR.parent


class Settings(BaseSettings):
    """Parametri d'ambiente. Ogni campo e' sovrascrivibile con MSE_<NOME>."""

    model_config = SettingsConfigDict(
        env_prefix="MSE_",
        env_file=(BACKEND_DIR / ".env", PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Macro Scenario Engine"
    host: str = "127.0.0.1"
    port: int = 8000
    timezone: str = "Europe/Rome"
    log_level: str = "INFO"

    db_path: str = "data/macro_scenario.db"
    cache_dir: str = "data/cache"

    user_agent: str = (
        "MacroScenarioEngine/1.0 (uso personale, non commerciale; "
        "contatto: cambia@questa.email)"
    )
    min_request_interval_seconds: int = 300
    http_timeout_seconds: int = 25
    http_max_retries: int = 3
    network_enabled: bool = True

    scheduler_enabled: bool = True
    calendar_refresh_minutes: int = 60
    cb_refresh_minutes: int = 180
    scenario_refresh_minutes: int = 15
    mt5_export_minutes: int = 1

    mt5_files_dir: str = ""

    llm_provider: str = "none"
    llm_api_key: str = ""
    llm_model: str = "claude-sonnet-5"
    llm_base_url: str = ""
    llm_timeout_seconds: int = 45

    # ---------------------------------------------------------------- helpers
    def resolve(self, relative: str) -> Path:
        """Risolve un percorso relativo rispetto alla root del progetto."""
        p = Path(relative)
        return p if p.is_absolute() else (PROJECT_ROOT / p)

    @property
    def database_file(self) -> Path:
        path = self.resolve(self.db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def cache_path(self) -> Path:
        path = self.resolve(self.cache_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def mt5_export_dir(self) -> Path:
        raw = self.mt5_files_dir.strip()
        path = Path(raw) if raw else self.resolve("data/mt5")
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def llm_enabled(self) -> bool:
        return self.llm_provider.lower() not in ("", "none") and bool(self.llm_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


# --------------------------------------------------------------------------- #
#  Configurazione di dominio (YAML)
# --------------------------------------------------------------------------- #

_YAML_FILES = {
    "operator": "operator.yaml",
    "weights": "weights.yaml",
    "indicators": "indicators.yaml",
    "playbooks": "playbooks.yaml",
    "lexicon": "lexicon.yaml",
    "sources": "sources.yaml",
}


def _read_yaml(name: str) -> Dict[str, Any]:
    path = CONFIG_DIR / name
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Configurazione non valida in {path}: atteso un dizionario")
    return data


@dataclass
class AppConfig:
    """Snapshot immutabile della configurazione YAML."""

    operator: Dict[str, Any] = field(default_factory=dict)
    weights: Dict[str, Any] = field(default_factory=dict)
    indicators: Dict[str, Any] = field(default_factory=dict)
    playbooks: Dict[str, Any] = field(default_factory=dict)
    lexicon: Dict[str, Any] = field(default_factory=dict)
    sources: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------- scorciatoie
    @property
    def pairs(self) -> List[str]:
        return [str(p).upper() for p in self.operator.get("coppie", [])]

    @property
    def currencies(self) -> List[str]:
        return [str(c).upper() for c in self.operator.get("valute", [])]

    @property
    def synthetic(self) -> Dict[str, Any]:
        return self.operator.get("valute_sintetiche", {}) or {}

    @property
    def all_currencies(self) -> List[str]:
        """Valute reali + pseudo-valute (XAU...)."""
        return self.currencies + [c for c in self.synthetic if c not in self.currencies]

    @property
    def horizons(self) -> Dict[str, Any]:
        return self.operator.get("orizzonti", {}) or {}

    @property
    def default_horizon(self) -> str:
        return str(self.operator.get("orizzonte_default", "h24_72"))

    @property
    def central_banks(self) -> Dict[str, str]:
        return self.operator.get("banche_centrali", {}) or {}

    @property
    def subscore_weights(self) -> Dict[str, float]:
        raw = self.weights.get("pesi_sottoscore", {}) or {}
        total = sum(float(v) for v in raw.values()) or 1.0
        return {k: float(v) / total for k, v in raw.items()}

    def regime_for(self, currency: str) -> str:
        per_ccy = self.weights.get("regime_per_valuta", {}) or {}
        return str(
            per_ccy.get(currency.upper(), self.weights.get("regime_default", "HAWKISH_DOMINANT"))
        )

    def horizon_hours(self, horizon: str) -> int:
        spec = self.horizons.get(horizon) or self.horizons.get(self.default_horizon) or {}
        return int(spec.get("ore", 72))

    def split_pair(self, symbol: str) -> tuple[str, str]:
        """EURUSD → ('EUR', 'USD'). Supporta anche XAUUSD."""
        s = symbol.upper().replace("/", "").replace("_", "")
        if len(s) < 6:
            raise ValueError(f"Simbolo non riconosciuto: {symbol}")
        return s[:3], s[3:6]


_config_lock = threading.Lock()
_config: AppConfig | None = None


def _load_config() -> AppConfig:
    data = {key: _read_yaml(fname) for key, fname in _YAML_FILES.items()}
    return AppConfig(**data)


def get_config() -> AppConfig:
    """Restituisce la configurazione YAML corrente (thread-safe, con cache)."""
    global _config
    if _config is None:
        with _config_lock:
            if _config is None:
                _config = _load_config()
                logger.info("Configurazione YAML caricata da %s", CONFIG_DIR)
    return _config


def reload_config() -> AppConfig:
    """Ricarica i file YAML da disco. Usato da POST /api/v1/admin/reload-config."""
    global _config
    with _config_lock:
        _config = _load_config()
        logger.info("Configurazione YAML ricaricata")
    return _config
