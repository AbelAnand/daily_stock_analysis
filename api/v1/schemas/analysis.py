# -*- coding: utf-8 -*-
"""
===================================
分析相关模型
===================================

职责：
1. 定义分析请求和响应模型
2. 定义任务状态模型
3. 定义异步任务队列相关模型
"""

from typing import Optional, List, Any, Literal
from enum import Enum

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator
from src.utils.analysis_metadata import SELECTION_SOURCE_PATTERN
from src.utils.market_review_region import normalize_market_review_region_strict


class TaskStatusEnum(str, Enum):
    """任务状态枚举"""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


AnalysisPhase = Literal["auto", "premarket", "intraday", "postmarket"]


class AnalyzeRequest(BaseModel):
    """Analysis request parameters"""
    
    stock_code: Optional[str] = Field(
        None, 
        description="Single stock code", 
        json_schema_extra={"example": "600519"},
    )
    stock_codes: Optional[List[str]] = Field(
        None, 
        description="Multiple stock codes (mutually exclusive with stock_code)",
        json_schema_extra={"example": ["600519", "000858"]},
    )
    report_type: str = Field(
        "detailed",
        description="Report type: simple / detailed / full / brief",
        pattern="^(simple|detailed|full|brief)$",
    )
    force_refresh: bool = Field(
        False,
        description="Force refresh (ignore cache)"
    )
    async_mode: bool = Field(
        False,
        description="Use async mode"
    )
    analysis_phase: AnalysisPhase = Field(
        "auto",
        description="Analysis phase override: auto (inferred) / premarket / intraday / postmarket",
    )
    stock_name: Optional[str] = Field(
        None,
        description="Stock name selected by the user (provided by autocomplete)",
        json_schema_extra={"example": "Kweichow Moutai"},
    )
    original_query: Optional[str] = Field(
        None,
        description="Original user input (e.g. Moutai, gzmt, 600519)",
        json_schema_extra={"example": "Moutai"},
    )
    selection_source: Optional[str] = Field(
        None,
        description="Stock selection source: manual | autocomplete | import | image (image recognition)",
        pattern=SELECTION_SOURCE_PATTERN,
        json_schema_extra={"example": "autocomplete"},
    )
    notify: bool = Field(
        True,
        description="Send push notification (Telegram/WeCom etc.)"
    )
    report_language: Optional[Literal["zh", "en", "ko"]] = Field(
        None,
        validation_alias=AliasChoices("report_language", "reportLanguage"),
        description="Report output language for this analysis; defaults to the global REPORT_LANGUAGE",
    )
    skills: Optional[List[str]] = Field(
        None,
        validation_alias=AliasChoices("skills", "strategies"),
        description="Strategy skill IDs used for this analysis; compatible with the legacy strategies field",
        json_schema_extra={"example": ["bull_trend", "growth_quality"]},
    )

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "stock_code": "600519",
            "report_type": "detailed",
            "force_refresh": False,
            "async_mode": False,
            "analysis_phase": "auto",
            "stock_name": "Kweichow Moutai",
            "original_query": "Moutai",
            "selection_source": "autocomplete",
            "notify": True,
            "report_language": "zh",
            "skills": ["bull_trend"]
        }
    })


class MarketReviewRequest(BaseModel):
    """Market review trigger parameters."""

    send_notification: bool = Field(
        True,
        description="Send push notification after the market review completes",
    )
    report_language: Optional[Literal["zh", "en", "ko"]] = Field(
        None,
        validation_alias=AliasChoices("report_language", "reportLanguage"),
        description="Report output language for this market review; defaults to the global REPORT_LANGUAGE",
    )
    region: Optional[str] = Field(
        None,
        min_length=1,
        max_length=64,
        description=(
            "Market coverage for this market review. Valid tokens are cn, hk, us, jp, kr, both; "
            "both must be used alone, other tokens may be comma-combined. Input is case-insensitive, whitespace around tokens is ignored, "
            "tokens are deduplicated and sorted as cn,hk,us,jp,kr; empty values, empty tokens, unknown tokens, mixing both with others, or exceeding "
            "64 characters return 4xx for the whole request with no partial execution. Defaults to the runtime global MARKET_REVIEW_REGION."
        ),
        json_schema_extra={
            "example": "cn,us",
            "examples": ["cn", "jp,kr", "both"],
        },
    )

    @field_validator("region")
    @classmethod
    def normalize_region(cls, value: Optional[str]) -> Optional[str]:
        """Strictly validate request input and return its canonical ordering."""
        if value is None:
            return None
        return normalize_market_review_region_strict(value)


