# -*- coding: utf-8 -*-
"""
===================================
历史记录相关模型
===================================

职责：
1. 定义历史记录列表和详情模型
2. 定义分析报告完整模型
"""

from typing import Optional, List, Any, Dict, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from api.v1.schemas.market_phase import MarketPhaseSummary
from src.schemas.decision_action import DecisionAction


class HistoryItem(BaseModel):
    """历史记录摘要（列表展示用）"""

    id: Optional[int] = Field(None, description="Primary key ID of the analysis history record")
    query_id: str = Field(..., description="query_id linked to the analysis record (shared across a batch analysis)")
    stock_code: str = Field(..., description="Stock code")
    stock_name: Optional[str] = Field(None, description="Stock name")
    report_type: Optional[str] = Field(None, description="Report type")
    region: Optional[str] = Field(
        None,
        description="Canonical market scope actually used by the market review",
    )
    trend_prediction: Optional[str] = Field(None, description="Trend prediction")
    analysis_summary: Optional[str] = Field(None, description="Analysis summary")
    sentiment_score: Optional[int] = Field(
        None,
        description="Sentiment score (historical data may fall outside 0-100; not constrained on read)",
    )
    operation_advice: Optional[str] = Field(None, description="Operation advice")
    action: Optional[DecisionAction] = Field(None, description="Structured action taxonomy")
    action_label: Optional[str] = Field(None, description="Display label for the suggested action")
    current_price: Optional[float] = Field(None, description="Price at analysis time")
    change_pct: Optional[float] = Field(None, description="Change percent at analysis time (%)")
    volume_ratio: Optional[float] = Field(None, description="Volume ratio at analysis time")
    turnover_rate: Optional[float] = Field(None, description="Turnover rate at analysis time")
    model_used: Optional[str] = Field(
        None,
        description="Model snapshot stored with the history record; for display only, not used for model configuration or runtime routing",
    )
    market_phase_summary: Optional[MarketPhaseSummary] = Field(
        None,
        description="Low-sensitivity market phase summary for this analysis",
    )
    created_at: Optional[str] = Field(None, description="Created at")
    
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "id": 1234,
            "query_id": "abc123",
            "stock_code": "600519",
            "stock_name": "Kweichow Moutai",
            "report_type": "detailed",
            "sentiment_score": 75,
            "operation_advice": "Hold",
            "created_at": "2024-01-01T12:00:00"
        }
    })


class HistoryListResponse(BaseModel):
    """历史记录列表响应"""
    
    total: int = Field(..., description="Total number of records")
    page: int = Field(..., description="Current page")
    limit: int = Field(..., description="Page size")
    items: List[HistoryItem] = Field(default_factory=list, description="Record list")
    
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "total": 100,
            "page": 1,
            "limit": 20,
            "items": []
        }
    })


class DeleteHistoryRequest(BaseModel):
    """删除历史记录请求"""

    record_ids: List[int] = Field(default_factory=list, description="Primary key IDs of history records to delete")


class DeleteHistoryResponse(BaseModel):
    """删除历史记录响应"""

    deleted: int = Field(..., description="Number of history records actually deleted")


class NewsIntelItem(BaseModel):
    """新闻情报条目"""

    title: str = Field(..., description="News title")
    snippet: str = Field("", description="News snippet (up to 200 characters)")
    url: str = Field(..., description="News URL")

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "title": "Company releases earnings flash: revenue up 20% YoY",
            "snippet": "The announcement shows quarterly revenue grew 20% YoY...",
            "url": "https://example.com/news/123"
        }
    })


class NewsIntelResponse(BaseModel):
    """新闻情报响应"""

    total: int = Field(..., description="Number of news items")
    items: List[NewsIntelItem] = Field(default_factory=list, description="News list")

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "total": 2,
            "items": []
        }
    })


