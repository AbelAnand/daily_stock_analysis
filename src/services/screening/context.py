# -*- coding: utf-8 -*-
# Derived from AlphaSift revision 9f522747caafd3c0b1ddb7e14d5cf44c8580b6cf.
# Licensed under Apache-2.0 and modified for daily_stock_analysis.
"""Context assembly for LLM ranking."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import pandas as pd

from src.services.screening.normalize import normalize_code as _normalize_code

_CONTEXT_TRIM_MARKER = "[context_trimmed]"
_CANDIDATE_CONTEXT_COLUMNS = {
    "news": "news",
    "announcement": "announcement",
    "announcements": "announcement",
    "fund_flow": "fund_flow",
    "fundflow": "fund_flow",
    "quote": "quote_valuation",
    "summary": "summary",
    "context": "context",
    "context_summary": "compressed_summary",
    "text": "text",
    "risk": "risk",
    "catalyst": "catalyst",
    "source_count": "source_count",
    "source_confidence": "source_confidence",
    "source_weight_score": "source_weight_score",
    "event_tags": "event_tags",
    "announcement_categories": "announcement_categories",
    "negative_event_flags": "negative_risks",
}


@dataclass(frozen=True)
class _ContextSection:
    text: str
    kind: str
    priority: int
    min_chars: int
    weight: int
    line_aware: bool = False


def build_llm_context(
    *,
    base_context: str = "",
    context_files: list[str | Path] | None = None,
    candidate_context_files: list[str | Path] | None = None,
    candidate_context_rows: list[dict[str, object]] | None = None,
    snapshot_df: pd.DataFrame | None = None,
    candidate_df: pd.DataFrame | None = None,
    event_profile: dict[str, object] | None = None,
    max_chars: int = 4000,
    degradation: list[str] | None = None,
) -> str:
    """Build bounded context text for the LLM soft ranker."""
    sections: list[_ContextSection] = []
    if base_context.strip():
        sections.append(_section(
            "[Manual context]\n" + base_context.strip(),
            kind="market_context",
            priority=5,
            min_chars=80,
            weight=1,
            line_aware=True,
        ))

    file_context = _read_context_files(context_files or [])
    if file_context:
        sections.append(_section(
            "[Context files]\n" + file_context,
            kind="market_files",
            priority=5,
            min_chars=80,
            weight=1,
            line_aware=True,
        ))

    event_profile_context = summarize_event_profile(event_profile)
    if event_profile_context:
        sections.append(_section(
            event_profile_context,
            kind="event_profile",
            priority=2,
            min_chars=160,
            weight=2,
            line_aware=True,
        ))

    candidate_identity = summarize_candidate_identity(candidate_df)
    if candidate_identity:
        sections.append(_section(
            candidate_identity,
            kind="candidate_identity",
            priority=0,
            min_chars=360,
            weight=4,
            line_aware=True,
        ))

    candidate_external_context = _read_candidate_context_files(
        candidate_context_files or [],
        candidate_df,
    )
    if candidate_external_context:
        sections.append(_section(
            "[Candidate external leads]\n" + candidate_external_context,
            kind="candidate_external_context",
            priority=1,
            min_chars=320,
            weight=4,
            line_aware=True,
        ))

    collected_candidate_context = _format_candidate_context_rows(
        candidate_context_rows or [],
        candidate_df,
    )
    if collected_candidate_context:
        sections.append(_section(
            "[Candidate collected leads]\n" + collected_candidate_context,
            kind="candidate_collected_context",
            priority=1,
            min_chars=360,
            weight=4,
            line_aware=True,
        ))

    snapshot_context = summarize_snapshot_context(snapshot_df, title="Full-market snapshot")
    if snapshot_context:
        sections.append(_section(
            snapshot_context,
            kind="market_snapshot",
            priority=4,
            min_chars=160,
            weight=2,
            line_aware=True,
        ))

    candidate_context = summarize_snapshot_context(candidate_df, title="Candidate pool snapshot")
    if candidate_context:
        sections.append(_section(
            candidate_context,
            kind="candidate_snapshot",
            priority=2,
            min_chars=180,
            weight=2,
            line_aware=True,
        ))

    candidate_profile = summarize_candidate_profile(candidate_df)
    if candidate_profile:
        sections.append(_section(
            candidate_profile,
            kind="candidate_profile",
            priority=2,
            min_chars=240,
            weight=3,
            line_aware=True,
        ))

    return _join_bounded_context_sections(sections, max_chars=max_chars, degradation=degradation)


def summarize_snapshot_context(df: pd.DataFrame | None, *, title: str) -> str:
    """Summarize breadth, activity and extremes from a snapshot DataFrame."""
    if df is None or df.empty:
        return ""

    lines = [f"[{title}]", f"Sample size: {len(df)}"]
    if "change_pct" in df.columns:
        change = pd.to_numeric(df["change_pct"], errors="coerce").dropna()
        if not change.empty:
            positive_ratio = (change > 0).mean() * 100
            lines.append(
                "Breadth: "
                f"advancers {positive_ratio:.1f}%, "
                f"median change {change.median():.2f}%, "
                f"mean change {change.mean():.2f}%"
            )
            lines.append("Extremes: " + _format_extremes(df, change, ascending=False))
    if "amount" in df.columns:
        amount = pd.to_numeric(df["amount"], errors="coerce").dropna()
        if not amount.empty:
            lines.append(f"Median turnover: {amount.median():.0f}")
    if "volume_ratio" in df.columns:
        volume_ratio = pd.to_numeric(df["volume_ratio"], errors="coerce").dropna()
        if not volume_ratio.empty:
            hot_ratio = (volume_ratio >= 2).mean() * 100
            lines.append(f"Share with volume ratio >= 2: {hot_ratio:.1f}%")
    return "\n".join(lines)


def summarize_candidate_profile(df: pd.DataFrame | None) -> str:
    """Summarize factor conflicts and leadership inside the candidate pool."""
    if df is None or df.empty:
        return ""

    lines = ["[Candidate pool structure]"]
    factor_cols = {
        "value": "factor_value_score",
        "liquidity": "factor_liquidity_score",
        "momentum": "factor_momentum_score",
        "reversal": "factor_reversal_score",
        "activity": "factor_activity_score",
        "stability": "factor_stability_score",
        "size": "factor_size_score",
        "theme_heat": "factor_theme_heat_score",
    }
    available = {label: col for label, col in factor_cols.items() if col in df.columns}
    if available:
        averages = []
        for label, col in available.items():
            series = pd.to_numeric(df[col], errors="coerce").dropna()
            if not series.empty:
                averages.append(f"{label}={series.mean():.1f}")
        if averages:
            lines.append("Factor averages: " + ", ".join(averages))

        leaders = []
        columns = [col for col in ("code", "name") if col in df.columns]
        for label, col in available.items():
            series = pd.to_numeric(df[col], errors="coerce")
            if series.dropna().empty or not columns:
                continue
            idx = series.idxmax()
            row = df.loc[idx]
            leaders.append(f"{label}:{row.get('code', '')}{row.get('name', '')}({series.loc[idx]:.1f})")
        if leaders:
            lines.append("Factor leaders: " + ", ".join(leaders[:8]))

    if "screen_score" in df.columns:
        screen_score = pd.to_numeric(df["screen_score"], errors="coerce").dropna()
        if not screen_score.empty:
            lines.append(
                f"Screen score distribution: max {screen_score.max():.1f}, "
                f"median {screen_score.median():.1f}, min {screen_score.min():.1f}"
            )

    industry_summary = _summarize_label_distribution(df, "industry")
    if industry_summary:
        lines.append("Industry distribution: " + industry_summary)
    concept_summary = _summarize_label_distribution(df, "concepts")
    if concept_summary:
        lines.append("Concept leads: " + concept_summary)
    heat_summary = _summarize_board_heat(df)
    if heat_summary:
        lines.append("Board/theme heat: " + heat_summary)

    return "\n".join(lines) if len(lines) > 1 else ""


def summarize_candidate_identity(df: pd.DataFrame | None, *, limit: int = 30) -> str:
    """Summarize top candidate identities and key ranking fields."""
    if df is None or df.empty or "code" not in df.columns:
        return ""

    lines = ["[Candidate identities]"]
    for _, row in df.head(max(int(limit), 1)).iterrows():
        code = _normalize_code(row.get("code", row.get("代码", "")))
        if not code:
            continue
        name = _safe_context_value(
            row.get("name") or row.get("名称") or row.get("股票名称"),
            max_len=40,
        )
        fields = []
        score = _safe_context_value(row.get("screen_score"), max_len=24)
        if score:
            fields.append(f"screen_score={score}")
        industry = _safe_context_value(row.get("industry") or row.get("行业"), max_len=40)
        if industry:
            fields.append(f"industry={industry}")
        concepts = _safe_context_value(row.get("concepts") or row.get("概念"), max_len=80)
        if concepts:
            fields.append(f"concepts={concepts}")
        heat = _safe_context_value(row.get("board_heat_score"), max_len=24)
        if heat:
            fields.append(f"board_heat_score={heat}")
        suffix = ": " + ", ".join(fields) if fields else ""
        lines.append(f"- {code} {name}{suffix}")
    if len(df) > limit:
        lines.append(f"...[candidate_identity_omitted:{len(df) - limit}]")
    return "\n".join(lines) if len(lines) > 1 else ""


def summarize_event_profile(event_profile: dict[str, object] | None) -> str:
    """Summarize strategy-level event preferences for the LLM."""
    if not event_profile:
        return ""
    lines = ["[Strategy event preferences]"]
    field_labels = {
        "preferred_event_tags": "Preferred event tags",
        "avoided_event_tags": "Avoided event tags",
        "preferred_announcement_categories": "Preferred announcement categories",
        "avoided_announcement_categories": "Avoided announcement categories",
        "notes": "Event notes",
    }
    for field, label in field_labels.items():
        value = _format_profile_value(event_profile.get(field))
        if value:
            lines.append(f"{label}: {value}")
    source_weights = event_profile.get("source_weights")
    if isinstance(source_weights, dict) and source_weights:
        items = []
        for source, weight in source_weights.items():
            text = _safe_context_value(source, max_len=40)
            if not text:
                continue
            try:
                items.append(f"{text}={float(weight):.2f}")
            except (TypeError, ValueError):
                continue
        if items:
            lines.append("Source weights: " + ", ".join(items))
    return "\n".join(lines) if len(lines) > 1 else ""


def _read_context_files(paths: list[str | Path]) -> str:
    chunks: list[str] = []
    for path_like in paths:
        path = Path(path_like)
        if not path.is_file():
            raise FileNotFoundError(f"Context file not found: {path}")
        text = path.read_text(encoding="utf-8").strip()
        if text:
            chunks.append(f"# {path.name}\n{text}")
    return "\n\n".join(chunks)


def _read_candidate_context_files(
    paths: list[str | Path],
    candidate_df: pd.DataFrame | None,
) -> str:
    if not paths or candidate_df is None or candidate_df.empty or "code" not in candidate_df.columns:
        return ""

    candidate_names, candidate_order = _candidate_maps(candidate_df)
    candidate_codes = set(candidate_names)
    chunks: list[tuple[int, int, str]] = []
    row_position = 0
    for path_like in paths:
        path = Path(path_like)
        if not path.is_file():
            raise FileNotFoundError(f"Candidate context file not found: {path}")
        rows = _load_candidate_context_rows(path)
        for row in rows:
            code = _normalize_code(row.get("code", row.get("代码", "")))
            item = _format_candidate_context_row(row, candidate_codes, candidate_names)
            if item:
                chunks.append((candidate_order.get(code, len(candidate_order)), row_position, item))
            row_position += 1
    return "\n".join(item for _, _, item in sorted(chunks))


def _format_candidate_context_rows(
    rows: list[dict[str, object]],
    candidate_df: pd.DataFrame | None,
) -> str:
    if not rows or candidate_df is None or candidate_df.empty or "code" not in candidate_df.columns:
        return ""
    candidate_names, candidate_order = _candidate_maps(candidate_df)
    candidate_codes = set(candidate_names)
    chunks = []
    for idx, row in enumerate(rows):
        code = _normalize_code(row.get("code", row.get("代码", "")))
        item = _format_candidate_context_row(row, candidate_codes, candidate_names)
        if item:
            chunks.append((candidate_order.get(code, len(candidate_order)), idx, item))
    return "\n".join(item for _, _, item in sorted(chunks))


def _candidate_maps(candidate_df: pd.DataFrame) -> tuple[dict[str, str], dict[str, int]]:
    candidate_names: dict[str, str] = {}
    candidate_order: dict[str, int] = {}
    for idx, (_, row) in enumerate(candidate_df.iterrows()):
        code = _normalize_code(row.get("code", row.get("代码", "")))
        if not code:
            continue
        candidate_names[code] = str(row.get("name", row.get("名称", "")) or "")
        candidate_order.setdefault(code, idx)
    return candidate_names, candidate_order


def _format_candidate_context_row(
    row: dict[str, object],
    candidate_codes: set[str],
    candidate_names: dict[str, str],
) -> str:
    code = _normalize_code(row.get("code", row.get("代码", "")))
    if code not in candidate_codes:
        return ""
    fields = []
    for column, label in _CANDIDATE_CONTEXT_COLUMNS.items():
        value = _safe_context_value(row.get(column))
        if value:
            fields.append(f"{label}:{value}")
    if not fields:
        return ""
    name = _safe_context_value(row.get("name") or row.get("名称")) or candidate_names.get(code, "")
    return f"- {code} {name}: " + "; ".join(fields)


def _load_candidate_context_rows(path: Path) -> list[dict[str, object]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path, dtype=str).fillna("").to_dict(orient="records")
    if suffix == ".jsonl":
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                item = json.loads(line)
                if isinstance(item, dict):
                    rows.append(item)
        return rows
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            items = data.get("items") or data.get("data")
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
            rows = []
            for code, value in data.items():
                if isinstance(value, dict):
                    rows.append({"code": code, **value})
                elif isinstance(value, str):
                    rows.append({"code": code, "text": value})
            return rows
    raise ValueError(f"Unsupported candidate context file format: {path}")


def _safe_context_value(value: object, *, max_len: int = 280) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        text = ",".join(str(item).strip() for item in value if str(item).strip())
    else:
        text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "<na>"}:
        return ""
    return text[:max_len]


def _format_profile_value(value: object) -> str:
    if isinstance(value, list):
        return "，".join(
            item
            for item in (_safe_context_value(raw, max_len=80) for raw in value)
            if item
        )
    return _safe_context_value(value, max_len=280)


def _section(
    text: str,
    *,
    kind: str,
    priority: int,
    min_chars: int,
    weight: int,
    line_aware: bool = False,
) -> _ContextSection:
    return _ContextSection(
        text=text.strip(),
        kind=kind,
        priority=priority,
        min_chars=max(int(min_chars), 0),
        weight=max(int(weight), 1),
        line_aware=line_aware,
    )


def _join_bounded_context_sections(
    sections: list[_ContextSection],
    *,
    max_chars: int,
    degradation: list[str] | None,
) -> str:
    sections = [section for section in sections if section.text.strip()]
    combined = "\n\n".join(section.text for section in sections).strip()
    if not combined:
        return ""
    if len(combined) <= max_chars:
        return combined

    marker_line = f"[Context degraded] {_CONTEXT_TRIM_MARKER} low-priority context trimmed."
    budget = max(int(max_chars) - len(marker_line) - 2, 0)
    if budget <= 0:
        return marker_line[:max_chars]

    allocations = _allocate_section_budgets(sections, budget)
    chunks: list[str] = []
    trimmed_kinds: list[str] = []
    for section, limit in zip(sections, allocations, strict=False):
        trimmed = _trim_section(section, limit)
        if trimmed:
            chunks.append(trimmed)
        if len(trimmed) < len(section.text):
            trimmed_kinds.append(section.kind)

    result = "\n\n".join(chunks).strip()
    if trimmed_kinds:
        trimmed_labels = ",".join(dict.fromkeys(trimmed_kinds))
        marker_line = (
            f"[Context degraded] {_CONTEXT_TRIM_MARKER} "
            f"trimmed={trimmed_labels}"
        )
        result = _append_marker_within_limit(result, marker_line, max_chars=max_chars)
        if degradation is not None:
            degradation.append(f"LLM context truncated: trimmed={trimmed_labels}")
    return result[:max_chars]


def _allocate_section_budgets(sections: list[_ContextSection], budget: int) -> list[int]:
    separator_budget = max(len(sections) - 1, 0) * 2
    body_budget = max(budget - separator_budget, 0)
    minimums = [min(len(section.text), section.min_chars) for section in sections]
    minimum_total = sum(minimums)
    if minimum_total > body_budget:
        return _priority_floor_allocations(sections, body_budget)

    allocations = list(minimums)
    remaining = body_budget - minimum_total
    while remaining > 0:
        expandable = [
            idx
            for idx, section in enumerate(sections)
            if allocations[idx] < len(section.text)
        ]
        if not expandable:
            break
        total_weight = sum(sections[idx].weight for idx in expandable)
        progressed = False
        for idx in expandable:
            section = sections[idx]
            extra = len(section.text) - allocations[idx]
            share = max(1, int(remaining * section.weight / max(total_weight, 1)))
            take = min(extra, share, remaining)
            if take <= 0:
                continue
            allocations[idx] += take
            remaining -= take
            progressed = True
            if remaining <= 0:
                break
        if not progressed:
            break
    return allocations


def _priority_floor_allocations(sections: list[_ContextSection], budget: int) -> list[int]:
    allocations = [0] * len(sections)
    remaining = max(int(budget), 0)
    for idx in sorted(range(len(sections)), key=lambda item: sections[item].priority):
        if remaining <= 0:
            break
        section = sections[idx]
        floor = min(len(section.text), max(section.min_chars, 80))
        take = min(floor, remaining)
        allocations[idx] = take
        remaining -= take
    return allocations


def _trim_section(section: _ContextSection, limit: int) -> str:
    if limit <= 0:
        return ""
    text = section.text
    if len(text) <= limit:
        return text

    marker = f"\n...{_CONTEXT_TRIM_MARKER}:{section.kind}"
    if limit <= len(marker) + 8:
        return text[:limit].rstrip()
    content_limit = limit - len(marker)
    if not section.line_aware:
        return text[:content_limit].rstrip() + marker

    kept: list[str] = []
    for line in text.splitlines():
        candidate = "\n".join([*kept, line]).rstrip() + marker
        if len(candidate) > limit:
            prefix = "\n".join(kept).rstrip()
            separator = "\n" if prefix else ""
            remaining = limit - len(prefix) - len(separator) - len(marker)
            if remaining > 8:
                kept.append(line[:remaining].rstrip())
            break
        kept.append(line)
    if not kept:
        return text[:content_limit].rstrip() + marker
    return "\n".join(kept).rstrip() + marker


def _append_marker_within_limit(text: str, marker: str, *, max_chars: int) -> str:
    if not text:
        return marker[:max_chars]
    candidate = f"{text}\n{marker}"
    if len(candidate) <= max_chars:
        return candidate
    keep = max(max_chars - len(marker) - 1, 0)
    if keep <= 0:
        return marker[:max_chars]
    return text[:keep].rstrip() + "\n" + marker


def _format_extremes(df: pd.DataFrame, change: pd.Series, *, ascending: bool) -> str:
    columns = [col for col in ("code", "name", "change_pct") if col in df.columns]
    if not columns:
        return ""
    top = df.loc[change.sort_values(ascending=ascending).head(3).index, columns]
    items = []
    for _, row in top.iterrows():
        code = str(row.get("code", ""))
        name = str(row.get("name", ""))
        pct = row.get("change_pct", 0)
        items.append(f"{code}{name}({float(pct):.2f}%)")
    return ", ".join(items)


def _summarize_label_distribution(df: pd.DataFrame, column: str) -> str:
    if column not in df.columns:
        return ""
    labels: list[str] = []
    for raw in df[column].dropna().astype(str):
        for item in raw.replace("，", ",").replace("、", ",").split(","):
            label = item.strip()
            if label and label.lower() not in {"nan", "none", "<na>"}:
                labels.append(label)
    if not labels:
        return ""
    counts = pd.Series(labels).value_counts().head(6)
    return "，".join(f"{label}{count}" for label, count in counts.items())


def _summarize_board_heat(df: pd.DataFrame, *, limit: int = 5) -> str:
    if "board_heat_score" not in df.columns:
        return ""
    values = pd.to_numeric(df["board_heat_score"], errors="coerce")
    if values.dropna().empty:
        return ""
    items = []
    for idx in values.sort_values(ascending=False).dropna().head(limit).index:
        row = df.loc[idx]
        code = str(row.get("code", "") or "")
        name = str(row.get("name", "") or "")
        label = str(row.get("board_heat_summary", "") or row.get("industry", "") or "")
        trend = _safe_context_value(row.get("board_heat_trend_score"), max_len=20)
        persistence = _safe_context_value(row.get("board_heat_persistence_score"), max_len=20)
        cooling = _safe_context_value(row.get("board_heat_cooling_score"), max_len=20)
        state = _safe_context_value(row.get("board_heat_state"), max_len=20)
        trend_text = f",trend={trend}" if trend else ""
        persistence_text = f",persist={persistence}" if persistence else ""
        cooling_text = f",cooling={cooling}" if cooling else ""
        state_text = f",state={state}" if state else ""
        items.append(
            f"{code}{name}:{float(values.loc[idx]):.1f}"
            f"{trend_text}{persistence_text}{cooling_text}{state_text}({label[:60]})"
        )
    return "，".join(items)