class MarketReviewAccepted(BaseModel):
    """Market review background task accepted response."""

    status: str = Field("accepted", description="Submission status")
    message: str = Field(..., description="Message")
    send_notification: bool = Field(..., description="Whether a notification is sent")
    region: str = Field(
        ...,
        description="Canonical market scope actually used by this task",
        examples=["us", "jp,kr"],
    )
    trace_id: Optional[str] = Field(
        None,
        description="Diagnostic trace ID of this background task",
    )
    task_id: Optional[str] = Field(
        None,
        description="Task ID (only returned when the task was actually submitted)",
    )


class AnalysisResultResponse(BaseModel):
    """分析结果响应模型"""
    
    query_id: str = Field(..., description="Unique identifier of the analysis record")
    trace_id: Optional[str] = Field(None, description="Diagnostic trace ID")
    stock_code: str = Field(..., description="Stock code")
    stock_name: Optional[str] = Field(None, description="Stock name")
    report: Optional[Any] = Field(None, description="Analysis report")
    diagnostic_summary: Optional[Any] = Field(None, description="Run diagnostic summary")
    created_at: str = Field(..., description="Created at")
    
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "query_id": "abc123def456",
            "stock_code": "600519",
            "stock_name": "Kweichow Moutai",
            "report": {
                "summary": {
                    "sentiment_score": 75,
                    "operation_advice": "Hold"
                }
            },
            "created_at": "2024-01-01T12:00:00"
        }
    })


class TaskAccepted(BaseModel):
    """异步任务接受响应"""
    
    task_id: str = Field(..., description="Task ID, used to query status")
    trace_id: Optional[str] = Field(None, description="Diagnostic trace ID")
    status: str = Field(
        ..., 
        description="Task status",
        pattern="^(pending|processing)$"
    )
    message: Optional[str] = Field(None, description="Message")
    analysis_phase: AnalysisPhase = Field("auto", description="Requested analysis phase")
    
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "task_id": "task_abc123",
            "status": "pending",
            "message": "Analysis task accepted",
            "analysis_phase": "auto"
        }
    })


class BatchTaskAcceptedItem(BaseModel):
    """批量异步任务中的单个成功提交项。"""

    task_id: str = Field(..., description="Task ID, used to query status")
    trace_id: Optional[str] = Field(None, description="Diagnostic trace ID")
    stock_code: str = Field(..., description="Stock code")
    status: str = Field(
        ...,
        description="Task status",
        pattern="^(pending|processing)$"
    )
    message: Optional[str] = Field(None, description="Message")
    analysis_phase: AnalysisPhase = Field("auto", description="Requested analysis phase")

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "task_id": "task_abc123",
            "stock_code": "600519",
            "status": "pending",
            "message": "Analysis task queued: 600519",
            "analysis_phase": "auto"
        }
    })


class BatchDuplicateTaskItem(BaseModel):
    """批量异步任务中的重复提交项。"""

    stock_code: str = Field(..., description="Stock code")
    existing_task_id: str = Field(..., description="Existing task ID")
    message: str = Field(..., description="Error message")

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "stock_code": "600519",
            "existing_task_id": "task_existing_123",
            "message": "Stock 600519 is already being analyzed (task_id: task_existing_123)"
        }
    })


class BatchTaskAcceptedResponse(BaseModel):
    """批量异步任务接受响应。"""

    accepted: List[BatchTaskAcceptedItem] = Field(default_factory=list, description="Successfully submitted tasks")
    duplicates: List[BatchDuplicateTaskItem] = Field(default_factory=list, description="Tasks skipped as duplicates")
    message: str = Field(..., description="Summary message")

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "accepted": [
                {
                    "task_id": "task_abc123",
                    "stock_code": "600519",
                    "status": "pending",
                    "message": "Analysis task queued: 600519",
                    "analysis_phase": "auto"
                }
            ],
            "duplicates": [
                {
                    "stock_code": "000858",
                    "existing_task_id": "task_existing_456",
                    "message": "Stock 000858 is already being analyzed (task_id: task_existing_456)"
                }
            ],
            "message": "Submitted 1 task, skipped 1 duplicate"
        }
    })


