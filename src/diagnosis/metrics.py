"""
可解释指标计算（纯函数，零 LLM 依赖，不依赖项目内其他模块）

从 JSONL 事件列表计算四项指标：
  - compute_phase_timings(events) -> list[PhaseTiming]
  - compute_rule_and_resolution_summary(events, top_n) -> RuleAndResolutionSummary
  - estimate_user_time_saved(events, config?) -> TimeSavedEstimate
  - compute_explainability_metrics(events, config?) -> ExplainabilityReport
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── 数据结构 ────────────────────────────────────────────────────

@dataclass
class PhaseTiming:
    from_phase: str
    to_phase: str
    duration_ms: float


@dataclass
class RoundResolution:
    round: int
    static_resolve_pct: float
    static_net_fix_pct: float
    actionable_resolve_pct: float
    stubborn_count: int


@dataclass
class RuleAndResolutionSummary:
    first_round_errors: int
    first_round_warnings: int
    last_round_errors: int
    last_round_warnings: int
    error_cleared_round: int | None       # error 首次归零的修订轮次（None = 从未归零）
    total_static_resolution_pct: float     # 全流程静态消解率（首轮→末轮问题数变化）
    top_rules: list[tuple[str, int]]       # (rule_id, 累计命中次数) TOP-N
    resolution_by_round: list[RoundResolution] = field(default_factory=list)


@dataclass
class TimeSavedEstimate:
    draft_words: int
    manual_hours_neutral: float
    ai_hours: float
    time_saved_hours: float
    time_saved_ratio: float
    work_sessions_saved: float
    conservative_hours: float
    optimistic_hours: float


@dataclass
class ExplainabilityReport:
    session_id: str
    total_wall_time_s: float
    phase_timings: list[PhaseTiming]
    rule_and_resolution: RuleAndResolutionSummary
    time_saved: TimeSavedEstimate


# ── 默认参数 ────────────────────────────────────────────────────

_DEFAULT_DRAFT_SPEED_CPH = 500        # 人工初稿速度（字/小时）
_DEFAULT_REVISE_SPEED_CPH = 800       # 人工修订速度
_DEFAULT_FORMAT_SPEED_CPH = 2000      # 人工格式调整速度
_DEFAULT_HOURS_PER_SESSION = 3        # 单次有效工作时长
_DEFAULT_TOP_RULES_N = 5


# ── 1. 生成耗时 ─────────────────────────────────────────────────

def compute_phase_timings(events: list[dict[str, Any]]) -> list[PhaseTiming]:
    """从 phase_span 事件提取各阶段耗时列表。"""
    return [
        PhaseTiming(
            from_phase=e.get("from_phase", ""),
            to_phase=e.get("to_phase", ""),
            duration_ms=float(e.get("duration_ms") or 0),
        )
        for e in events
        if e.get("type") == "phase_span"
    ]


# ── 2. 规则命中 + 消解率（合并）─────────────────────────────────

def _error_warning_counts(breakdown: dict | None) -> tuple[int, int]:
    """从 static_rule_breakdown 中提取 error / warning 数量。"""
    if not breakdown or not isinstance(breakdown, dict):
        return 0, 0
    by_sev = breakdown.get("by_severity") or {}
    return by_sev.get("error", 0), by_sev.get("warning", 0)


def compute_rule_and_resolution_summary(
    events: list[dict[str, Any]],
    top_n: int = _DEFAULT_TOP_RULES_N,
) -> RuleAndResolutionSummary:
    """
    从 evaluation 事件计算规则命中与消解率汇总。

    策略：
      - 高频规则：跨轮累加各轮 by_rule_id 映射，排序取 top_n
      - 消解率：从已有 static_delta / actionable_coarse_delta 直接读 resolve_rate_pct
      - error 清零：找 by_severity.error 首次为 0 的轮次
    """
    evals = [e for e in events if e.get("type") == "evaluation"]

    if not evals:
        return RuleAndResolutionSummary(
            first_round_errors=0,
            first_round_warnings=0,
            last_round_errors=0,
            last_round_warnings=0,
            error_cleared_round=None,
            total_static_resolution_pct=0.0,
            top_rules=[],
            resolution_by_round=[],
        )

    # ── 首 / 末轮 error & warning 数 ──
    first_breakdown = evals[0].get("static_rule_breakdown")
    last_breakdown = evals[-1].get("static_rule_breakdown")
    first_err, first_warn = _error_warning_counts(first_breakdown)
    last_err, last_warn = _error_warning_counts(last_breakdown)

    # ── 全流程消解率（首轮→末轮问题数变化）──
    first_total = int(evals[0].get("n_static_rule_issues") or 0)
    last_total = int(evals[-1].get("n_static_rule_issues") or 0)
    total_resolution_pct = (
        round((first_total - last_total) / first_total * 100, 1)
        if first_total > 0
        else 0.0
    )

    # ── error 清零轮次 ──
    error_cleared = None
    for ev in evals:
        breakdown = ev.get("static_rule_breakdown")
        err, _ = _error_warning_counts(breakdown)
        if err == 0:
            error_cleared = int(ev.get("revision_round") or 0)
            break

    # ── 高频规则 TOP-N：跨轮累加 by_rule_id ──
    rule_hits: dict[str, int] = {}
    for ev in evals:
        breakdown = ev.get("static_rule_breakdown") or {}
        by_rule = breakdown.get("by_rule_id") or {}
        for rid, cnt in by_rule.items():
            rule_hits[rid] = rule_hits.get(rid, 0) + int(cnt)
    top_rules = sorted(rule_hits.items(), key=lambda x: x[1], reverse=True)[:top_n]

    # ── 各轮消解率 ──
    resolution_by_round: list[RoundResolution] = []
    for ev in evals:
        rnd = int(ev.get("revision_round") or 0)
        static_delta = ev.get("static_delta") or {}
        actionable_delta = ev.get("actionable_coarse_delta") or {}
        resolution_by_round.append(RoundResolution(
            round=rnd,
            static_resolve_pct=float(static_delta.get("resolve_rate_pct") or 0),
            static_net_fix_pct=float(static_delta.get("net_fix_rate_pct") or 0),
            actionable_resolve_pct=float(actionable_delta.get("resolve_rate_pct") or 0),
            stubborn_count=int(ev.get("stubborn_count") or 0),
        ))

    return RuleAndResolutionSummary(
        first_round_errors=first_err,
        first_round_warnings=first_warn,
        last_round_errors=last_err,
        last_round_warnings=last_warn,
        error_cleared_round=error_cleared,
        total_static_resolution_pct=total_resolution_pct,
        top_rules=top_rules,
        resolution_by_round=resolution_by_round,
    )


# ── 3. 用户时间节省 ──────────────────────────────────────────────

def estimate_user_time_saved(
    events: list[dict[str, Any]],
    draft_speed_cph: int | None = None,
    revise_speed_cph: int | None = None,
    format_speed_cph: int | None = None,
    hours_per_session: float | None = None,
) -> TimeSavedEstimate:
    """
    根据终稿字数和总墙钟估算用户节省时间。

    draft_words: 从 draft_complete.approx_chars 取
    revise_words: draft_words × revision_rounds × 0.3（每轮修订改动约 30% 内容）
    None 参数回落默认值。
    """
    if draft_speed_cph is None:
        draft_speed_cph = _DEFAULT_DRAFT_SPEED_CPH
    if revise_speed_cph is None:
        revise_speed_cph = _DEFAULT_REVISE_SPEED_CPH
    if format_speed_cph is None:
        format_speed_cph = _DEFAULT_FORMAT_SPEED_CPH
    if hours_per_session is None:
        hours_per_session = _DEFAULT_HOURS_PER_SESSION

    # 取 draft_complete 的 approx_chars
    draft_events = [e for e in events if e.get("type") == "draft_complete"]
    draft_words = int(draft_events[-1].get("approx_chars") or 0) if draft_events else 0

    # 取修订轮数
    evals = [e for e in events if e.get("type") == "evaluation"]
    # revision_round 是最后一轮的值，但首轮是 0，所以实际轮数 = max(revision_round)
    revision_rounds = max((int(e.get("revision_round") or 0) for e in evals), default=0)

    # 取 AI 总耗时（秒）
    run_ends = [e for e in events if e.get("type") == "run_end"]
    total_wall_time_ms = float(run_ends[-1].get("total_wall_time_ms") or 0) if run_ends else 0.0
    ai_hours = total_wall_time_ms / 3_600_000.0

    # 人工耗时估算
    revise_words = draft_words * revision_rounds * 0.3
    manual_draft_hours = draft_words / draft_speed_cph
    manual_revise_hours = revise_words / revise_speed_cph
    manual_format_hours = draft_words / format_speed_cph
    manual_hours_neutral = manual_draft_hours + manual_revise_hours + manual_format_hours

    # 三档估计（只调初稿速度）
    manual_optimistic = draft_words / (draft_speed_cph * 0.8) + manual_revise_hours + manual_format_hours
    manual_conservative = draft_words / (draft_speed_cph * 1.4) + manual_revise_hours + manual_format_hours

    time_saved_hours = max(0, manual_hours_neutral - ai_hours)
    time_saved_ratio = time_saved_hours / manual_hours_neutral if manual_hours_neutral > 0 else 0.0
    work_sessions = time_saved_hours / hours_per_session if hours_per_session > 0 else 0.0

    return TimeSavedEstimate(
        draft_words=draft_words,
        manual_hours_neutral=round(manual_hours_neutral, 1),
        ai_hours=round(ai_hours, 2),
        time_saved_hours=round(time_saved_hours, 1),
        time_saved_ratio=round(time_saved_ratio, 3),
        work_sessions_saved=round(work_sessions, 1),
        conservative_hours=round(max(0, manual_conservative - ai_hours), 1),
        optimistic_hours=round(max(0, manual_optimistic - ai_hours), 1),
    )


# ── 4. 顶层聚合 ─────────────────────────────────────────────────

def compute_explainability_metrics(
    events: list[dict[str, Any]],
    draft_speed_cph: int | None = None,
    revise_speed_cph: int | None = None,
    format_speed_cph: int | None = None,
    hours_per_session: float | None = None,
    top_rules_n: int | None = None,
) -> ExplainabilityReport:
    """从 JSONL 事件列表计算全部四项可解释指标。None 参数回落默认值。"""
    if draft_speed_cph is None:
        draft_speed_cph = _DEFAULT_DRAFT_SPEED_CPH
    if revise_speed_cph is None:
        revise_speed_cph = _DEFAULT_REVISE_SPEED_CPH
    if format_speed_cph is None:
        format_speed_cph = _DEFAULT_FORMAT_SPEED_CPH
    if hours_per_session is None:
        hours_per_session = _DEFAULT_HOURS_PER_SESSION
    if top_rules_n is None:
        top_rules_n = _DEFAULT_TOP_RULES_N

    session_id = events[0].get("session_id", "") if events else ""

    # 总墙钟
    run_ends = [e for e in events if e.get("type") == "run_end"]
    total_wall_time_ms = float(run_ends[-1].get("total_wall_time_ms") or 0) if run_ends else 0.0

    return ExplainabilityReport(
        session_id=str(session_id or ""),
        total_wall_time_s=round(total_wall_time_ms / 1000.0, 1),
        phase_timings=compute_phase_timings(events),
        rule_and_resolution=compute_rule_and_resolution_summary(events, top_n=top_rules_n),
        time_saved=estimate_user_time_saved(
            events,
            draft_speed_cph=draft_speed_cph,
            revise_speed_cph=revise_speed_cph,
            format_speed_cph=format_speed_cph,
            hours_per_session=hours_per_session,
        ),
    )