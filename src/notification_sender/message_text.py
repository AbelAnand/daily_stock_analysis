# -*- coding: utf-8 -*-
"""
Localized user-visible text shared by notification senders.

Senders only know the global config, not the per-result report language, so
they resolve the language from ``config.report_language`` (falling back to the
process-wide config). Unknown / missing languages render English so that an
English-only recipient never sees Chinese in a subject line or caption.
"""

from typing import Any, Optional

from src.report_language import is_supported_report_language_value, normalize_report_language

_MESSAGE_TEXT = {
    "report_title": {
        "zh": "股票分析报告",
        "en": "Stock Analysis Report",
        "ko": "주식 분석 리포트",
    },
    "report_title_full": {
        "zh": "股票智能分析报告",
        "en": "Stock Intelligence Report",
        "ko": "주식 인텔리전스 리포트",
    },
    "bot_username": {
        "zh": "A股分析机器人",
        "en": "Stock Analysis Bot",
        "ko": "주식 분석 봇",
    },
    "email_sender_name": {
        "zh": "股票分析助手",
        "en": "Stock Analysis Assistant",
        "ko": "주식 분석 도우미",
    },
    "image_report_ready_plain": {
        "zh": "报告已生成，详见下方图片。",
        "en": "The report has been generated; see the image below.",
        "ko": "리포트가 생성되었습니다. 아래 이미지를 확인하세요.",
    },
    "image_report_ready_html": {
        "zh": "报告已生成，详见下方图片（点击可查看大图）：",
        "en": "The report has been generated; see the image below (click to enlarge):",
        "ko": "리포트가 생성되었습니다. 아래 이미지를 확인하세요 (클릭하면 확대):",
    },
    "image_alt": {
        "zh": "股票分析报告",
        "en": "Stock analysis report",
        "ko": "주식 분석 리포트",
    },
    "file_content_prefix": {
        "zh": "{label} 文件内容: {name}",
        "en": "{label} file: {name}",
        "ko": "{label} 파일 내용: {name}",
    },
    "file_label_text": {"zh": "文本", "en": "Text", "ko": "텍스트"},
    "file_label_generic": {"zh": "文件", "en": "File", "ko": "파일"},
    "unknown_error": {"zh": "未知错误", "en": "Unknown error", "ko": "알 수 없는 오류"},
}


def resolve_message_language(config: Optional[Any] = None) -> str:
    """Language for sender-level text: explicit config value, else global config, else en."""
    value = getattr(config, "report_language", None) if config is not None else None
    if not value:
        try:
            from src.config import get_config

            value = getattr(get_config(), "report_language", None)
        except Exception:
            value = None
    if is_supported_report_language_value(value):
        return normalize_report_language(value)
    return "en"


def get_message_text(key: str, language: Optional[str] = None, **fmt: Any) -> str:
    table = _MESSAGE_TEXT.get(key) or {}
    lang = language if language in table else None
    if lang is None:
        lang = normalize_report_language(language, default="en") if language else "en"
        if lang not in table:
            lang = "en"
    text = table.get(lang) or table.get("en") or ""
    return text.format(**fmt) if fmt else text
