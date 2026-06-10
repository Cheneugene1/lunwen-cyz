"""
修订辅助：静态规则问题对比（修复率指标）、按章节拆分 actionable_items。
"""

from __future__ import annotations

import re
from typing import Iterable

from ..models import StaticRuleIssue


def normalize_static_issue(text: str) -> str:
    """用于跨轮次对比的同义规范化（空白折叠）。"""
    return re.sub(r"\s+", " ", (text or "").strip())


def static_issue_delta(
    previous_issues: Iterable[str],
    current_issues: Iterable[str],
) -> dict[str, int | float]:
    """
    对比两轮评估的静态规则问题集合（规范化后）。
    返回：prev_total, resolved, new, net, resolve_rate（前一轮基数上的消解比例）。
    """
    prev_set = {normalize_static_issue(x) for x in previous_issues if x and x.strip()}
    cur_set = {normalize_static_issue(x) for x in current_issues if x and x.strip()}
    resolved = len(prev_set - cur_set)
    new = len(cur_set - prev_set)
    prev_total = len(prev_set)
    net = resolved - new
    rate = (resolved / prev_total) if prev_total else 0.0
    net_rate = (net / prev_total) if prev_total else 0.0
    return {
        "prev_total": prev_total,
        "resolved": resolved,
        "new": new,
        "net": net,
        "resolve_rate_pct": round(rate * 100, 1),
        "net_fix_rate_pct": round(net_rate * 100, 1),
    }


def static_issue_delta_by_rule_id(
    previous: Iterable[StaticRuleIssue],
    current: Iterable[StaticRuleIssue],
) -> dict[str, int | float | list[str]]:
    """按 rule_id@rule_version 集合差分（规则升级时可 bump rule_version）。"""

    def _key(x: StaticRuleIssue) -> str:
        v = getattr(x, "rule_version", None) or "1"
        return f"{x.rule_id}@{v}"

    prev_ids = {_key(x) for x in previous if x.rule_id}
    cur_ids = {_key(x) for x in current if x.rule_id}
    resolved_keys = sorted(prev_ids - cur_ids)
    new_keys = sorted(cur_ids - prev_ids)
    prev_total = len(prev_ids)
    resolved = len(resolved_keys)
    new = len(new_keys)
    net = resolved - new
    rate = (resolved / prev_total) if prev_total else 0.0
    net_rate = (net / prev_total) if prev_total else 0.0
    return {
        "prev_total": prev_total,
        "resolved": resolved,
        "new": new,
        "net": net,
        "resolve_rate_pct": round(rate * 100, 1),
        "net_fix_rate_pct": round(net_rate * 100, 1),
        "resolved_rule_ids": resolved_keys,
        "new_rule_ids": new_keys,
    }


_CHAPTER_ZH = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _chapter_numbers_normalized(text: str) -> set[int]:
    nums = {int(m) for m in re.findall(r"第(\d+)章", text)}
    for ch, n in _CHAPTER_ZH.items():
        if f"第{ch}章" in text:
            nums.add(n)
    return nums


def _keyword_hints_for_fingerprint(text: str, max_tokens: int = 3) -> str:
    rest = re.sub(r"^【[^】]+】\s*", "", text)
    toks = re.findall(r"[\u4e00-\u9fff]{2,5}", rest[:120])
    out: list[str] = []
    for t in toks:
        if t not in out:
            out.append(t)
        if len(out) >= max_tokens:
            break
    return "|".join(out)


def actionable_coarse_fingerprints(
    items: Iterable[str],
    *,
    include_keyword_hints: bool = False,
) -> set[str]:
    """
    从 LLM/合并后的 actionable 条目中提取粗粒度指纹：【标签】+ 章节号；
    无章节时用 标签|*；可选追加少量关键词子串（配置 actionable_fingerprint_include_keywords）。
    """
    fps: set[str] = set()
    for raw in items:
        text = (raw or "").strip()
        if not text:
            continue
        mtag = re.match(r"^【([^】]+)】", text)
        tag = mtag.group(1).strip() if mtag else "_未分类"
        chs = _chapter_numbers_normalized(text)
        hint = ""
        if include_keyword_hints:
            hint = _keyword_hints_for_fingerprint(text)
        if not chs:
            base = f"{tag}|*"
            fps.add(f"{base}|{hint}" if hint else base)
        else:
            for n in sorted(chs):
                base = f"{tag}|{n}"
                fps.add(f"{base}|{hint}" if hint else base)
    return fps


