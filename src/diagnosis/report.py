"""
运行诊断 — 将摘要打印为终端表格（Rich），无需 LLM
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .analyzer import load_events, summarize
from .metrics import (
    ExplainabilityReport,
    PhaseTiming,
    compute_explainability_metrics,
)

console = Console()


def print_run_summary(path: str) -> None:
    """读取 JSONL 并打印人类可读摘要。"""
    events = load_events(path)
    s = summarize(events)

    console.print(f"\n[bold]运行诊断[/bold]  session={s.get('session_id')!r}  事件数={s['n_events']}")

    if s["phase_spans"]:
        table = Table(title="阶段耗时（phase_span）")
        table.add_column("上一阶段", style="cyan")
        table.add_column("下一阶段", style="green")
        table.add_column("耗时 ms", justify="right")
        for row in s["phase_spans"]:
            table.add_row(
                str(row.get("from_phase", "")),
                str(row.get("to_phase", "")),
                f"{row.get('duration_ms', 0):.0f}",
            )
        console.print(table)
        console.print(f"[dim]阶段累计约 {s['total_phase_ms']:.0f} ms（monotonic，含轮询内子阶段）[/dim]\n")

    if s.get("last_evaluation"):
        ev = s["last_evaluation"]
        console.print(
            f"[bold]末次评估[/bold]  round={ev.get('revision_round')}  "
            f"总分={ev.get('score_total')} / 10  建议条数={ev.get('n_actionable_items')}  "
            f"阈值={ev.get('threshold')}"
        )
        dims = ev.get("dimensions") or {}
        if dims:
            console.print(
                f"  结构={dims.get('structure')}  逻辑={dims.get('logic')}  "
                f"语言={dims.get('language')}  匹配={dims.get('alignment')}"
            )

    re = s.get("run_end") or {}
    if re:
        console.print(
            f"\n[bold]结束[/bold]  status={re.get('status')!r}  "
            f"path={re.get('paper_path', '—')}"
        )
        if re.get("error"):
            console.print(f"[red]{re['error'][:500]}[/red]")

    console.print(f"\n[dim]原始日志: {path}[/dim]\n")


# ── 可解释指标报告 ────────────────────────────────────────────────

def _fmt_seconds(total_s: float) -> str:
    """秒数→人类可读字符串。"""
    if total_s < 60:
        return f"{total_s:.1f}s"
    minutes = int(total_s // 60)
    seconds = total_s % 60
    return f"{minutes}分{seconds:.0f}秒"


def _fmt_hours(hours: float) -> str:
    """小时数→人类可读字符串。"""
    if hours < 1:
        return f"{hours * 60:.0f}分钟"
    return f"{hours:.1f}小时"


def print_explainability_summary(
    path: str,
    draft_speed_cph: int | None = None,
    revise_speed_cph: int | None = None,
    format_speed_cph: int | None = None,
    hours_per_session: float | None = None,
    top_rules_n: int | None = None,
) -> None:
    """
    读取 JSONL，计算全部可解释指标并以 Rich 面板展示。

    参数可覆盖默认速度/展示参数；未提供的从 explainability 配置读取，再无则使用默认值。
    """
    events = load_events(path)
    if not events:
        console.print("[yellow]无可解释指标数据（JSONL 为空）[/yellow]")
        return

    # 从配置读参数（显式传入的优先）
    try:
        from ..config import get as _get
        exp_cfg = _get("explainability") or {}
    except Exception:
        exp_cfg = {}
    uts_cfg = exp_cfg.get("user_time_saved") or {}
    if draft_speed_cph is None:
        draft_speed_cph = int(uts_cfg.get("manual_draft_speed_cph") or 0) or None
    if revise_speed_cph is None:
        revise_speed_cph = int(uts_cfg.get("manual_revise_speed_cph") or 0) or None
    if format_speed_cph is None:
        format_speed_cph = int(uts_cfg.get("manual_format_speed_cph") or 0) or None
    if hours_per_session is None:
        hours_per_session = float(uts_cfg.get("hours_per_session") or 0) or None
    if top_rules_n is None:
        top_rules_n = int(exp_cfg.get("top_rules_n") or 0) or None

    report = compute_explainability_metrics(
        events,
        draft_speed_cph=draft_speed_cph,
        revise_speed_cph=revise_speed_cph,
        format_speed_cph=format_speed_cph,
        hours_per_session=hours_per_session,
        top_rules_n=top_rules_n,
    )

    # ── 汇总面板 ──
    evals = [e for e in events if e.get("type") == "evaluation"]
    final_score = ""
    final_version = ""
    if evals:
        final_score = f"总分={evals[-1].get('score_total', '?')}/10"
    revisions = [e for e in events if e.get("type") == "revision_complete"]
    if revisions:
        final_version = f"终稿 v{revisions[-1].get('new_version', '?')}"
    revision_count = len(revisions)

    header_lines = [
        f"session={report.session_id}  {final_score}  修订 {revision_count} 轮  {final_version}"
    ]
    console.print(Panel(
        "\n".join(header_lines),
        title="📊 论文 Agent 可解释指标",
        border_style="bright_blue",
    ))

    # ── 1. 生成耗时 ──
    _print_phase_timings(report)

    # ── 2. 规则命中与消解 ──
    _print_rule_and_resolution(report)

    # ── 3. 用户时间节省 ──
    _print_time_saved(report)

    console.print(f"\n[dim]原始日志: {path}[/dim]\n")


def _print_phase_timings(report: ExplainabilityReport) -> None:
    """打印阶段耗时表。"""
    timings = report.phase_timings
    if not timings:
        return

    # 按 to_phase 聚合（同阶段多次转移的累加）
    phase_ms: dict[str, float] = {}
    phase_order: list[str] = []
    for t in timings:
        key = t.to_phase
        if key not in phase_ms:
            phase_ms[key] = 0.0
            phase_order.append(key)
        phase_ms[key] += t.duration_ms

    total_ms = sum(phase_ms.values())
    if total_ms <= 0:
        return

    table = Table(title=f"🕐 生成耗时（总墙钟 {_fmt_seconds(report.total_wall_time_s)}）")
    table.add_column("阶段", style="cyan")
    table.add_column("耗时", justify="right")
    table.add_column("占比", justify="right")

    for phase in phase_order:
        ms = phase_ms[phase]
        pct = ms / total_ms * 100
        table.add_row(phase, _fmt_seconds(ms / 1000), f"{pct:.1f}%")

    table.add_row("总计", _fmt_seconds(total_ms / 1000), "100%", style="bold")
    console.print(table)


def _print_rule_and_resolution(report: ExplainabilityReport) -> None:
    """打印规则命中与消解率摘要。"""
    rr = report.rule_and_resolution
    if rr.first_round_errors == 0 and rr.first_round_warnings == 0 and not rr.resolution_by_round:
        return

    lines: list[str] = []

    # 趋势行
    err_arrow = "✅" if rr.last_round_errors == 0 else f"❌ {rr.last_round_errors}"
    lines.append(
        f"初稿 → 终稿：Error {rr.first_round_errors}→{err_arrow}"
        f" | Warning {rr.first_round_warnings}→{rr.last_round_warnings}"
    )

    # 消解率
    if rr.resolution_by_round:
        res_parts = []
        for r in rr.resolution_by_round:
            if r.static_resolve_pct > 0 or r.actionable_resolve_pct > 0:
                res_parts.append(
                    f"R{r.round} 静态消解 {r.static_resolve_pct:.1f}%"
                )
        if res_parts:
            lines.append(" → ".join(res_parts))

        # 顽固问题
        stubborn_seq = [str(r.stubborn_count) for r in rr.resolution_by_round if r.round > 0]
        if stubborn_seq and any(int(s) > 0 for s in stubborn_seq):
            lines.append(f"顽固问题 {'→'.join(stubborn_seq)}")

    if rr.total_static_resolution_pct != 0:
        lines.append(f"全流程静态规则消解率 {rr.total_static_resolution_pct:.1f}%")

    if rr.error_cleared_round is not None:
        lines.append(f"error 清零于 R{rr.error_cleared_round}（第 {rr.error_cleared_round + 1} 次评估）")
    elif rr.last_round_errors > 0:
        lines.append(f"[yellow]⚠ 终稿仍有 {rr.last_round_errors} 条 error 未消解[/yellow]")

    # 高频规则
    if rr.top_rules:
        rule_parts = [f"{rid}({cnt}次)" for rid, cnt in rr.top_rules]
        lines.append(f"高频规则：{' | '.join(rule_parts)}")

    console.print(Panel(
        "\n".join(lines),
        title="📏 规则命中与消解（全流程）",
        border_style="green" if rr.last_round_errors == 0 else "yellow",
    ))


def _print_time_saved(report: ExplainabilityReport) -> None:
    """打印用户时间节省估算。"""
    ts = report.time_saved
    if ts.draft_words <= 0:
        return

    lines = [
        f"论文 {ts.draft_words:,} 字 | 人工估算 {_fmt_hours(ts.manual_hours_neutral)}（中性）"
        f" | AI 耗时 {_fmt_hours(ts.ai_hours)}",
        f"节省 ~{_fmt_hours(ts.time_saved_hours)}"
        f"（约 {ts.work_sessions_saved:.0f} 个半天）"
        f"  保守 {_fmt_hours(ts.conservative_hours)}"
        f" | 乐观 {_fmt_hours(ts.optimistic_hours)}",
        "",
        "[dim]⚠ 基于人工写作速度的粗略估算，仅供参考[/dim]",
    ]

    console.print(Panel(
        "\n".join(lines),
        title="⏱ 用户时间节省估算",
        border_style="bright_magenta",
    ))