class ReportMeta(BaseModel):
    """报告元信息"""

    model_config = ConfigDict(protected_namespaces=("model_validate", "model_dump"))

    id: Optional[int] = Field(None, description="Primary key ID of the analysis history record (history reports only)")
    query_id: str = Field(..., description="query_id linked to the analysis record (shared across a batch analysis)")
    stock_code: str = Field(..., description="Stock code")
    stock_name: Optional[str] = Field(None, description="Stock name")
    report_type: Optional[str] = Field(None, description="Report type")
    report_language: Optional[str] = Field(None, description="Report output language (zh/en)")
    created_at: Optional[str] = Field(None, description="Created at")
    current_price: Optional[float] = Field(None, description="Price at analysis time")
    change_pct: Optional[float] = Field(None, description="Change percent at analysis time (%)")
    model_used: Optional[str] = Field(
        None,
        description="Model snapshot from the report metadata; for display only, not used for runtime model calls or configuration routing",
    )
    market_phase_summary: Optional[MarketPhaseSummary] = Field(
        None,
        description="Low-sensitivity market phase summary for this analysis",
    )


class ReportSummary(BaseModel):
    """报告概览区"""
    
    analysis_summary: Optional[str] = Field(None, description="Key conclusion")
    operation_advice: Optional[str] = Field(None, description="Operation advice")
    action: Optional[DecisionAction] = Field(None, description="Structured action taxonomy")
    action_label: Optional[str] = Field(None, description="Display label for the suggested action")
    trend_prediction: Optional[str] = Field(None, description="Trend prediction")
    sentiment_score: Optional[int] = Field(
        None,
        description="Sentiment score (historical data may fall outside 0-100; not constrained on read)",
    )
    sentiment_label: Optional[str] = Field(None, description="Sentiment label")


class ReportStrategy(BaseModel):
    """策略点位区"""
    
    ideal_buy: Optional[str] = Field(None, description="Ideal buy price")
    secondary_buy: Optional[str] = Field(None, description="Secondary buy price")
    stop_loss: Optional[str] = Field(None, description="Stop-loss price")
    take_profit: Optional[str] = Field(None, description="Take-profit price")


class AnalysisContextPackOverviewSubject(BaseModel):
    """AnalysisContextPack 可见摘要标的信息"""

    code: str = Field(..., description="Stock code")
    stock_name: Optional[str] = Field(None, description="Stock name")
    market: Optional[str] = Field(None, description="Market")


class AnalysisContextPackOverviewBlock(BaseModel):
    """AnalysisContextPack 可见摘要数据块"""

    key: str = Field(..., description="Stable key of the data block")
    label: str = Field(..., description="Display name of the data block")
    status: Literal[
        "available",
        "missing",
        "not_supported",
        "fallback",
        "stale",
        "estimated",
        "partial",
        "fetch_failed",
    ] = Field(..., description="Quality status of the data block")
    source: Optional[str] = Field(None, description="Data source")
    warnings: List[str] = Field(default_factory=list, description="Warning codes of the data block")
    missing_reasons: List[str] = Field(default_factory=list, description="Missing reasons")


class AnalysisContextPackOverviewCounts(BaseModel):
    """AnalysisContextPack 可见摘要状态计数"""

    available: int = 0
    missing: int = 0
    not_supported: int = 0
    fallback: int = 0
    stale: int = 0
    estimated: int = 0
    partial: int = 0
    fetch_failed: int = 0


class AnalysisContextPackOverviewMetadata(BaseModel):
    """AnalysisContextPack 可见摘要元数据"""

    trigger_source: Optional[str] = Field(None, description="Trigger source")
    news_result_count: Optional[int] = Field(None, description="Number of news results")


class AnalysisContextPackOverviewDataQuality(BaseModel):
    """AnalysisContextPack 可见摘要数据质量评分"""

    overall_score: Optional[int] = Field(None, ge=0, le=100, description="Overall input data quality score")
    level: Optional[Literal["good", "usable", "limited", "poor"]] = Field(
        None,
        description="Input data quality grade",
    )
    block_scores: Dict[str, int] = Field(default_factory=dict, description="Quality scores of the fixed data blocks")
    limitations: List[str] = Field(default_factory=list, description="Low-sensitivity data limitation notes")


