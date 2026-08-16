"""Modelli ORM (SQLAlchemy 2.0) del Macro Scenario Engine."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Event(Base):
    """Evento di calendario economico normalizzato (SEZIONE 4.1)."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    hash_dedup: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    timestamp_utc: Mapped[datetime] = mapped_column(DateTime, index=True)
    currency: Mapped[str] = mapped_column(String(8), index=True)
    title: Mapped[str] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(48), index=True, default="OTHER")
    impact: Mapped[str] = mapped_column(String(8), default="YELLOW", index=True)

    # Valori numerici normalizzati (None se non pubblicati / non numerici)
    actual: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    forecast: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    previous: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    revised: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Valori grezzi come pubblicati (es. "236K", "3.2%")
    actual_raw: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    forecast_raw: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    previous_raw: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    revised_raw: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    unit: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    # Esito della classificazione sorpresa (SEZIONE 3.3)
    surprise_z: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    surprise_label: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    surprise_impact: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    source: Mapped[str] = mapped_column(String(48), default="unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("ix_events_ccy_ts", "currency", "timestamp_utc"),
        Index("ix_events_cat_ccy_ts", "category", "currency", "timestamp_utc"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "timestamp_utc": self.timestamp_utc.isoformat() + "Z"
            if self.timestamp_utc and self.timestamp_utc.tzinfo is None
            else (self.timestamp_utc.isoformat() if self.timestamp_utc else None),
            "currency": self.currency,
            "title": self.title,
            "category": self.category,
            "impact": self.impact,
            "actual": self.actual,
            "forecast": self.forecast,
            "previous": self.previous,
            "revised": self.revised,
            "actual_raw": self.actual_raw,
            "forecast_raw": self.forecast_raw,
            "previous_raw": self.previous_raw,
            "revised_raw": self.revised_raw,
            "unit": self.unit,
            "surprise_z": self.surprise_z,
            "surprise_label": self.surprise_label,
            "surprise_impact": self.surprise_impact,
            "source": self.source,
        }


class PolicyRate(Base):
    """Tasso di policy corrente per valuta (SEZIONE 4.2)."""

    __tablename__ = "policy_rates"

    id: Mapped[int] = mapped_column(primary_key=True)
    currency: Mapped[str] = mapped_column(String(8), unique=True, index=True)
    central_bank: Mapped[str] = mapped_column(String(16))
    rate: Mapped[float] = mapped_column(Float)
    previous_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    effective_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    next_meeting: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    source: Mapped[str] = mapped_column(String(64), default="manuale")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "currency": self.currency,
            "central_bank": self.central_bank,
            "rate": self.rate,
            "previous_rate": self.previous_rate,
            "effective_date": self.effective_date.isoformat() if self.effective_date else None,
            "next_meeting": self.next_meeting.isoformat() if self.next_meeting else None,
            "source": self.source,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class CBDocument(Base):
    """Documento di banca centrale analizzato dal layer semantico (SEZIONE 3.4)."""

    __tablename__ = "cb_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    bank: Mapped[str] = mapped_column(String(16), index=True)
    currency: Mapped[str] = mapped_column(String(8), index=True)
    doc_type: Mapped[str] = mapped_column(String(24), default="PRESS_RELEASE")
    title: Mapped[str] = mapped_column(String(512))
    published_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    body: Mapped[str] = mapped_column(Text, default="")

    tone_score: Mapped[float] = mapped_column(Float, default=0.0)
    tone_label: Mapped[str] = mapped_column(String(16), default="NEUTRAL")
    themes: Mapped[Dict[str, Any]] = mapped_column(JSON, default=list)
    summary_it: Mapped[str] = mapped_column(Text, default="")
    classifier: Mapped[str] = mapped_column(String(24), default="rule_based")
    matched_terms: Mapped[Dict[str, Any]] = mapped_column(JSON, default=list)

    source: Mapped[str] = mapped_column(String(48), default="rss")
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "bank": self.bank,
            "currency": self.currency,
            "doc_type": self.doc_type,
            "title": self.title,
            "url": self.url,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "tone_score": round(self.tone_score, 1),
            "tone_label": self.tone_label,
            "themes": self.themes,
            "summary_it": self.summary_it,
            "classifier": self.classifier,
            "matched_terms": self.matched_terms,
        }


