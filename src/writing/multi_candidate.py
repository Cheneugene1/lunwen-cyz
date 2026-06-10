"""
初稿多候选生成与筛选（仅 draft 阶段）

对配置的 section_id 并行或串行生成 N 版正文 → 规则预筛 → LLM 为各候选打分 → 取最优。
修订阶段不启用。分块生成（大章 chunk）与多候选互斥：分块章仍走原逻辑。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..config import get
from ..llm import build_messages, chat_json
from ..models import SectionNode, WritingPlan

logger = logging.getLogger(__name__)

_SYSTEM_CANDIDATE_RANK = """你是一名严谨的本科毕业论文评阅助手。
你将收到同一章节的若干篇候选正文（编号从 0 递增）。请只根据给定材料评价，不要臆造事实。

评分维度（综合为 0～10 分，可带一位小数）：
- 与「技术规范摘录」及章节要点的一致性（不得与硬件/协议事实矛盾）
- 对章节要点的覆盖与结构完整
- 学术书面语与逻辑连贯

硬性结构边界（用户消息中的「章节结构边界」或「摘要专用边界」优先）：
- 若某候选违反边界（例如写出了下一章才应有的小节标题体系、或将摘要写成带章节号的正文），
  则该候选的综合得分不得超过 3 分；明显整段越界可给 0～1 分。
- 不因语言华丽而放宽结构越界。

