"""
终端展示层 — 将评估结果格式化为 Rich Panel。

与 controller 的流程编排分离，不依赖 LLM 或状态机。
pure display logic：接收数据 → 渲染 Panel → 输出到 Console。
"""
from typing import Optional

from rich.console import Console
from rich.panel import Panel

from .config import get
from .models import Evaluation, StaticRuleIssue
from .writing.revision_helpers import static_rule_summary_lines

console = Console()


def render_eval_panel(ev: Evaluation, quality_threshold: float):
    """渲染主评估报告 Panel（总分 + 四维分 + 反馈 + 建议列表）。"""
    console.print(
        Panel(
            f"**总分：{ev.score_total:.1f} / 10**\n\n"
            f"- 结构：{ev.dimensions.structure:.1f}　逻辑：{ev.dimensions.logic:.1f}\n"
            f"- 语言：{ev.dimensions.language:.1f}　匹配：{ev.dimensions.alignment:.1f}\n\n"
            f"**总体评价**：{ev.feedback}\n\n"
            + ("\n".join(f"• {item}" for item in ev.actionable_items)
               if ev.actionable_items else ""),
            title="📊 论文评估报告",
            border_style=(
                "green" if ev.score_total >= quality_threshold else "yellow"
            ),
        )
    )


def render_qa_panel(
    ev: Evaluation,
    static_delta: Optional[dict],
    coarse_delta: Optional[dict],
    *,
    thesis_mode: bool,
    max_show: Optional[int] = None,
):
    """渲染修订质量面板（静态硬规则快照 + 差分对比）。"""
    if max_show is None:
        max_show = int(get("panel_static_summary_max_items", 5))

    qa_parts: list[str] = []

    # ── 静态硬规则快照 ──
    if thesis_mode:
        n_st = len(ev.static_rule_issues or [])
        sum_lines = static_rule_summary_lines(
            ev.static_rule_issues or [],
            max_show=max_show,
        )
        snap = (
            "[dim]静态硬规则（`_check_thesis_rules`）当前命中 **"
            f"{n_st}** 条；下方评估报告中的 `•` 列表为 LLM 建议 + 上列 hard 的合并，"
            "不全等于静态集合。[/dim]\n"
        )
        snap += "\n".join(sum_lines) if sum_lines else "_（无静态硬规则）_"
        qa_parts.append(snap)

    # ── 差分对比 ──
    deltas_md: list[str] = []
    if static_delta is not None:
        pct_note = (
            f"消解率 {static_delta['resolve_rate_pct']}%　"
            f"净修复率 {static_delta['net_fix_rate_pct']}%（按 **rule_id@rule_version**）"
        )
        if static_delta["prev_total"] == 0 and static_delta["new"] > 0:
            summary_line = f"上一轮无静态 rule_id；本轮新增 {static_delta['new']} 个。"
        elif static_delta["prev_total"] == 0:
            summary_line = "上一轮无静态 rule_id；本轮仍无。"
        else:
            summary_line = (
                f"消解 {static_delta['resolved']}　"
                f"新增 {static_delta['new']}　净 {static_delta['net']}　{pct_note}"
            )
        rid_res = static_delta.get("resolved_rule_ids") or []
        rid_new = static_delta.get("new_rule_ids") or []
        extra = ""
        if rid_res:
            extra += f"\n- 已消解：`{'`, `'.join(rid_res)}`"
        if rid_new:
            extra += f"\n- 新增：`{'`, `'.join(rid_new)}`"
        deltas_md.append("**静态规则（rule_id 差分）**\n" + summary_line + extra)

    if coarse_delta is not None:
        cf = coarse_delta
        deltas_md.append(
            "**建议粗粒度指纹**（`【标签】|章节`，仅对比 `actionable_items` 句式变化）\n"
            f"- 指纹基数 {cf['prev_fp_total']}　消解 {cf['resolved_fp']}　"
            f"新增 {cf['new_fp']}　净 {cf['net_fp']}　"
            f"消解率 {cf['resolve_rate_pct']}%　净修复率 {cf['net_fix_rate_pct']}%"
        )
        rf = cf.get("resolved_fingerprints") or []
        nw = cf.get("new_fingerprints") or []
        if rf:
            deltas_md.append("- 已消解指纹：`" + "`, `".join(rf) + "`")
        if nw:
            deltas_md.append("- 新增指纹：`" + "`, `".join(nw) + "`")

    if deltas_md:
        qa_parts.append("\n\n".join(deltas_md))

    if not qa_parts:
        return

    net_combined = 0
    if static_delta is not None:
        net_combined += int(static_delta.get("net", 0) or 0)
    if coarse_delta is not None:
        net_combined += int(coarse_delta.get("net_fp", 0) or 0)

    console.print(
        Panel(
            "\n\n".join(qa_parts),
            title="📉 修订质量与硬规则快照",
            border_style="cyan" if net_combined >= 0 else "yellow",
        )
    )


def render_stubborn_panel(stubborn_md: str):
    """渲染顽固问题 Panel（跨轮未消解项）。"""
    console.print(
        Panel(
            stubborn_md,
            title="⚠ 顽固问题（跨轮未消解，下轮修订优先处理）",
            border_style="red",
        )
    )