def actionable_coarse_delta(
    previous_items: Iterable[str],
    current_items: Iterable[str],
    *,
    include_keyword_hints: bool = False,
) -> dict[str, int | float | list[str]]:
    prev = actionable_coarse_fingerprints(
        previous_items, include_keyword_hints=include_keyword_hints
    )
    cur = actionable_coarse_fingerprints(
        current_items, include_keyword_hints=include_keyword_hints
    )
    resolved = sorted(prev - cur)
    new = sorted(cur - prev)
    prev_total = len(prev)
    r_count = len(resolved)
    n_count = len(new)
    net = r_count - n_count
    rate = (r_count / prev_total) if prev_total else 0.0
    net_rate = (net / prev_total) if prev_total else 0.0
    return {
        "prev_fp_total": prev_total,
        "resolved_fp": r_count,
        "new_fp": n_count,
        "net_fp": net,
        "resolve_rate_pct": round(rate * 100, 1),
        "net_fix_rate_pct": round(net_rate * 100, 1),
        "resolved_fingerprints": resolved,
        "new_fingerprints": new,
    }


def dedupe_llm_against_static(
    static_issues: Iterable[StaticRuleIssue],
    llm_items: list[str],
) -> list[str]:
    """
    去掉与静态规则 message 重复或高度重叠的 LLM 建议，降低评估列表冗余。
    """
    static_list = list(static_issues)
    if not static_list or not llm_items:
        return [x for x in llm_items if x and str(x).strip()]

    static_norms = {normalize_static_issue(s.message) for s in static_list}
    static_ids = {s.rule_id for s in static_list}
    prefixes = {s.rule_id.split(":", 1)[0] for s in static_list}

    out: list[str] = []
    for item in llm_items:
        it = (item or "").strip()
        if not it:
            continue
        n = normalize_static_issue(it)
        if n in static_norms:
            continue
        dup_sub = False
        for sm in static_norms:
            if len(sm) >= 18 and sm in n:
                dup_sub = True
                break
            if len(n) >= 18 and n in sm:
                dup_sub = True
                break
        if dup_sub:
            continue
        # 与静态同主题的常见 LLM 复述（保守匹配）
        if "mcu_abstract_body_mismatch" in static_ids and "型号一致性" in it:
            continue
        if "citation_after_punct" in prefixes and "【引用】" in it and (
            "标点" in it or "标点后" in it
        ):
            continue
        if any(p.startswith("section_overflow") for p in static_ids) and "【结构】" in it and (
            "越界" in it or "混入" in it and "章" in it
        ):
            continue
        if any(rid.startswith("abstract_") for rid in static_ids) and (
            "摘要违规" in it or ("【语言】" in it and "摘要" in it and "图表" in it)
        ):
            continue
        out.append(item)
    return out


def static_rule_summary_lines(issues: Iterable[StaticRuleIssue], max_show: int = 5) -> list[str]:
    """供评估面板展示当前静态规则摘要（含 severity / category / version）。"""
    lines: list[str] = []
    for i, iss in enumerate(issues):
        if i >= max_show:
            break
        sev = getattr(iss, "severity", "error") or "error"
        cat = getattr(iss, "rule_category", "general") or "general"
        ver = getattr(iss, "rule_version", "1") or "1"
        msg = iss.message[:120] + ("…" if len(iss.message) > 120 else "")
        lines.append(f"- [{sev}][{cat}] `{iss.rule_id}`@{ver}：{msg}")
    return lines


def extract_stubborn_actionable_items(
    previous_items: list[str],
    current_items: list[str],
) -> list[str]:
    """
    从两轮 actionable_items 中提取跨轮未消解的具体条目原文。
    对比粗粒度指纹交集，返回当前轮中仍存在的条目。
    """
    prev_fp = actionable_coarse_fingerprints(previous_items)
    cur_map: dict[str, str] = {}
    fp_to_items: dict[str, list[str]] = {}
    for item in current_items:
        fps = actionable_coarse_fingerprints([item])
        for fp in fps:
            fp_to_items.setdefault(fp, []).append(item)
    persistent_fp = prev_fp & set(fp_to_items.keys())
    result: list[str] = []
    seen: set[str] = set()
    for fp in sorted(persistent_fp):
        for item in fp_to_items[fp]:
            if item not in seen:
                seen.add(item)
                result.append(item)
    return result


def _chapter_nums_in_item(item: str) -> set[int]:
    return {int(m) for m in re.findall(r"第(\d+)章", item)}


def _mentions_section_subchapter(item: str, chapter_num: int) -> bool:
    """如 3.2节、第3章3.2 —— 归属第 chapter_num 章。"""
    if f"第{chapter_num}章" in item:
        return True
    if re.search(rf"(?<![\d\.]){chapter_num}\.\d+", item):
        return True
    return False


