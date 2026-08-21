# -*- coding: utf-8 -*-
"""DecisionSignal API endpoints."""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Security
from fastapi.security import APIKeyCookie

from api.v1.schemas.common import ErrorResponse
from api.v1.schemas.decision_signals import (
    DecisionSignalCreateRequest,
    DecisionSignalFeedbackItem,
    DecisionSignalFeedbackRequest,
    DecisionSignalItem,
    DecisionSignalListResponse,
    DecisionSignalMutationResponse,
    DecisionSignalOutcomeListResponse,
    DecisionSignalOutcomeRunRequest,
    DecisionSignalOutcomeRunResponse,
    DecisionSignalOutcomeStatsResponse,
    DecisionSignalReassessRequest,
    DecisionSignalReassessErrorResponse,
    DecisionSignalReassessResponse,
    DecisionSignalStatusUpdateRequest,
)
from src.auth import COOKIE_NAME
from src.services.decision_signal_service import (
    DecisionSignalNotFoundError,
    DecisionSignalService,
    DecisionSignalStorageError,
)
from src.services.decision_signal_outcome_service import DecisionSignalOutcomeService
from src.services.decision_signal_reassess_service import (
    DecisionSignalReassessGuardrailBlockedError,
    DecisionSignalReassessService,
    DecisionSignalSourceReportNotFoundError,
    DecisionSignalUnsupportedReportSnapshotError,
    DecisionSignalUnsupportedReportTypeError,
)


logger = logging.getLogger(__name__)

admin_session_cookie = APIKeyCookie(
    name=COOKIE_NAME,
    scheme_name="AdminSessionCookie",
    auto_error=False,
)
router = APIRouter(dependencies=[Security(admin_session_cookie)])

AUTH_RESPONSE = {
    401: {
        "model": ErrorResponse,
        "description": "Not logged in or admin session invalid (when ADMIN_AUTH_ENABLED=true)",
    },
}


def _bad_request(exc: Exception, *, error: str = "validation_error") -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"error": error, "message": str(exc)},
    )


def _not_found(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"error": "not_found", "message": str(exc)},
    )


def _error(status_code: int, exc: Exception, *, error: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": error, "message": str(exc)},
    )


def _internal_error(message: str, exc: Exception) -> HTTPException:
    logger.error("%s: %s", message, exc, exc_info=True)
    return HTTPException(
        status_code=500,
        detail={"error": "internal_error", "message": message},
    )


def _guardrail_blocked(exc: DecisionSignalReassessGuardrailBlockedError) -> HTTPException:
    response = DecisionSignalReassessErrorResponse(
        error="guardrail_blocked",
        message="Reassessed decision signal was blocked by guardrail.",
        blocked_reason=exc.blocked_reason,
        warnings=exc.warnings,
    )
    return HTTPException(status_code=400, detail=response.model_dump())


@router.post(
    "",
    response_model=DecisionSignalMutationResponse,
    responses={
        **AUTH_RESPONSE,
        400: {"model": ErrorResponse, "description": "Invalid request fields"},
        422: {"model": ErrorResponse, "description": "Request body or path parameter validation failed"},
        500: {"model": ErrorResponse, "description": "Creation failed"},
    },
    summary="Create or deduplicate a decision signal",
    description=(
        "Explicitly write a DecisionSignal. If horizon/expires_at are omitted, the service fills in the default lifecycle; "
        "when the same-source dedup key or narrow relaxed dedup matches, the existing record is returned with created=false; "
        "creating an active signal or renewing an expired one invalidates older opposing active signals for the same stock, "
        "and an active duplicate retry re-runs that fix; ordinary old duplicate/replay is not treated as a new activation event; "
        "strict idempotency under concurrency is not guaranteed."
    ),
    operation_id="createDecisionSignal",
)
def create_signal(request: DecisionSignalCreateRequest) -> DecisionSignalMutationResponse:
    service = DecisionSignalService()
    try:
        payload = request.model_dump(exclude_unset=True)
        return DecisionSignalMutationResponse(**service.create_signal(payload))
    except DecisionSignalStorageError as exc:
        raise _internal_error("Create decision signal failed", exc)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Create decision signal failed", exc)