class CurrencyScoreSnapshot(Base):
    """Storico degli score valutari (SEZIONE 3.2), uno per ciclo di calcolo."""

    __tablename__ = "currency_scores"

    id: Mapped[int] = mapped_column(primary_key=True)
    currency: Mapped[str] = mapped_column(String(8), index=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    composite: Mapped[float] = mapped_column(Float)
    subscores: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    chains: Mapped[Dict[str, Any]] = mapped_column(JSON, default=list)
    data_quality: Mapped[float] = mapped_column(Float, default=0.0)
    regime: Mapped[str] = mapped_column(String(24), default="HAWKISH_DOMINANT")

    __table_args__ = (Index("ix_scores_ccy_time", "currency", "computed_at"),)


class Scenario(Base):
    """Scenario probabilistico generato (SEZIONE 3.5), storicizzato per calibrazione."""

    __tablename__ = "scenarios"

    id: Mapped[int] = mapped_column(primary_key=True)
    pair: Mapped[str] = mapped_column(String(16), index=True)
    horizon: Mapped[str] = mapped_column(String(16), index=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    horizon_end: Mapped[datetime] = mapped_column(DateTime, index=True)

    bias: Mapped[float] = mapped_column(Float)
    bias_label: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[str] = mapped_column(String(8))
    confidence_score: Mapped[float] = mapped_column(Float, default=0.0)

    base_direction: Mapped[str] = mapped_column(String(12))
    base_probability: Mapped[float] = mapped_column(Float)
    alt_a_probability: Mapped[float] = mapped_column(Float)
    alt_b_probability: Mapped[float] = mapped_column(Float)

    payload: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    rewritable: Mapped[bool] = mapped_column(Boolean, default=False)

    outcome: Mapped[Optional["ScenarioOutcome"]] = relationship(
        back_populates="scenario", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_scenarios_pair_horizon_time", "pair", "horizon", "generated_at"),)


class ScenarioOutcome(Base):
    """Valutazione ex-post di uno scenario (SEZIONE 7)."""

    __tablename__ = "scenario_outcomes"

    id: Mapped[int] = mapped_column(primary_key=True)
    scenario_id: Mapped[int] = mapped_column(ForeignKey("scenarios.id"), unique=True, index=True)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    price_start: Mapped[float] = mapped_column(Float)
    price_end: Mapped[float] = mapped_column(Float)
    change_pct: Mapped[float] = mapped_column(Float)
    realized_direction: Mapped[str] = mapped_column(String(12))
    base_hit: Mapped[bool] = mapped_column(Boolean)
    brier: Mapped[float] = mapped_column(Float)
    # Banda neutra (in %) usata per classificare QUESTO esito: adattiva su
    # volatilita' e orizzonte, quindi diversa da scenario a scenario. NULL sugli
    # esiti valutati prima dell'introduzione (banda fissa 0.15%).
    neutral_band_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    scenario: Mapped[Scenario] = relationship(back_populates="outcome")


class PriceBar(Base):
    """Prezzi di chiusura per la calibrazione ex-post (import CSV da MT5)."""

    __tablename__ = "prices"

    id: Mapped[int] = mapped_column(primary_key=True)
    pair: Mapped[str] = mapped_column(String(16), index=True)
    timestamp_utc: Mapped[datetime] = mapped_column(DateTime, index=True)
    close: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(48), default="csv")

    __table_args__ = (UniqueConstraint("pair", "timestamp_utc", name="uq_price_pair_ts"),)


class SourceHealth(Base):
    """Stato dei collector (SEZIONE 4.4 / dashboard 'Fonti & Salute sistema')."""

    __tablename__ = "source_health"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    label: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(12), default="UNKNOWN")  # OK/DEGRADED/DOWN
    last_run: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_success: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    latency_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    items: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "status": self.status,
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "last_success": self.last_success.isoformat() if self.last_success else None,
            "latency_ms": round(self.latency_ms, 1) if self.latency_ms is not None else None,
            "items": self.items,
            "message": self.message,
            "consecutive_failures": self.consecutive_failures,
        }