必须输出合法 JSON 对象，格式为：{"scores":[...]}。
scores 数组长度必须与候选数量完全一致，scores[i] 对应候选 i。"""


def multi_candidate_settings() -> dict[str, Any]:
    """读取 multi_candidate 配置，带默认值（默认关闭）。"""
    raw = get("multi_candidate", None)
    if not isinstance(raw, dict):
        raw = {}
    n = int(raw.get("n") or 3)
    n = max(2, min(n, 5))
    thr = float(raw.get("llm_score_threshold") or 6.0)
    thr = max(0.0, min(thr, 10.0))
    tj = float(raw.get("temperature_jitter") or 0.04)
    return {
        "enabled": bool(raw.get("enabled", False)),
        "section_ids": list(raw.get("section_ids") or []),
        "n": n,
        "llm_score_threshold": thr,
        "rule_prefilter": bool(raw.get("rule_prefilter", True)),
        "parallel": bool(raw.get("parallel", False)),
        "temperature_jitter": max(0.0, min(tj, 0.2)),
    }


def section_uses_multi_candidate(
    section_id: str,
    thesis_mode: bool,
    settings: dict[str, Any],
) -> bool:
    if not thesis_mode or not settings.get("enabled"):
        return False
    ids = settings.get("section_ids") or []
    if not ids:
        return False
    return section_id in ids


def rule_prefilter_candidates(
    bodies: list[str],
    section_id: str,
    target_words: int,
) -> list[str]:
    """
    规则预筛：去掉过短/过长/明显失败占位文本。
    target_words 来自 thesis 配置（用于非摘要章节）。
    """
    if section_id == "abstract_zh":
        min_c, max_c = 200, 3500
    elif section_id == "abstract_en":
        min_c, max_c = 80, 3000
    else:
        min_c = max(80, int(target_words * 0.12))
        max_c = max(4500, int(target_words * 12))

    bad_markers = (
        "本章节生成失败",
        "本部分生成失败",
        "生成失败，请手动",
        "（本章节生成失败",
        "（本部分生成失败",
    )

    out: list[str] = []
    for b in bodies:
        s = (b or "").strip()
        if not s:
            continue
        if any(m in s for m in bad_markers):
            continue
        L = len(s)
        if L < min_c or L > max_c:
            continue
        out.append(s)
    return out


def _outline_headlines_for_rank(plan: WritingPlan, current_section_id: str) -> str:
    """供评分模型感知全文结构，避免把其他章内容写进本章。"""
    lines: list[str] = []
    for node in plan.outline:
        if node.section_id == current_section_id:
            lines.append(f"- **`{node.section_id}`（当前章）** {node.title}")
        else:
            lines.append(f"- `{node.section_id}` {node.title}")
    return "\n".join(lines) if lines else "（无大纲）"


def _scope_lines_for_rank(section: SectionNode) -> str:
    parts: list[str] = []
    if section.scope_must_include:
        parts.append(
            "**必须包含（子串意义上应出现，缺则扣分）**："
            + "；".join(str(x).strip() for x in section.scope_must_include[:10] if str(x).strip())
        )
    if section.scope_forbidden:
        parts.append(
            "**禁止出现（出现则大幅扣分）**："
            + "；".join(str(x).strip() for x in section.scope_forbidden[:10] if str(x).strip())
        )
    return "\n\n".join(parts) if parts else ""


def _chapter_boundary_rank_text(section: SectionNode) -> str:
    """
    与 writer._clean_section_overflow 语义对齐的可读边界说明（正文章 s1–s6）。
    """
    sid = section.section_id
    if not sid.startswith("s") or not sid[1:].isdigit():
        return (
            "本节非 s1–s6 正文；请勿写出其他正文章节才应有的一级/二级章节标题体系，"
            "或把下一章内容前置到本节。"
        )
    n = int(sid[1:])
    nxt = n + 1
    return (
        f"当前为论文**第 {n} 章**（`section_id={sid}`）。下列任一情形视为**结构越界**：\n"
        f"- 行首 Markdown 标题形如：`### {nxt}.x`、`## 第{nxt}章`、`## {nxt} `、`### 第{nxt}章`（含 `#` 与「第」之间少空格）\n"
        f"- 正文**末尾区域**出现裸行：「第 {nxt} 章」「第{nxt}章」开头的一行，或「{nxt} 」+ 中文标题（下一章起手式）\n"
        f"- 正文**末尾区域**行首无 `#` 的编号小节：如 `{nxt}.1 …`、`{nxt}.2 …`（主编号大于 {n}）\n"
        "不算越界：正文中的交叉引用句（如「详见第3章」），前提是**不展开**该章的小节结构。\n"
        "越界候选得分不得超过 3 分；明显附录式写入下一章多小节则 0～1 分。"
    )


def _abstract_boundary_rank_text() -> str:
    return (
        "当前为**中文摘要**。\n"
        "禁止：参考文献标注 [n]、图/表引用、以及正文章节式标题（如 `### 2.1`、`## 第2章`、行首编号小节）。\n"
        "违反则该候选得分不得超过 3 分。"
    )


def _overflow_patterns_next_chapter(section_id: str) -> list[re.Pattern[str]]:
    """与 writer._clean_section_overflow 中「下一章」全文匹配部分一致的 regex 列表。"""
    if not section_id.startswith("s") or not section_id[1:].isdigit():
        return []
    nxt = int(section_id[1:]) + 1
    return [
        re.compile(rf"(?m)^#{{1,3}}\s+{nxt}\.\d"),
        re.compile(rf"(?m)^##\s+第{nxt}章"),
        re.compile(rf"(?m)^##\s+{nxt}\s"),
        re.compile(rf"(?m)^#+\s+{nxt}\."),
        re.compile(rf"(?m)^#{{1,3}}\s+第\s*{nxt}\s*章"),
    ]


def _candidate_has_structural_overflow(section_id: str, text: str) -> bool:
    """正文 s1–s6：是否含下一章标题模式（与后处理截断规则一致，全文扫描）。"""
    if not text:
        return False
    for pat in _overflow_patterns_next_chapter(section_id):
        if pat.search(text):
            return True
    # 末尾窗口裸标题（与 writer 默认 tail 30% 一致）
    if not section_id.startswith("s") or not section_id[1:].isdigit():
        return False
    nxt = int(section_id[1:]) + 1
    tail_frac = float(get("section_overflow_tail_scan_fraction", 0.3))
    tail_frac = min(0.95, max(0.05, tail_frac))
    tail_start = int(len(text) * (1.0 - tail_frac))
    tail = text[tail_start:]
    tail_pats = [
        re.compile(rf"(?m)^第\s*{nxt}\s*章(?:\s|$|[\u4e00-\u9fff])"),
        re.compile(rf"(?m)^{nxt}\s+[\u4e00-\u9fff]"),
    ]
    if any(p.search(tail) for p in tail_pats):
        return True
    current = int(section_id[1:])
    para_pat = re.compile(r"(?m)^(\d+)\.(\d+)\s+")
    for m in para_pat.finditer(text):
        if m.start() < tail_start:
            continue
        try:
            major = int(m.group(1))
        except ValueError:
            continue
        if current < major <= 20:
            return True
    return False


def _abstract_structural_violation(text: str) -> bool:
    if not text:
        return False
    if re.search(r"(?m)^#{1,3}\s*\d+\.\d", text):
        return True
    if re.search(r"(?m)^#{1,3}\s*第\s*[\d一二三四五六七八九十]+\s*章", text):
        return True
    return False


def _cap_scores_for_boundary(
    candidates: list[str],
    section_id: str,
    scores: list[float],
) -> list[float]:
    """
    LLM 打分后做确定性封顶：命中越界模式则分数不超过 3，避免「高分低能」仅靠提示被忽略。
    """
    out: list[float] = []
    for i, (body, sc) in enumerate(zip(candidates, scores)):
        v = float(sc)
        bad = False
        if section_id == "abstract_zh":
            bad = _abstract_structural_violation(body or "")
        elif section_id.startswith("s") and section_id[1:].isdigit():
            bad = _candidate_has_structural_overflow(section_id, body or "")
        if bad:
            capped = min(v, 3.0)
            if capped < v:
                logger.info(
                    "多候选 #%d 命中结构越界模式，分数 %.2f 封顶为 %.2f",
                    i, v, capped,
                )
            out.append(capped)
        else:
            out.append(v)
    return out


def llm_pick_best_candidate(
    candidates: list[str],
    section: SectionNode,
    plan: WritingPlan,
    tech_spec_text: str,
    llm_threshold: float,
) -> tuple[int, list[float]]:
    """
    调用 LLM 对候选打分，返回 (最优下标, 各候选分数列表)。
    解析失败时返回 (0, [])，由调用方视为「保留第一个候选」。
    """
    if len(candidates) <= 1:
        return 0, []

    bullets = "；".join(section.bullets[:12]) if section.bullets else "（未列出要点）"
    spec_excerpt = (tech_spec_text or "")[:2800]

    boundary_heading = (
        "摘要专用边界"
        if section.section_id == "abstract_zh"
        else "章节结构边界"
    )
    boundary_block = (
        _abstract_boundary_rank_text()
        if section.section_id == "abstract_zh"
        else _chapter_boundary_rank_text(section)
    )
    scope_blk = _scope_lines_for_rank(section)
    headlines = _outline_headlines_for_rank(plan, section.section_id)

    blocks = []
    cap = 3200
    for i, text in enumerate(candidates):
        blocks.append(f"### 候选 {i}\n{(text or '')[:cap]}")

    user = (
        f"## 章节标题\n{section.title}\n\n"
        f"## 章节 section_id\n{section.section_id}\n\n"
        f"## 论文结构（各章标题；勿把其他章节的正文级展开写进本章）\n{headlines}\n\n"
        f"## 章节要点\n{bullets}\n\n"
        + (f"## 规划边界（scope）\n{scope_blk}\n\n" if scope_blk else "")
        + f"## {boundary_heading}\n{boundary_block}\n\n"
        f"## 论文题目（参考）\n{plan.title or '（无）'}\n\n"
        f"## 技术规范摘录\n{spec_excerpt or '（无）'}\n\n"
        + "\n\n".join(blocks)
        + '\n\n请输出 JSON：{"scores": [各候选分数]}'
    )

    messages = build_messages(_SYSTEM_CANDIDATE_RANK, user)
    data = chat_json(messages, temperature=0.15, max_tokens=800)
    scores = data.get("scores") if isinstance(data, dict) else None
    if not isinstance(scores, list) or len(scores) != len(candidates):
        logger.warning(
            "多候选 LLM 打分结果无效（长度 %s 与候选数 %d 不符），保留候选 0",
            scores, len(candidates),
        )
        return 0, []

    norm: list[float] = []
    for x in scores:
        try:
            v = float(x)
        except (TypeError, ValueError):
            v = 0.0
        norm.append(max(0.0, min(10.0, v)))

    norm = _cap_scores_for_boundary(candidates, section.section_id, norm)

    best_i = max(range(len(norm)), key=lambda i: norm[i])
    best_score = norm[best_i]
    if best_score < llm_threshold:
        logger.warning(
            "多候选最高分 %.2f 低于阈值 %.2f，仍采用该最优候选",
            best_score,
            llm_threshold,
        )
    else:
        logger.info(
            "章节 [%s] 多候选选中 #%d（择优分 %.2f，已含结构越界规则封顶）",
            section.title,
            best_i,
            best_score,
        )
    return best_i, norm
