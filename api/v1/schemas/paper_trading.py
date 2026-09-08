# -*- coding: utf-8 -*-
"""Schemas for the paper-trading dashboard API (Alpaca paper account)."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class PaperTradingStatus(BaseModel):
    enabled: bool = Field(..., description="PAPER_TRADING_ENABLED: automated execution after the daily run")
    dry_run: bool
    kill_switch_active: bool
    allow_short: bool
    market_open: bool
    next_open: Optional[str] = None
    next_close: Optional[str] = None
    max_positions: int


class PaperAccount(BaseModel):
    label: str = ""
    account_number: str
    equity: float
    last_equity: float = Field(0.0, description="Equity at the previous close (day P&L anchor)")
    cash: float = 0.0
    buying_power: float = 0.0
    portfolio_value: float = 0.0
    long_market_value: float = 0.0
    short_market_value: float = 0.0
    created_at: Optional[str] = None


class PaperPnl(BaseModel):
    total_pnl: float = Field(..., description="Bot P&L: realized + unrealized since the first bot entry")
    total_pnl_pct: float = 0.0
    total_pnl_basis: str = Field(
        ..., description="'account_equity_since_first_trade' | 'realized_plus_unrealized'"
    )
    since: Optional[str] = Field(None, description="ISO date of the first real bot entry")
    baseline_equity: Optional[float] = None
    day_pnl: float = 0.0
    day_pnl_pct: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = Field(0.0, description="Sum of closed-trade P&L from the post-mortem loop")
    closed_trades: int = 0
    wins: int = 0
    losses: int = 0


class PaperPnlPoint(BaseModel):
    date: str = Field(..., description="ISO date (trading day)")
    equity: float
    pnl: float = Field(..., description="Cumulative bot P&L at the end of that day (last point is live)")


class PaperExposure(BaseModel):
    open_positions: int
    gross_exposure: float
    gross_exposure_pct: float
    long_market_value: float = 0.0
    short_market_value: float = 0.0


class PaperPositionItem(BaseModel):
    symbol: str
    side: str = Field(..., description="'long' | 'short'")
    qty: float
    avg_entry: float
    current_price: Optional[float] = None
    market_value: float
    cost_basis: float = 0.0
    unrealized_pl: float
    unrealized_plpc: float = Field(0.0, description="Fraction, 0.012 = +1.2%")
    unrealized_intraday_pl: float = 0.0
    change_today: float = Field(0.0, description="Fraction")
    stop_price: Optional[float] = Field(None, description="Live protective stop leg, if any")
    target_price: Optional[float] = None
    initial_stop: Optional[float] = Field(None, description="Stop recorded at entry (R anchor)")
    r_multiple: Optional[float] = None
    protected: bool = False


class PaperOpenOrderItem(BaseModel):
    id: str
    symbol: str
    side: str
    qty: float
    order_class: str = ""
    order_type: str = ""
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    status: str = ""
    submitted_at: Optional[str] = None


class PaperTradeActivityItem(BaseModel):
    id: int
    created_at: Optional[str] = None
    symbol: str
    action: Optional[str] = None
    side: Optional[str] = None
    qty: Optional[float] = None
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    status: str
    reason: Optional[str] = None
    order_id: Optional[str] = None
    dry_run: bool = False


class PaperDashboardResponse(BaseModel):
    generated_at: str
    status: PaperTradingStatus
    account: PaperAccount
    pnl: PaperPnl
    pnl_history: List[PaperPnlPoint] = Field(default_factory=list)
    exposure: PaperExposure
    positions: List[PaperPositionItem]
    open_orders: List[PaperOpenOrderItem]
    recent_trades: List[PaperTradeActivityItem]


class PaperCloseResult(BaseModel):
    symbol: str
    side: str = Field(..., description="'sell' (close long) | 'buy_to_cover' (close short)")
    qty: float
    status: str = Field(..., description="'submitted' | 'dry_run' | 'error'")
    order_id: Optional[str] = None
    message: str = ""


class PaperCloseAllResponse(BaseModel):
    requested: int
    closed: int
    failed: int
    results: List[PaperCloseResult]