class AnalysisContextPackOverview(BaseModel):
    """历史/API 可见的低敏 AnalysisContextPack 摘要"""

    pack_version: str = Field(..., description="AnalysisContextPack version")
    created_at: Optional[str] = Field(None, description="Created at")
    subject: AnalysisContextPackOverviewSubject
    blocks: List[AnalysisContextPackOverviewBlock] = Field(default_factory=list)
    counts: AnalysisContextPackOverviewCounts
    data_quality: Optional[AnalysisContextPackOverviewDataQuality] = Field(
        None,
        description="Low-sensitivity input data quality summary for this analysis",
    )
    warnings: List[str] = Field(default_factory=list, description="Top-level data quality warnings")
    metadata: AnalysisContextPackOverviewMetadata = Field(default_factory=AnalysisContextPackOverviewMetadata)


class ReportDetails(BaseModel):
    """报告详情区"""
    
    news_content: Optional[str] = Field(None, description="News summary")
    raw_result: Optional[Any] = Field(None, description="Raw analysis result (JSON)")
    context_snapshot: Optional[Any] = Field(None, description="Context snapshot at analysis time (JSON)")
    analysis_context_pack_overview: Optional[AnalysisContextPackOverview] = Field(
        None,
        description="Low-sensitivity summary of the analysis input context pack",
    )
    financial_report: Optional[Any] = Field(None, description="Structured financial report summary (from fundamental_context)")
    dividend_metrics: Optional[Any] = Field(None, description="Structured dividend metrics (including TTM)")
    belong_boards: Optional[Any] = Field(None, description="Related boards")
    sector_rankings: Optional[Any] = Field(None, description="Sector rankings (structure {top, bottom})")
    concept_rankings: Optional[Any] = Field(None, description="Concept board rankings (structure {top, bottom})")
    market_structure: Optional[Any] = Field(None, description="Market structure context (theme layer + stock position layer)")

    @model_validator(mode="after")
    def populate_context_derived_details(self) -> "ReportDetails":
        if self.concept_rankings is None and self.context_snapshot is not None:
            try:
                from src.utils.data_processing import extract_board_detail_fields

                extracted = extract_board_detail_fields(self.context_snapshot)
                self.concept_rankings = extracted.get("concept_rankings")
            except Exception:
                self.concept_rankings = None
        if self.market_structure is None:
            try:
                from src.utils.data_processing import extract_market_structure_detail_field

                self.market_structure = extract_market_structure_detail_field(
                    self.context_snapshot,
                    self.raw_result,
                )
            except Exception:
                self.market_structure = None
        return self


class AnalysisReport(BaseModel):
    """完整分析报告"""

    meta: ReportMeta = Field(..., description="Metadata")
    summary: ReportSummary = Field(..., description="Summary section")
    strategy: Optional[ReportStrategy] = Field(None, description="Strategy levels section")
    details: Optional[ReportDetails] = Field(None, description="Details section")

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "meta": {
                "query_id": "abc123",
                "stock_code": "600519",
                "stock_name": "Kweichow Moutai",
                "report_type": "detailed",
                "report_language": "zh",
                "created_at": "2024-01-01T12:00:00"
            },
            "summary": {
                "analysis_summary": "Technicals look positive; suggest holding",
                "operation_advice": "Hold",
                "trend_prediction": "Bullish",
                "sentiment_score": 75,
                "sentiment_label": "Optimistic"
            },
            "strategy": {
                "ideal_buy": "1800.00",
                "secondary_buy": "1750.00",
                "stop_loss": "1700.00",
                "take_profit": "2000.00"
            },
            "details": None
        }
    })


