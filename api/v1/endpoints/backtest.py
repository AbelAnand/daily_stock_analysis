# -*- coding: utf-8 -*-
"""Backtest endpoints."""

from __future__ import annotations

import logging
from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import get_database_manager
from api.v1.schemas.backtest import (
    BacktestRunRequest,
    BacktestRunResponse,
    BacktestResultItem,
    BacktestResultsResponse,
    PerformanceMetrics,
)
from api.v1.schemas.common import ErrorResponse
from src.services.backtest_service import BacktestService
from src.storage import DatabaseManager

logger = logging.getLogger(__name__)

router = APIRouter()

BacktestAnalysisPhaseQuery = Literal["premarket", "intraday", "postmarket", "unknown"]


def _validate_analysis_date_range(
    analysis_date_from: Optional[date],
    analysis_date_to: Optional[date],
) -> None:
    if analysis_date_from and analysis_date_to and analysis_date_from > analysis_date_to:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_params",
                "message": "analysis_date_from cannot be after analysis_date_to",
            },
        )


@router.post(
    "/run",
    response_model=BacktestRunResponse,
    responses={
        200: {"description": "Backtest completed"},
        400: {"description": "Invalid request parameters", "model": ErrorResponse},
        500: {"description": "Server error", "model": ErrorResponse},
    },
    summary="Trigger backtest",
    description="Run backtest evaluation on historical analysis records and write to backtest_results/backtest_summaries",
)
def run_backtest(
    request: BacktestRunRequest,
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> BacktestRunResponse:
    try:
        _validate_analysis_date_range(request.analysis_date_from, request.analysis_date_to)
        service = BacktestService(db_manager)
        stats = service.run_backtest(
            code=request.code,
            force=request.force,
            eval_window_days=request.eval_window_days,
            min_age_days=request.min_age_days,
            analysis_date_from=request.analysis_date_from,
            analysis_date_to=request.analysis_date_to,
            limit=request.limit,
        )
        return BacktestRunResponse(**stats)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_params", "message": str(exc)},
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Backtest execution failed: {exc}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"Backtest execution failed: {str(exc)}"},
        )


@router.get(
    "/results",
    response_model=BacktestResultsResponse,
    responses={
        200: {"description": "Backtest result list"},
        400: {"description": "Invalid request parameters", "model": ErrorResponse},
        500: {"description": "Server error", "model": ErrorResponse},
    },
    summary="Get backtest results",
    description="Paginated backtest results, filterable by stock code",
)
def get_backtest_results(
    code: Optional[str] = Query(None, description="Filter by stock code"),
    eval_window_days: Optional[int] = Query(None, ge=1, le=120, description="Filter by evaluation window"),
    analysis_date_from: Optional[date] = Query(None, description="Analysis date from (inclusive)"),
    analysis_date_to: Optional[date] = Query(None, description="Analysis date to (inclusive)"),
    analysis_phase: Optional[BacktestAnalysisPhaseQuery] = Query(None, description="Filter by analysis phase: premarket/intraday/postmarket/unknown"),
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(20, ge=1, le=200, description="Items per page"),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> BacktestResultsResponse:
    try:
        _validate_analysis_date_range(analysis_date_from, analysis_date_to)
        service = BacktestService(db_manager)
        data = service.get_recent_evaluations(
            code=code,
            eval_window_days=eval_window_days,
            limit=limit,
            page=page,
            analysis_date_from=analysis_date_from,
            analysis_date_to=analysis_date_to,
            analysis_phase=analysis_phase,
        )
        items = [BacktestResultItem(**item) for item in data.get("items", [])]
        return BacktestResultsResponse(
            total=int(data.get("total", 0)),
            page=page,
            limit=limit,
            items=items,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_params", "message": str(exc)},
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to query backtest results: {exc}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"Failed to query backtest results: {str(exc)}"},
        )


@router.get(
    "/performance",
    response_model=PerformanceMetrics,
    responses={
        200: {"description": "Overall backtest performance"},
        400: {"description": "Invalid request parameters", "model": ErrorResponse},
        404: {"description": "No backtest summary", "model": ErrorResponse},
        500: {"description": "Server error", "model": ErrorResponse},
    },
    summary="Get overall backtest performance",
)
def get_overall_performance(
    eval_window_days: Optional[int] = Query(None, ge=1, le=120, description="Filter by evaluation window"),
    analysis_date_from: Optional[date] = Query(None, description="Analysis date from (inclusive)"),
    analysis_date_to: Optional[date] = Query(None, description="Analysis date to (inclusive)"),
    analysis_phase: Optional[BacktestAnalysisPhaseQuery] = Query(None, description="Filter by analysis phase: premarket/intraday/postmarket/unknown"),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> PerformanceMetrics:
    try:
        _validate_analysis_date_range(analysis_date_from, analysis_date_to)
        service = BacktestService(db_manager)
        summary = service.get_summary(
            scope="overall",
            code=None,
            eval_window_days=eval_window_days,
            analysis_date_from=analysis_date_from,
            analysis_date_to=analysis_date_to,
            analysis_phase=analysis_phase,
        )
        if summary is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": "Overall backtest summary not found"},
            )
        return PerformanceMetrics(**summary)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_params", "message": str(exc)},
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to query overall performance: {exc}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"Failed to query overall performance: {str(exc)}"},
        )


@router.get(
    "/performance/{code}",
    response_model=PerformanceMetrics,
    responses={
        200: {"description": "Per-stock backtest performance"},
        400: {"description": "Invalid request parameters", "model": ErrorResponse},
        404: {"description": "No backtest summary", "model": ErrorResponse},
        500: {"description": "Server error", "model": ErrorResponse},
    },
    summary="Get per-stock backtest performance",
)
def get_stock_performance(
    code: str,
    eval_window_days: Optional[int] = Query(None, ge=1, le=120, description="Filter by evaluation window"),
    analysis_date_from: Optional[date] = Query(None, description="Analysis date from (inclusive)"),
    analysis_date_to: Optional[date] = Query(None, description="Analysis date to (inclusive)"),
    analysis_phase: Optional[BacktestAnalysisPhaseQuery] = Query(None, description="Filter by analysis phase: premarket/intraday/postmarket/unknown"),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> PerformanceMetrics:
    try:
        _validate_analysis_date_range(analysis_date_from, analysis_date_to)
        service = BacktestService(db_manager)
        summary = service.get_summary(
            scope="stock",
            code=code,
            eval_window_days=eval_window_days,
            analysis_date_from=analysis_date_from,
            analysis_date_to=analysis_date_to,
            analysis_phase=analysis_phase,
        )
        if summary is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": f"Backtest summary not found for {code}"},
            )
        return PerformanceMetrics(**summary)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_params", "message": str(exc)},
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to query per-stock performance: {exc}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"Failed to query per-stock performance: {str(exc)}"},
        )
