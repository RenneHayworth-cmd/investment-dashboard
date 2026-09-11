from typing import Any, Literal
from pydantic import BaseModel, Field


class Instrument(BaseModel):
    category: str
    code: str
    name: str
    status: str
    source: str
    latest_date: str
    cache_time: str
    metrics: dict[str, Any]
    error: str
    formal_history_valid: bool


class Strategy(BaseModel):
    summary: dict[str, Any] = Field(default_factory=dict)
    daily: list[dict[str, Any]] = Field(default_factory=list)
    positions: list[dict[str, Any]] = Field(default_factory=list)
    trades: list[dict[str, Any]] = Field(default_factory=list)
    components: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class StrategyLive(BaseModel):
    mode: Literal['unavailable', 'intraday', 'pending_close', 'formal']
    available: bool
    complete: bool
    valuation_date: str
    formal_date: str
    quote_time: str
    missing_codes: list[str]
    daily_pnl: float | None
    estimated_assets: float | None
    warnings: list[str]


class Dashboard(BaseModel):
    schema_version: int = 2
    generated_at: str
    expected_formal_date: str
    session: str
    formal_dates: dict[str, str]
    formal_updated_at: str
    quote_time: str
    preview_codes: list[str]
    missing_formal_codes: list[str]
    missing_quote_codes: list[str]
    missing_index_codes: list[str]
    items: list[Instrument]
    etf_formal: list[dict[str, Any]]
    etf_preview: list[dict[str, Any]]
    index_formal: list[dict[str, Any]]
    index_preview: list[dict[str, Any]]
    guidance: list[dict[str, Any]]
    strategy_parameters: dict[str, Any]
    strategy: Strategy
    strategy_live: StrategyLive
    trade_preview: dict[str, Any]
    derivatives: list[Instrument]
    spreads: list[Instrument]
    refreshing: bool = False
    refresh_error: str = ""
    refresh_stage: str = "等待初始化"
    last_refresh_at: str = ""
