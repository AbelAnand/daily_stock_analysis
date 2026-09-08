# -*- coding: utf-8 -*-
"""Paper-trading dashboard endpoints: live account view and manual position closes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from api.deps import get_paper_dashboard_service
from api.v1.errors import api_error
from api.v1.schemas.common import ErrorResponse
from api.v1.schemas.paper_trading import (
    PaperCloseAllResponse,
    PaperCloseResult,
    PaperDashboardResponse,
)
from src.services.paper_dashboard_service import (
    PaperDashboardService,
    PaperTradingUnavailableError,
    PositionNotFoundError,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _unavailable(exc: Exception) -> HTTPException:
    return api_error(
        503,
        "paper_trading_unavailable",
        "No reachable Alpaca paper account. Configure ALPACA_<LABEL>_KEY_ID / _SECRET_KEY and restart.",
        detail=str(exc)[:300],
    )


def _internal_error(message: str, exc: Exception) -> HTTPException:
    logger.error(f"{message}: {exc}", exc_info=True)
    return api_error(500, "internal_error", f"{message}: {str(exc)[:300]}")


@router.get(
    "/dashboard",
    response_model=PaperDashboardResponse,
    responses={503: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Paper-trading dashboard: account, P&L, open positions, open orders, recent activity",
)
def get_dashboard(
    service: PaperDashboardService = Depends(get_paper_dashboard_service),
) -> PaperDashboardResponse:
    try:
        return PaperDashboardResponse(**service.snapshot())
    except PaperTradingUnavailableError as exc:
        raise _unavailable(exc)
    except Exception as exc:
        raise _internal_error("Failed to load paper-trading dashboard", exc)


@router.post(
    "/positions/{symbol}/close",
    response_model=PaperCloseResult,
    responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Close one open paper position at market (cancels its bracket legs first)",
)
def close_position(
    symbol: str,
    service: PaperDashboardService = Depends(get_paper_dashboard_service),
) -> PaperCloseResult:
    try:
        return PaperCloseResult(**service.close_position(symbol))
    except PositionNotFoundError as exc:
        raise api_error(404, "position_not_found", str(exc))
    except PaperTradingUnavailableError as exc:
        raise _unavailable(exc)
    except Exception as exc:
        raise _internal_error(f"Failed to close {symbol.upper()}", exc)


@router.post(
    "/positions/close-all",
    response_model=PaperCloseAllResponse,
    responses={503: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Close every open paper position at market (per-symbol results; partial failures reported)",
)
def close_all_positions(
    service: PaperDashboardService = Depends(get_paper_dashboard_service),
) -> PaperCloseAllResponse:
    try:
        return PaperCloseAllResponse(**service.close_all_positions())
    except PaperTradingUnavailableError as exc:
        raise _unavailable(exc)
    except Exception as exc:
        raise _internal_error("Failed to close all positions", exc)