def _is_abstract_only_issue_for_body(item: str, section_id: str) -> bool:
    """正文修订时不应把「只涉及摘要/关键词行」的问题当作本章主任务（避免乱改）。"""
    if not section_id.startswith("s") or not section_id[1:].isdigit():
        return False
    if not re.search(r"摘要|关键词|Abstract|Keywords", item):
        return False
    # 同条既点摘要又点具体章节时，正文章仍需处理正文侧
    if _chapter_nums_in_item(item):
        return False
    if _mentions_section_subchapter(item, int(section_id[1:])):
        return False
    return True


def partition_actionable_items(
    section_id: str,
    section_title: str,
    items: list[str],
) -> tuple[list[str], list[str]]:
    """
    将评估建议分为：
    - primary：与本章修订直接相关；
    - other：主要针对其他章/全文协调，模型勿为迎合而在本章编造或硬删他章内容。

    若无法为某章列出任何 primary，则退回全量列表（避免无建议可用）。
    """
    if not items:
        return [], []

    primary: list[str] = []
    other: list[str] = []

    stitle = (section_title or "").strip()
    for item in items:
        it = item.strip()
        if not it:
            continue

        is_primary = False

        if section_id == "abstract_zh":
            if re.search(r"摘要|图表|关键词|abstract", it, re.I) or "摘要违规" in it:
                is_primary = True
            elif not _chapter_nums_in_item(it) and re.search(r"型号|主控|传感器|DHT|DS18|STM32|ESP", it):
                # 摘要中可能重复出现型号词
                is_primary = True
        elif section_id == "abstract_en":
            if re.search(
                r"Abstract|英文摘要|英文关键词|Keywords|\babstract\b", it, re.I
            ):
                is_primary = True
        elif section_id == "keywords":
            if "关键词" in it or "Keywords" in it:
                is_primary = True
        elif section_id == "refs":
            if re.search(
                r"参考文献|引用列表|佚名|【引用】|未出现在参考文献|\[\d+\]",
                it,
            ):
                is_primary = True
        elif section_id == "acknowledgment":
            if re.search(r"致谢|acknowledge|感言|个人感悟", it, re.I):
                is_primary = True
        elif section_id.startswith("s") and section_id[1:].isdigit():
            k = int(section_id[1:])
            if _is_abstract_only_issue_for_body(it, section_id):
                is_primary = False
            elif _mentions_section_subchapter(it, k):
                is_primary = True
            else:
                nums = _chapter_nums_in_item(it)
                if not nums:
                    if re.search(r"全文|各章|逐一|统一为", it):
                        is_primary = True
                    elif stitle and stitle in it:
                        is_primary = True
                    else:
                        is_primary = True  # 未标注章节时默认本章可能相关
                elif k in nums:
                    is_primary = True
                else:
                    is_primary = False
            is_conclusion_sec = bool(stitle and "结论" in stitle)
            if is_primary and not is_conclusion_sec:
                if re.search(r"结论|研究问题|末章", it) and not _mentions_section_subchapter(
                    it, k
                ):
                    nums_ck = _chapter_nums_in_item(it)
                    if not nums_ck or k not in nums_ck:
                        is_primary = False
        else:
            is_primary = True

        if is_primary:
            primary.append(item)
        else:
            other.append(item)

    if not primary:
        return list(items), []

    return primary, other


def identify_stubborn_issues(
    previous_items: list[str],
    current_items: list[str],
    previous_static_rule_ids: set[str],
    current_static_rule_ids: set[str],
) -> list[str]:
    """
    识别跨轮次未被解决的问题，返回供修订 prompt 使用的顽固问题摘要。
    对比维度：静态 rule_id 集合 + actionable 粗粒度指纹集合。
    返回空列表表示无可追踪的顽固问题。
    """
    prev_fp = actionable_coarse_fingerprints(previous_items)
    cur_fp = actionable_coarse_fingerprints(current_items)

    # 仍存在的静态规则（交集）
    persistent_static = previous_static_rule_ids & current_static_rule_ids
    # 仍存在的 actionable 指纹（交集）
    persistent_fp = prev_fp & cur_fp

    lines: list[str] = []
    if persistent_static:
        lines.append("**以下静态规则问题在上一轮修订后仍未解决：**")
        for rid in sorted(persistent_static):
            lines.append(f"- `{rid}`")
    if persistent_fp:
        if lines:
            lines.append("")
        lines.append("**以下内容类问题在上一轮修订后仍未解决：**")
        for fp in sorted(persistent_fp):
            label = fp.replace("|", " | ")
            lines.append(f"- {label}")

    return lines