@router.get(
    "",
    response_model=DecisionSignalListResponse,
    responses={
        **AUTH_RESPONSE,
        400: {"model": ErrorResponse, "description": "Invalid query parameters"},
        422: {"model": ErrorResponse, "description": "Query parameter validation failed"},
        500: {"model": ErrorResponse, "description": "Query failed"},
    },
    summary="List decision signals",
    description=(
        "Paginated DecisionSignal query; active signals past expires_at are lazily expired before reading. "
        "When source_type=analysis and only source_report_id is given, if no signal matches, a one-time lazy backfill from that history report is attempted "
        "(only on the first list hit; this exact query triggers a history decision-signal backfill write, i.e. read-with-write behavior; "
        "other paginated list filter scenarios are unaffected). "
        "holding_only=true reads only cached portfolio_positions of the active account and does not trigger a portfolio snapshot replay."
    ),
    operation_id="listDecisionSignals",
)
def list_signals(
    market: Optional[str] = Query(None, description="Optional market filter: cn/hk/us/jp/kr/tw"),
    stock_code: Optional[str] = Query(None, description="Optional stock code filter"),
    action: Optional[str] = Query(None, description="Optional decision action filter"),
    market_phase: Optional[str] = Query(None, description="Optional market phase filter"),
    decision_profile: Optional[str] = Query(
        None,
        description="Optional decision profile filter: conservative/balanced/aggressive/unknown",
    ),
    source_type: Optional[str] = Query(None, description="Optional source type filter"),
    source_report_id: Optional[int] = Query(None, description="Optional source report id filter"),
    trace_id: Optional[str] = Query(None, description="Optional trace id filter"),
    trigger_source: Optional[str] = Query(None, description="Optional trigger source filter"),
    status: Optional[str] = Query(None, description="Optional status filter"),
    created_from: Optional[str] = Query(None, description="Inclusive created_at lower bound"),
    created_to: Optional[str] = Query(None, description="Inclusive created_at upper bound"),
    expires_from: Optional[str] = Query(None, description="Inclusive expires_at lower bound"),
    expires_to: Optional[str] = Query(None, description="Inclusive expires_at upper bound"),
    holding_only: bool = Query(False, description="Filter to active cached portfolio holdings only"),
    account_id: Optional[int] = Query(
        None,
        description="Optional active portfolio account id for holding_only",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> DecisionSignalListResponse:
    service = DecisionSignalService()
    try:
        return DecisionSignalListResponse(
            **service.list_signals(
                market=market,
                stock_code=stock_code,
                action=action,
                market_phase=market_phase,
                decision_profile=decision_profile,
                source_type=source_type,
                source_report_id=source_report_id,
                trace_id=trace_id,
                trigger_source=trigger_source,
                status=status,
                created_from=created_from,
                created_to=created_to,
                expires_from=expires_from,
                expires_to=expires_to,
                holding_only=holding_only,
                account_id=account_id,
                page=page,
                page_size=page_size,
            )
        )
    except DecisionSignalStorageError as exc:
        raise _internal_error("List decision signals failed", exc)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("List decision signals failed", exc)


@router.post(
    "/outcomes/run",
    response_model=DecisionSignalOutcomeRunResponse,
    responses={
        **AUTH_RESPONSE,
        400: {"model": ErrorResponse, "description": "Invalid request fields"},
        404: {"model": ErrorResponse, "description": "Signal not found"},
        422: {"model": ErrorResponse, "description": "Request body validation failed"},
        500: {"model": ErrorResponse, "description": "Outcome computation failed"},
    },
    summary="Trigger decision signal outcome evaluation",
    description=(
        "Explicitly trigger signal-level outcome computation; by default skips completed and terminal unable, "
        "but recomputes recoverable unable cases such as missing quote data; force=true recomputes and overwrites the same "
        "signal_id+horizon+engine_version。"
    ),
    operation_id="runDecisionSignalOutcomes",
)
def run_outcomes(request: DecisionSignalOutcomeRunRequest) -> DecisionSignalOutcomeRunResponse:
    service = DecisionSignalOutcomeService()
    try:
        return DecisionSignalOutcomeRunResponse(
            **service.run_outcomes(
                signal_id=request.signal_id,
                horizons=request.horizons,
                force=request.force,
                market=request.market,
                stock_code=request.stock_code,
                action=request.action,
                source_type=request.source_type,
                status=request.status,
                limit=request.limit,
            )
        )
    except DecisionSignalNotFoundError as exc:
        raise _not_found(exc)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Run decision signal outcomes failed", exc)


@router.get(
    "/outcomes",
    response_model=DecisionSignalOutcomeListResponse,
    responses={
        **AUTH_RESPONSE,
        400: {"model": ErrorResponse, "description": "Invalid query parameters"},
        422: {"model": ErrorResponse, "description": "Query parameter validation failed"},
        500: {"model": ErrorResponse, "description": "Query failed"},
    },
    summary="List decision signal outcomes",
    description="Paginated signal-level outcome query; by default only the current signal outcome engine_version is queried.",
    operation_id="listDecisionSignalOutcomes",
)
def list_outcomes(
    signal_id: Optional[int] = Query(None, gt=0),
    horizon: Optional[str] = Query(None),
    engine_version: Optional[str] = Query(None),
    eval_status: Optional[str] = Query(None),
    outcome: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> DecisionSignalOutcomeListResponse:
    service = DecisionSignalOutcomeService()
    try:
        return DecisionSignalOutcomeListResponse(
            **service.list_outcomes(
                signal_id=signal_id,
                horizon=horizon,
                engine_version=engine_version,
                eval_status=eval_status,
                outcome=outcome,
                page=page,
                page_size=page_size,
            )
        )
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("List decision signal outcomes failed", exc)


@router.get(
    "/outcomes/stats",
    response_model=DecisionSignalOutcomeStatsResponse,
    responses={
        **AUTH_RESPONSE,
        400: {"model": ErrorResponse, "description": "Invalid query parameters"},
        422: {"model": ErrorResponse, "description": "Query parameter validation failed"},
        500: {"model": ErrorResponse, "description": "Statistics failed"},
    },
    summary="Get decision signal outcome statistics",
    description="By default aggregates the current engine_version and excludes archived signals.",
    operation_id="getDecisionSignalOutcomeStats",
)
def get_outcome_stats(
    horizons: Optional[List[str]] = Query(None),
    engine_version: Optional[str] = Query(None),
    statuses: Optional[List[str]] = Query(None),
) -> DecisionSignalOutcomeStatsResponse:
    service = DecisionSignalOutcomeService()
    try:
        return DecisionSignalOutcomeStatsResponse(
            **service.get_stats(
                horizons=horizons,
                engine_version=engine_version,
                statuses=statuses,
            )
        )
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Get decision signal outcome stats failed", exc)


@router.post(
    "/reassess",
    response_model=DecisionSignalReassessResponse,
    responses={
        **AUTH_RESPONSE,
        400: {"model": DecisionSignalReassessErrorResponse, "description": "History report not applicable or persistence blocked by risk guardrail"},
        404: {"model": ErrorResponse, "description": "Source history report not found"},
        422: {"model": ErrorResponse, "description": "Request body validation failed"},
        500: {"model": ErrorResponse, "description": "Reassessment failed"},
    },
    summary="Reassess decision profile with optional persistence",
    description=(
        "Recompute the decision_profile signal from the persisted history report snapshot for source_report_id; "
        "persist=false returns a read-only preview, persist=true writes the guardrail-approved server-side result to DecisionSignal."
    ),
    operation_id="reassessDecisionSignalPreview",
)
def reassess_signal(request: DecisionSignalReassessRequest) -> DecisionSignalReassessResponse:
    service = DecisionSignalReassessService()
    try:
        return DecisionSignalReassessResponse(
            **service.reassess(
                source_report_id=request.source_report_id,
                decision_profile=request.decision_profile,
                persist=request.persist,
            )
        )
    except DecisionSignalSourceReportNotFoundError as exc:
        raise _error(404, exc, error="source_report_not_found")
    except DecisionSignalUnsupportedReportTypeError as exc:
        raise _error(400, exc, error="unsupported_report_type")
    except DecisionSignalUnsupportedReportSnapshotError as exc:
        raise _error(400, exc, error="unsupported_report_snapshot")
    except DecisionSignalReassessGuardrailBlockedError as exc:
        raise _guardrail_blocked(exc)
    except Exception as exc:
        raise _internal_error("Reassess decision signal failed", exc)


@router.get(
    "/latest/{stock_code}",
    response_model=DecisionSignalListResponse,
    responses={
        **AUTH_RESPONSE,
        400: {"model": ErrorResponse, "description": "Invalid request parameters"},
        422: {"model": ErrorResponse, "description": "Path or query parameter validation failed"},
        500: {"model": ErrorResponse, "description": "Query failed"},
    },
    summary="Get latest active decision signals for a stock",
    description="Return the latest active signals for the given stock; lazy expiry runs before reading.",
    operation_id="getLatestDecisionSignals",
)
def get_latest_active(
    stock_code: str,
    market: Optional[str] = Query(None, description="Optional market filter: cn/hk/us/jp/kr/tw"),
    limit: int = Query(1, ge=1, le=100),
) -> DecisionSignalListResponse:
    service = DecisionSignalService()
    try:
        return DecisionSignalListResponse(
            **service.get_latest_active(
                stock_code=stock_code,
                market=market,
                limit=limit,
            )
        )
    except DecisionSignalStorageError as exc:
        raise _internal_error("Get latest decision signals failed", exc)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Get latest decision signals failed", exc)


@router.get(
    "/{signal_id}",
    response_model=DecisionSignalItem,
    responses={
        **AUTH_RESPONSE,
        404: {"model": ErrorResponse, "description": "Signal not found"},
        422: {"model": ErrorResponse, "description": "Path parameter validation failed"},
        500: {"model": ErrorResponse, "description": "Query failed"},
    },
    summary="Get a decision signal",
    description="Get a single DecisionSignal by ID; lazy expiry runs before reading.",
    operation_id="getDecisionSignal",
)
def get_signal(signal_id: int) -> DecisionSignalItem:
    service = DecisionSignalService()
    try:
        return DecisionSignalItem(**service.get_signal(signal_id))
    except DecisionSignalNotFoundError as exc:
        raise _not_found(exc)
    except DecisionSignalStorageError as exc:
        raise _internal_error("Get decision signal failed", exc)
    except Exception as exc:
        raise _internal_error("Get decision signal failed", exc)


@router.get(
    "/{signal_id}/outcomes",
    response_model=DecisionSignalOutcomeListResponse,
    responses={
        **AUTH_RESPONSE,
        404: {"model": ErrorResponse, "description": "Signal not found"},
        422: {"model": ErrorResponse, "description": "Path parameter validation failed"},
        500: {"model": ErrorResponse, "description": "Query failed"},
    },
    summary="Get outcome for a decision signal",
    description="Return the outcome for the given signal_id under the current engine_version.",
    operation_id="listDecisionSignalOutcomesBySignal",
)
def list_signal_outcomes(signal_id: int) -> DecisionSignalOutcomeListResponse:
    service = DecisionSignalOutcomeService()
    try:
        return DecisionSignalOutcomeListResponse(**service.list_signal_outcomes(signal_id))
    except DecisionSignalNotFoundError as exc:
        raise _not_found(exc)
    except Exception as exc:
        raise _internal_error("List decision signal outcomes failed", exc)


@router.get(
    "/{signal_id}/feedback",
    response_model=DecisionSignalFeedbackItem,
    responses={
        **AUTH_RESPONSE,
        404: {"model": ErrorResponse, "description": "Signal not found"},
        422: {"model": ErrorResponse, "description": "Path parameter validation failed"},
        500: {"model": ErrorResponse, "description": "Query failed"},
    },
    summary="Get user feedback for a decision signal",
    description="Returns feedback_value=null when there is no feedback; 404 when the signal does not exist.",
    operation_id="getDecisionSignalFeedback",
)
def get_feedback(signal_id: int) -> DecisionSignalFeedbackItem:
    service = DecisionSignalOutcomeService()
    try:
        return DecisionSignalFeedbackItem(**service.get_feedback(signal_id))
    except DecisionSignalNotFoundError as exc:
        raise _not_found(exc)
    except Exception as exc:
        raise _internal_error("Get decision signal feedback failed", exc)


@router.put(
    "/{signal_id}/feedback",
    response_model=DecisionSignalFeedbackItem,
    responses={
        **AUTH_RESPONSE,
        400: {"model": ErrorResponse, "description": "Invalid request fields"},
        404: {"model": ErrorResponse, "description": "Signal not found"},
        422: {"model": ErrorResponse, "description": "Request body or path parameter validation failed"},
        500: {"model": ErrorResponse, "description": "Update failed"},
    },
    summary="Submit user feedback for a decision signal",
    description="Upsert the latest useful/not_useful feedback by signal_id.",
    operation_id="putDecisionSignalFeedback",
)
def put_feedback(signal_id: int, request: DecisionSignalFeedbackRequest) -> DecisionSignalFeedbackItem:
    service = DecisionSignalOutcomeService()
    try:
        return DecisionSignalFeedbackItem(
            **service.put_feedback(
                signal_id,
                feedback_value=request.feedback_value,
                reason_code=request.reason_code,
                note=request.note,
                source=request.source,
            )
        )
    except DecisionSignalNotFoundError as exc:
        raise _not_found(exc)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Put decision signal feedback failed", exc)


@router.patch(
    "/{signal_id}/status",
    response_model=DecisionSignalItem,
    responses={
        **AUTH_RESPONSE,
        400: {"model": ErrorResponse, "description": "Invalid status"},
        404: {"model": ErrorResponse, "description": "Signal not found"},
        422: {"model": ErrorResponse, "description": "Request body or path parameter validation failed"},
        500: {"model": ErrorResponse, "description": "Update failed"},
    },
    summary="Update decision signal status",
    description=(
        "Updates only valid statuses and optional metadata; omitting metadata keeps the existing value, null clears it, "
        "and an object replaces it wholesale while preserving the formal decision_profile identity. "
        "Terminal statuses such as expired/invalidated/closed/archived cannot be PATCHed back to active."
    ),
    operation_id="updateDecisionSignalStatus",
)
def update_status(signal_id: int, request: DecisionSignalStatusUpdateRequest) -> DecisionSignalItem:
    service = DecisionSignalService()
    try:
        return DecisionSignalItem(
            **service.update_status(
                signal_id,
                status=request.status,
                metadata=request.metadata,
                replace_metadata="metadata" in request.model_fields_set,
            )
        )
    except DecisionSignalNotFoundError as exc:
        raise _not_found(exc)
    except DecisionSignalStorageError as exc:
        raise _internal_error("Update decision signal status failed", exc)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Update decision signal status failed", exc)