class TaskStatus(BaseModel):
    """Task status model"""
    
    task_id: str = Field(..., description="Task ID")
    trace_id: Optional[str] = Field(None, description="Diagnostic trace ID")
    status: TaskStatusEnum = Field(
        ..., 
        description="Task status",
    )
    progress: Optional[int] = Field(
        None, 
        description="Progress percent (0-100)",
        ge=0,
        le=100
    )
    result: Optional[AnalysisResultResponse] = Field(
        None, 
        description="Analysis result (only present when completed)"
    )
    market_review_report: Optional[str] = Field(
        None,
        description="Report text returned by the market review task (market review tasks only)",
    )
    market_review_payload: Optional[Any] = Field(
        None,
        description="Structured market-review payload for API/Web consumers.",
    )
    region: Optional[str] = Field(
        None,
        description="Canonical market scope actually used by the market review task",
    )
    error: Optional[str] = Field(
        None, 
        description="Error message (only present when failed)"
    )
    stock_name: Optional[str] = Field(None, description="Stock name")
    original_query: Optional[str] = Field(None, description="Original user input")
    selection_source: Optional[str] = Field(
        None,
        description="Selection source",
        pattern=SELECTION_SOURCE_PATTERN,
    )
    analysis_phase: Optional[AnalysisPhase] = Field(
        None,
        description="Requested analysis phase; may be empty for legacy DB fallback records without a persisted field",
    )
    skills: Optional[List[str]] = Field(None, description="Strategy skill IDs used by this task")
    
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "task_id": "task_abc123",
            "status": "completed",
            "progress": 100,
            "result": None,
            "market_review_report": None,
            "error": None,
            "stock_name": "Kweichow Moutai",
            "original_query": "Moutai",
            "selection_source": "autocomplete",
            "analysis_phase": "auto",
            "skills": ["bull_trend"]
        }
    })


class TaskInfo(BaseModel):
    """
    Task details model

    Used for task list and SSE event delivery
    """
    
    task_id: str = Field(..., description="Task ID")
    trace_id: Optional[str] = Field(None, description="Diagnostic trace ID")
    stock_code: str = Field(..., description="Stock code")
    stock_name: Optional[str] = Field(None, description="Stock name")
    status: TaskStatusEnum = Field(..., description="Task status")
    progress: int = Field(0, description="Progress percent (0-100)", ge=0, le=100)
    message: Optional[str] = Field(None, description="Status message")
    report_type: str = Field("detailed", description="Report type")
    created_at: str = Field(..., description="Created at")
    started_at: Optional[str] = Field(None, description="Started at")
    completed_at: Optional[str] = Field(None, description="Completed at")
    error: Optional[str] = Field(None, description="Error message (only present when failed)")
    original_query: Optional[str] = Field(None, description="Original user input")
    selection_source: Optional[str] = Field(
        None,
        description="Selection source",
        pattern=SELECTION_SOURCE_PATTERN,
    )
    analysis_phase: AnalysisPhase = Field("auto", description="Requested analysis phase")
    skills: Optional[List[str]] = Field(None, description="Strategy skill IDs used by this task")
    region: Optional[str] = Field(
        None,
        description="Canonical market scope actually used by the market review task",
    )
    
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "task_id": "abc123def456",
            "stock_code": "600519",
            "stock_name": "Kweichow Moutai",
            "status": "processing",
            "progress": 50,
            "message": "Analyzing...",
            "report_type": "detailed",
            "created_at": "2026-02-05T10:30:00",
            "started_at": "2026-02-05T10:30:01",
            "completed_at": None,
            "error": None,
            "original_query": "Moutai",
            "selection_source": "autocomplete",
            "analysis_phase": "auto",
            "skills": ["bull_trend"]
        }
    })


class TaskListResponse(BaseModel):
    """任务列表响应模型"""
    
    total: int = Field(..., description="Total number of tasks")
    pending: int = Field(..., description="Number of pending tasks")
    processing: int = Field(..., description="Number of processing tasks")
    tasks: List[TaskInfo] = Field(..., description="Task list")
    
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "total": 3,
            "pending": 1,
            "processing": 2,
            "tasks": []
        }
    })


class DuplicateTaskErrorResponse(BaseModel):
    """重复任务错误响应模型"""
    
    error: str = Field("duplicate_task", description="Error type")
    message: str = Field(..., description="Error message")
    stock_code: str = Field(..., description="Stock code")
    existing_task_id: str = Field(..., description="Existing task ID")
    
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "error": "duplicate_task",
            "message": "Stock 600519 is already being analyzed",
            "stock_code": "600519",
            "existing_task_id": "abc123def456"
        }
    })