class MarkdownReportResponse(BaseModel):
    """Markdown 格式报告响应"""

    content: str = Field(..., description="Full report content in Markdown")

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "content": "# 📊 Kweichow Moutai (600519) Analysis Report\n\n> Analysis date: **2024-01-01**\n\n..."
        }
    })


class StockBarItem(BaseModel):
    """个股栏条目（去重后的股票维度摘要）"""

    id: int = Field(..., description="Primary key ID of the stock's latest analysis record")
    stock_code: str = Field(..., description="Stock code")
    stock_name: Optional[str] = Field(None, description="Stock name")
    report_type: Optional[str] = Field(None, description="Report type")
    sentiment_score: Optional[int] = Field(
        None,
        description="Latest sentiment score",
    )
    operation_advice: Optional[str] = Field(None, description="Latest operation advice")
    action: Optional[DecisionAction] = Field(None, description="Structured action taxonomy")
    action_label: Optional[str] = Field(None, description="Display label for the suggested action")
    analysis_count: int = Field(..., description="Total number of analyses for this stock")
    last_analysis_time: Optional[str] = Field(None, description="Last analysis time")
    model_used: Optional[str] = Field(
        None,
        description="Model snapshot of the latest analysis; for list display only, does not affect runtime calls or configuration",
    )
    market_phase_summary: Optional[MarketPhaseSummary] = Field(
        None,
        description="Low-sensitivity market phase summary of the latest analysis",
    )
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "id": 1234,
            "stock_code": "600519",
            "stock_name": "Kweichow Moutai",
            "report_type": "detailed",
            "sentiment_score": 75,
            "operation_advice": "Hold",
            "analysis_count": 18,
            "last_analysis_time": "2024-01-01T12:00:00",
            "model_used": "Gemini 2.5 Pro",
        }
    })


class StockBarResponse(BaseModel):
    """个股栏列表响应"""

    total: int = Field(..., description="Number of distinct stocks")
    items: List[StockBarItem] = Field(default_factory=list, description="Stock list")


class WatchlistRequest(BaseModel):
    """自选队列操作请求"""

    stock_code: str = Field(..., description="Stock code", min_length=1)


class WatchlistResponse(BaseModel):
    """自选队列响应"""

    stock_codes: List[str] = Field(default_factory=list, description="Stock codes currently in the watchlist queue")
    message: str = Field(..., description="Operation result description")


class RunDiagnosticComponent(BaseModel):
    """单个运行诊断组件摘要。"""

    key: str = Field(..., description="Component key")
    label: str = Field(..., description="Component display name")
    status: str = Field(..., description="Component status: ok/degraded/failed/unknown/not_configured/skipped")
    message: str = Field(..., description="Human-readable summary")
    details: Optional[Dict[str, Any]] = Field(None, description="Collapsible diagnostic details")


class RunDiagnosticSummaryResponse(BaseModel):
    """历史报告运行诊断摘要。"""

    trace_id: Optional[str] = Field(None, description="Diagnostic trace ID")
    task_id: Optional[str] = Field(None, description="Task ID")
    query_id: Optional[str] = Field(None, description="Analysis query ID")
    stock_code: Optional[str] = Field(None, description="Stock code")
    trigger_source: Optional[str] = Field(None, description="Trigger source")
    status: str = Field(..., description="Overall status: normal/degraded/failed/unknown")
    status_label: str = Field(..., description="Overall status display label")
    reason: str = Field(..., description="Primary diagnostic reason")
    components: Dict[str, RunDiagnosticComponent] = Field(default_factory=dict, description="Diagnostic components of the critical path")
    copy_text: str = Field(..., description="Copyable redacted troubleshooting text")

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "trace_id": "task_abc123",
            "query_id": "task_abc123",
            "stock_code": "600519",
            "status": "degraded",
            "status_label": "Partially degraded",
            "reason": "Realtime quote failed: timeout",
            "components": {},
            "copy_text": "trace_id: task_abc123\nstock_code: 600519\n...",
        }
    })
