"""
修订引擎 — 根据评估建议修订论文各章节。

包含 System Prompt、prompt 构建、顽固问题专项修复、修订自检。
依赖 helpers / postprocess，无循环导入（postprocess 不依赖本模块）。
"""
import logging
import re as _re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

from ..config import get
from ..llm import chat, chat_json, build_messages
from ..models import Manuscript, ManuscriptSection, WritingPlan
from ..ref_store import ReferenceStore
from .helpers import (
    _EXECUTION_PROTOCOL,
    _align_subsections_titles,
    _build_ref_context,
    _build_ref_list_section,
    _build_sensor_checklist,
    _chunk_body_looks_truncated,
    _executable_outline_prompt_section,
    _get_section_rule,
    _get_section_target_words,
    _is_thesis_mode,
    _neighbor_chapter_boundary_hint,
    _strip_revision_artifacts,
)
from .postprocess import _clean_section_overflow, _finalize_manuscript_postprocess, _fix_citation_position
from .revision_helpers import partition_actionable_items
from .scope_enforce import (
    scope_validation_settings,
    section_has_scope_constraints,
    validate_section_body_scope,
)
from .tech_spec import format_tech_spec_for_prompt

logger = logging.getLogger(__name__)

# 修订阶段 LLM 调用重试（网络/流中断等）
_REVISE_MAX_ATTEMPTS = 3
_REVISE_RETRY_BASE_DELAY_SEC = 2.0

# ── System Prompt ─────────────────────────────────────────────

_SYSTEM_REVISE_THESIS = """# Role
你是一位兼具本科毕业论文审阅专家（评审过300+本）与学术写作导师双重身份的修改专家。
你对论文文字有极高的鉴赏力，坚持「最小干预原则」：只在原文确实存在错误时才修改，
绝不为刷存在感而对通顺句子强行同义词替换或句式重组。

# Task
根据评估建议修订指定章节的正文。

# Constraints

1. 修订阈值（最重要）
   - 必须改：事实矛盾、术语不一致、引用越界、缺失标点、逻辑断裂、型号与 TechSpec 不符
   - 不应改：已经通顺的句子、已经准确的用词、已经规范的表达
   - 禁止为了追求"更好"而改写正确的句子——保留作者原有行文风格是第一优先级

2. 引用与格式
   - 只引用"可用参考文献"列表中的文献，格式 [序号]，放在标点之前
   - 每个陈述句以中文句号（。）结尾，禁止英文句点（.），禁止连续重复标点

3. 语言风格
   - 中文学术书面语，平实流畅，客观中立
   - 禁止事项：无故使用"旨在""拟""系"等陈旧公文腔；禁止口语化表达
   - 去 AI 味：拒绝"首先/其次/然后/最后"机械堆砌；每段一个核心论点

4. 术语规则
   - 模型名/芯片型号/协议名严禁展开（保持 STM32 不为"STM32 微控制器"）
   - 全文称呼以 TechSpec（技术规范文档）为准

5. 输出格式
   直接输出修订后的完整章节正文（Markdown），不输出任何前缀、标签或修改说明。
   禁止输出 "Part 1"、"Part 2"、"修改日志"、"修订说明" 等任何标记性文字。
   如果文中出现 <!-- TODO: 待撰写 --> 标记，必须将其展开为完整小节内容。

# 注意事项
- 逐条处理评估建议中的所有修改项
- 不新增与本章无关的内容，不删除与本章主题相关的有效段落
""" + _EXECUTION_PROTOCOL


# ── 术语锁定快照 ─────────────────────────────────────────────

def _build_term_lockdown_snapshot(spec: Optional[dict]) -> str:
    """从 TechSpec 抽取所有硬件型号/传感器型号，生成「术语锁定清单」。"""
    if not spec:
        return ""
    parts: list[str] = []
    hw = spec.get("hardware") or {}
    if not hw:
        return ""
    mcu = hw.get("mcu") or {}
    if mcu and mcu.get("model"):
        parts.append(f"主控芯片：{mcu['model']}")
    sensors = hw.get("sensors") or []
    for s in sensors:
        if (s or {}).get("model"):
            parts.append(f"{(s or {}).get('name', '传感器')}：{s['model']}")
    actuators = hw.get("actuators") or []
    for a in actuators:
        if a.get("spec") or a.get("name"):
            parts.append(f"{a.get('name', '执行器')}：{a.get('spec', '')}")
    comm = hw.get("communication_module") or {}
    if comm and comm.get("model"):
        parts.append(f"通信模块：{comm['model']}")
    if not parts:
        return ""
    return (
        "## ⚠ 术语一致性锁定清单（以下型号已被技术规范锁定，修订时不得更改）\n"
        + "\n".join(f"- {p}" for p in parts)
    )


# ── Prompt 构建 ───────────────────────────────────────────────

def _build_revise_prompt(
    section,
    plan: WritingPlan,
    current_body: str,
    actionable_items: List[str],
    ref_context: str,
    thesis_mode: bool,
    tech_spec_text: str = "",
    stubborn_issues_md: str = "",
    term_lockdown_snapshot: str = "",
) -> str:
    primary, global_rest = partition_actionable_items(
        section.section_id, section.title, actionable_items
    )
    pri_lines = "\n".join(f"{i+1}. {item}" for i, item in enumerate(primary))
    core_items = ""
    if stubborn_issues_md:
        core_items += (
            "## 【顽固问题 - 上一轮修订后仍未解决，本轮必须优先处理】\n"
            f"{stubborn_issues_md}\n\n"
        )
    core_items += f"## 本章修订重点（请优先处理）\n{pri_lines}\n"
    if global_rest:
        o_lines = "\n".join(f"{i+1}. {item}" for i, item in enumerate(global_rest))
        core_items += (
            "\n## 其他章节或全文性问题\n"
            "以下条目主要针对其他章或全文协调：**禁止**在本章正文中编造他章内容、大段删除与本章无关的段落以「迎合」建议；"
            "仅在本章内统一术语、修正明显错误，并避免引入新的前后矛盾。\n"
            f"{o_lines}\n"
        )
    special_rule = _get_section_rule(section.section_id)
    sensor_checklist_rev = ""
    sensor_checklist_sections = get("sensor_checklist_sections") or ["s5"]
    if section.section_id in sensor_checklist_sections and tech_spec_text:
        sensor_checklist_rev = _build_sensor_checklist(tech_spec_text)

    # 硬件越界检测
    hw_overflow_check_sections = get("hardware_overflow_check_sections") or ["s2"]
    hw_overflow_warning = ""
    if section.section_id in hw_overflow_check_sections:
        _HW_KWS = ["PCB", "原理图", "引脚", "上拉电阻", "下拉电阻", "消抖电路",
                    "PCB布局", "布线", "去耦电容", "晶振电路", "复位电路",
                    "I/O口", "GPIO配置", "时钟树", "中断优先级"]
        hw_hits = sum(1 for kw in _HW_KWS if kw in current_body)
        if hw_hits >= 3:
            hw_overflow_warning = (
                "\n\n⚠ **硬件内容越界警告**：当前章节正文疑似包含硬件实现细节"
                f"（检测到 {hw_hits} 处硬件特征词，如 " +
                "、".join(kw for kw in _HW_KWS if kw in current_body)[:80] +
                "等）。请将这些硬件实现细节移至后续设计/实现章节，本章只保留方案对比与选型论证。\n"
            )

    # 引用范围规则
    citation_scope_rule = ""
    if thesis_mode:
        enabled = get("citation_enabled_sections") or ["s1", "s2"]
        if section.section_id not in enabled and section.section_id not in ("abstract_zh", "abstract_en", "acknowledgment"):
            citation_scope_rule = (
                "\n\n⚠ **引用范围规则**：本章**禁止引用**。"
                "修订时不得新增任何 [n] 引用标记；若当前正文已存在引用标记，必须全部删除。\n"
            )
        elif section.section_id in enabled:
            citation_scope_rule = (
                "\n\n**引用范围**：本章允许引用。引用格式 [n] 置于标点之前，编号须在文献池范围内。\n"
            )

    core = (
        f"## 当前章节：{section.title}\n\n"
        + (f"{special_rule}\n\n" if special_rule else "")
        + (f"{sensor_checklist_rev}\n\n" if sensor_checklist_rev else "")
        + (f"{hw_overflow_warning}\n" if hw_overflow_warning else "")
        + (f"{citation_scope_rule}" if citation_scope_rule else "")
        + f"## 当前正文\n{current_body}\n\n"
        + f"## 修改建议\n{core_items}\n\n"
        + (f"## 可用参考文献\n{ref_context}\n\n" if ref_context else "")
    )
    if thesis_mode:
        exe_block = _executable_outline_prompt_section(section)
        if exe_block.strip():
            core += exe_block + "\n"
        nb = _neighbor_chapter_boundary_hint(plan, section)
        if nb:
            core += nb + "\n"
        if section.scope_must_include or section.scope_forbidden:
            core += (
                "## 修订边界\n"
                "不得删除或弱化「本章必须包含」所涉核心表述；不得新增「本章禁止」所列主题或等价展开。\n\n"
            )
    if thesis_mode and tech_spec_text.strip():
        core += (
            "## 技术规范（修订时必须遵守，不得与其中硬件/软件/算法事实矛盾）\n\n"
            f"{tech_spec_text}\n\n"
        )
    if term_lockdown_snapshot.strip():
        core += term_lockdown_snapshot + "\n\n"
    core += (
        f"## 任务\n"
        f"目标字数：约 {_get_section_target_words(section.section_id)} 字（修订后应与目标一致，过长请精简，过短请补充）。\n"
        "请按修改建议对正文进行修订。直接输出修订后的完整正文，不含章节标题。\n"
        "【全局一致性】若涉及主控芯片、传感器型号、通信协议等，须在本章内全文统一为同一表述，"
        "并与摘要、关键词及其他章节已采用的事实一致，禁止只改段首或首句而留下旧型号。\n"
    )
    return core


# ── 单章修订 ─────────────────────────────────────────────────

def _revise_one_section_body(
    system_prompt: str,
    user_prompt: str,
    section_title: str,
    fallback_body: str,
    max_tokens: int,
) -> str:
    """带重试与疑似截断续写的单章修订 LLM 调用。"""
    messages_full = build_messages(system_prompt, user_prompt)
    for attempt in range(1, _REVISE_MAX_ATTEMPTS + 1):
        try:
            body = chat(messages_full, temperature=0.5, max_tokens=max_tokens)
            if not body.strip():
                raise ValueError("LLM 返回空内容")
            if _chunk_body_looks_truncated(body):
                tail = body[-500:]
                cont_prompt = (
                    "上文可能在句末被截断。请**仅输出续写**：补完未完句子并自然收束（1～5句），"
                    "不要重复上文，不要小标题：\n\n" + tail
                )
                try:
                    cont = chat(
                        build_messages(system_prompt, cont_prompt),
                        temperature=0.35,
                        max_tokens=min(2000, max(600, max_tokens // 2)),
                    )
                    if cont.strip():
                        body = body.rstrip() + "\n" + cont.strip()
                        logger.info("章节 [%s] 修订输出已尝试截断续写", section_title)
                except Exception as ce:
                    logger.warning("章节 [%s] 修订截断续写失败: %s", section_title, ce)
            body = _strip_revision_artifacts(body)
            return body
        except Exception as e:
            logger.warning(
                "章节 [%s] 修订失败 (%d/%d): %s",
                section_title, attempt, _REVISE_MAX_ATTEMPTS, e,
            )
            if attempt < _REVISE_MAX_ATTEMPTS:
                time.sleep(_REVISE_RETRY_BASE_DELAY_SEC * (2 ** (attempt - 1)))
                continue
            logger.error(
                "章节 [%s] 在 %d 次修订尝试后仍失败，保留原文",
                section_title, _REVISE_MAX_ATTEMPTS,
            )
            return fallback_body
    return fallback_body


# ── 主入口 ───────────────────────────────────────────────────

def revise_manuscript(
    manuscript: Manuscript,
    plan: WritingPlan,
    store: ReferenceStore,
    actionable_items: List[str],
    stubborn_issues_md: str = "",
) -> tuple[Manuscript, dict[str, str]]:
    """根据评估建议修订论文。返回 (修订后的 Manuscript, 本轮应用的 term_map)。"""
    if not actionable_items:
        logger.info("无修改建议，跳过修订")
        return manuscript, {}

    thesis_mode = _is_thesis_mode()
    from .helpers import _citation_style as _cs

    if thesis_mode:
        system_prompt = _SYSTEM_REVISE_THESIS
    else:
        from .draft_engine import _citation_style_desc as _csd
        system_prompt = (
            # 普通模式用 draft normal prompt（与初稿一致）
            "你是一位学术论文写作专家。请根据评估建议修订指定章节的正文。"
        )

    section_map = {s.section_id: s for s in plan.outline}
    new_sections: List[ManuscriptSection] = []

    tech_spec_text = ""
    if thesis_mode and manuscript.tech_spec:
        tech_spec_text = format_tech_spec_for_prompt(manuscript.tech_spec)
    term_lockdown = _build_term_lockdown_snapshot(manuscript.tech_spec) if thesis_mode else ""

    # ── 收集需要 LLM 修订的章节（refs / keywords 跳过，不在 section_map 中的保留）──
    revise_sections: list[ManuscriptSection] = []
    for sec in manuscript.sections:
        if sec.section_id in ("refs", "keywords"):
            continue
        node = section_map.get(sec.section_id)
        if not node:
            continue
        revise_sections.append(sec)

    # ── 修订产出 revised_map（串行/并行分支不变）──
    revise_parallel = bool(get("parallel_revise", False))

    if revise_parallel and len(revise_sections) > 1:
        logger.info("REVISE 阶段启用并行修订（%d 章）", len(revise_sections))
        revised_map: dict[str, ManuscriptSection] = {}

        def _revise_one(sec: ManuscriptSection) -> ManuscriptSection:
            node = section_map[sec.section_id]
            ref_context = _build_ref_context(store, node)
            user_prompt = _build_revise_prompt(
                node, plan, sec.markdown_body, actionable_items,
                ref_context, thesis_mode,
                tech_spec_text=tech_spec_text,
                stubborn_issues_md=stubborn_issues_md,
                term_lockdown_snapshot=term_lockdown,
            )
            target_words = _get_section_target_words(sec.section_id)
            max_tok = min(8000, max(1500, int(target_words * 1.8)))
            body = _revise_one_section_body(
                system_prompt, user_prompt, sec.title, sec.markdown_body, max_tok,
            )
            scfg = scope_validation_settings()
            if scfg.get("enabled", True) and node and section_has_scope_constraints(node):
                ok_sc, issues_sc = validate_section_body_scope(body, node)
                if not ok_sc and scfg.get("retry_once", True):
                    fix_sc = (
                        "\n\n## 系统校验反馈（须满足本章必须包含/禁止项）\n"
                        + "\n".join(issues_sc)
                        + "\n请结合评估建议与上述约束，输出完整修订正文。\n"
                    )
                    body = _revise_one_section_body(
                        system_prompt, user_prompt + fix_sc, sec.title, body, max_tok,
                    )
            body = _fix_citation_position(body)
            body = _clean_section_overflow(body, sec.section_id)
            if node:
                _align_subsections_titles(body, node)
            logger.info("并行修订 [%s] 完成", sec.title)
            return ManuscriptSection(
                section_id=sec.section_id, title=sec.title, markdown_body=body,
            )

        with ThreadPoolExecutor(max_workers=min(6, len(revise_sections))) as ex:
            futures = {ex.submit(_revise_one, s): s for s in revise_sections}
            for fut in as_completed(futures):
                result = fut.result()
                revised_map[result.section_id] = result
    else:
        revised_map: dict[str, ManuscriptSection] = {}
        for sec in revise_sections:
            node = section_map[sec.section_id]
            ref_context = _build_ref_context(store, node)
            user_prompt = _build_revise_prompt(
                node, plan, sec.markdown_body, actionable_items,
                ref_context, thesis_mode,
                tech_spec_text=tech_spec_text,
                stubborn_issues_md=stubborn_issues_md,
                term_lockdown_snapshot=term_lockdown,
            )
            target_words = _get_section_target_words(sec.section_id)
            max_tok = min(8000, max(1500, int(target_words * 1.8)))
            body = _revise_one_section_body(
                system_prompt, user_prompt, sec.title, sec.markdown_body, max_tok,
            )
            scfg = scope_validation_settings()
            if scfg.get("enabled", True) and node and section_has_scope_constraints(node):
                ok_sc, issues_sc = validate_section_body_scope(body, node)
                if not ok_sc and scfg.get("retry_once", True):
                    fix_sc = (
                        "\n\n## 系统校验反馈（须满足本章必须包含/禁止项）\n"
                        + "\n".join(issues_sc)
                        + "\n请结合评估建议与上述约束，输出完整修订正文。\n"
                    )
                    body = _revise_one_section_body(
                        system_prompt, user_prompt + fix_sc, sec.title, body, max_tok,
                    )
            body = _fix_citation_position(body)
            body = _clean_section_overflow(body, sec.section_id)
            if node:
                _align_subsections_titles(body, node)
            revised_map[sec.section_id] = ManuscriptSection(
                section_id=sec.section_id, title=sec.title, markdown_body=body,
            )
            logger.info("章节 [%s] 修订完成", sec.title)

    # ── 单次遍历组装：保持 manuscript 原始顺序，refs 放最后 ──
    refs_before_ack = bool(get("reference_before_acknowledgment", False))
    refs_sec = _build_ref_list_section(store, thesis_mode)
    has_ack = any(s.section_id == "acknowledgment" for s in manuscript.sections)
    ack_inserted = False

    for sec in manuscript.sections:
        if sec.section_id == "refs":
            # refs 在致谢之前（配置）→ 遇到 ack 之前插入
            if refs_before_ack and not ack_inserted:
                new_sections.append(refs_sec)
            continue
        if sec.section_id == "keywords":
            continue
        if sec.section_id == "acknowledgment" and not refs_before_ack and not ack_inserted:
            # 修订过的用修订版，没修订过的用原版
            ack_sec = revised_map.get(sec.section_id, sec)
            new_sections.append(ack_sec)
            new_sections.append(refs_sec)  # 默认：refs 放致谢之后
            ack_inserted = True
            continue
        if sec.section_id in revised_map:
            new_sections.append(revised_map[sec.section_id])
        elif sec.section_id in section_map:
            new_sections.append(sec)

    # 兜底：如果 manuscript 中没有 acknowledgment 节点
    if not has_ack:
        if refs_before_ack:
            new_sections.append(refs_sec)
            ack = ManuscriptSection(section_id="acknowledgment", title="致谢", markdown_body="")
            new_sections.append(ack)
        else:
            ack = ManuscriptSection(section_id="acknowledgment", title="致谢", markdown_body="")
            new_sections.append(ack)
            new_sections.append(refs_sec)

    merged = Manuscript(
        sections=new_sections,
        cover_text=manuscript.cover_text,
        toc_text=manuscript.toc_text,
        version=manuscript.version + 1,
        thesis_title=manuscript.thesis_title,
        keywords_zh_text=manuscript.keywords_zh_text,
        keywords_en_text=manuscript.keywords_en_text,
        tech_spec=manuscript.tech_spec,
    )
    return _finalize_manuscript_postprocess(merged, plan)


# ── 顽固问题专项修复 ─────────────────────────────────────────

def stubborn_targeted_fix(
    manuscript: Manuscript,
    plan: WritingPlan,
    store: ReferenceStore,
    stubborn_items: list[str],
) -> tuple[Manuscript, dict[str, str]]:
    """
    顽固问题专项修复 — 三层分流：
      删除类 → 代码直接删段落（零 LLM 调用）
      重写类 → LLM 精炼 prompt（注入章节大纲/scope/要点约束）
      修正/其余 → 跳过（postprocess/term_map 已处理）
    返回 (修正后的 Manuscript, term_map)。
    """
    if not stubborn_items:
        return manuscript, {}

    section_map = {s.section_id: s for s in plan.outline}
    new_sections = {s.section_id: s.markdown_body for s in manuscript.sections}
    tech_spec_text = ""
    if _is_thesis_mode() and manuscript.tech_spec:
        tech_spec_text = format_tech_spec_for_prompt(manuscript.tech_spec)

    _REWRITE_SYSTEM = (
        "你是论文修订专家。严格根据指令修改正文，仅处理明确指出的问题。\n"
        "输出精确 JSON：{\"anchor\": \"需替换段落的原文首句（15-30字，与原文一字不差）\", "
        "\"replacement\": \"修改后的完整段落\"}\n"
        "严禁输出整章正文——只输出一个段落。anchor 必须与原文完全相同，方便程序搜索替换。"
    )
    _REWRITE_SYSTEM_SECTION = (
        "你是论文修订专家。严格根据指令修改正文，仅处理明确指出的问题。\n"
        "输出完整修订后的章节正文（Markdown，不含 ## 章节标题），替换整章。\n"
        "输出 JSON：{\"section_body\": \"修订后的完整章节正文\"}"
    )
    modified_any = False
    delete_count = 0
    rewrite_count = 0
    rewrite_section_count = 0
    skipped_resolved = 0

    _issue_feature_words = _re.compile(
        r"(?:重复|截断|不完整|缺失|不一致|错误|越界|冲突|矛盾|"
        r"标点缺失|标点错误|引用.*错误|格式.*错误)"
    )

    for item in stubborn_items:
        nums = set(_re.findall(r"第(\d+)章", item))
        if not nums:
            logger.warning("顽固项无明确章节号，跳过: %s", item[:80])
            continue
        target_ids = [f"s{n}" for n in nums]
        category = _stubborn_classify(item)

        for sid in target_ids:
            if sid not in new_sections:
                continue
            body = new_sections[sid]
            title = getattr(section_map.get(sid), 'title', sid)
            node = section_map.get(sid)

            if len(body) < 50:
                logger.info("顽固项 [%s] 对应章节过短（%d字），可能已被清空/重写，跳过", sid, len(body))
                skipped_resolved += 1
                continue

            if category == "delete":
                new_body = _stubborn_hard_delete(body, item, sid)
                if new_body != body:
                    new_sections[sid] = new_body
                    modified_any = True
                    delete_count += 1
                    logger.info("顽固专项 [%s] 代码删除完成", sid)
                continue

            if category == "rewrite":
                prompt_parts = _stubborn_rewrite_prompt(
                    item=item, body=body, title=title, node=node,
                    tech_spec_text=tech_spec_text,
                )
                try:
                    response = chat_json(
                        build_messages(_REWRITE_SYSTEM, "\n".join(prompt_parts)),
                        temperature=0.2, max_tokens=2000,
                    )
                except Exception as e:
                    logger.warning("顽固专项 [%s] LLM 失败: %s", sid, e)
                    continue

                anchor, new_paragraph = _parse_anchor_replace_json(response)
                replaced = body
                if anchor and new_paragraph:
                    replaced = _replace_paragraph_by_anchor(body, anchor, new_paragraph, sid)
                    if replaced != body:
                        new_sections[sid] = replaced
                        modified_any = True
                        rewrite_count += 1
                        logger.info("顽固专项 [%s] 段落级 LLM 重写完成", sid)
                        continue

                # 段落级失败 → 升级到节级替换
                logger.info(
                    "顽固专项 [%s] 段落级失败（anchor=%s），升级节级重写",
                    sid, (anchor or "无")[:30]
                )
                section_prompt_parts = _stubborn_rewrite_prompt(
                    item=item, body=body, title=title, node=node,
                    tech_spec_text=tech_spec_text,
                )
                section_prompt_parts.append(
                    "【任务】输出完整修订后的章节正文（JSON）。"
                    "不要只改一个段落——重写整章，确保修改后的内容与前后章连贯。"
                )
                try:
                    sec_response = chat_json(
                        build_messages(_REWRITE_SYSTEM_SECTION, "\n".join(section_prompt_parts)),
                        temperature=0.2, max_tokens=8000,
                    )
                except Exception as e:
                    logger.warning("顽固专项 [%s] 节级 LLM 失败: %s", sid, e)
                    continue

                if isinstance(sec_response, dict) and sec_response.get("section_body"):
                    new_body = str(sec_response["section_body"]).strip()
                    if len(new_body) > 100:
                        new_sections[sid] = new_body
                        modified_any = True
                        rewrite_section_count += 1
                        logger.info("顽固专项 [%s] 节级 LLM 重写完成（%d字）", sid, len(new_body))
                    else:
                        logger.warning("顽固专项 [%s] 节级输出过短，跳过", sid)
                else:
                    logger.warning("顽固专项 [%s] 节级响应无 section_body", sid)
                continue

    if not modified_any:
        if skipped_resolved > 0:
            logger.info("顽固专项：%d 项已被正常修订解决，跳过", skipped_resolved)
        return manuscript, {}

    logger.info(
        "顽固专项修复：删除 %d 项，段落重写 %d 项，节级重写 %d 项，跳过已修 %d 项",
        delete_count, rewrite_count, rewrite_section_count, skipped_resolved,
    )

    sections_out = [
        ManuscriptSection(
            section_id=s.section_id, title=s.title,
            markdown_body=new_sections.get(s.section_id, s.markdown_body),
        )
        for s in manuscript.sections
    ]
    merged = Manuscript(
        sections=sections_out, cover_text=manuscript.cover_text,
        toc_text=manuscript.toc_text, version=manuscript.version,
        thesis_title=manuscript.thesis_title,
        keywords_zh_text=manuscript.keywords_zh_text,
        keywords_en_text=manuscript.keywords_en_text,
        tech_spec=manuscript.tech_spec,
    )

    # 顽固修复后结构校验
    scfg = scope_validation_settings()
    validation_ok = True
    for sec in merged.sections:
        sec_body = sec.markdown_body
        sec_body = _clean_section_overflow(sec_body, sec.section_id)
        node = section_map.get(sec.section_id)
        if node and scfg.get("enabled", True) and section_has_scope_constraints(node):
            ok_sc, issues_sc = validate_section_body_scope(sec_body, node)
            if not ok_sc:
                validation_ok = False
                logger.warning(
                    "顽固专项 [%s] scope 校验失败: %s", sec.section_id, "; ".join(issues_sc[:3])
                )
        if sec_body != sec.markdown_body:
            sec.markdown_body = sec_body

    if not validation_ok:
        logger.warning("顽固专项修复后 scope 校验未通过，回滚本轮修改")
        return manuscript, {}

    return _finalize_manuscript_postprocess(merged, plan)


# ── 顽固项分类与处理 helper ──────────────────────────────────

def _stubborn_classify(item: str) -> str:
    """分类：'delete' | 'rewrite' | 'skip'"""
    DELETE_PATTERNS = [
        "不应出现", "禁止出现", "禁止写", "禁止含", "不得出现", "不得写",
        "禁止", "删除", "移除", "去掉", "删去", "去除",
        "越界", "混入", "不应包含", "不属于本章", "移除到",
    ]
    REWRITE_PATTERNS = [
        "补充", "修改", "应该", "改为", "调整", "完善",
        "不足", "缺少", "缺失", "错误", "纠正", "修正",
        "统一", "不一致", "改为", "修正为",
    ]
    SKIP_PATTERNS = ["参考文献格式", "个人感悟", "心得体会"]

    for kw in SKIP_PATTERNS:
        if kw in item:
            return "skip"
    for kw in DELETE_PATTERNS:
        if kw in item:
            return "delete"
    for kw in REWRITE_PATTERNS:
        if kw in item:
            return "rewrite"
    return "rewrite"


def _stubborn_hard_delete(body: str, item: str, section_id: str) -> str:
    """代码级删除：从 item 提取禁止关键词 → 搜 body → 删包含行所在段落。"""
    quotes = _re.findall(r"[「\"\'《]([^」\"\'》]{2,20})[」\"\'》]", item)
    keywords = [q.strip() for q in quotes if len(q.strip()) >= 2]

    if not keywords:
        words = ["硬件设计", "代码实现", "实验数据", "测试结果", "原理图", "PCB",
                 "学习感悟", "自我检讨", "感谢导师", "感谢老师", "心得体会", "收获很大"]
        for w in words:
            if w in item:
                keywords.append(w)
                break

    if not keywords:
        return body

    lines = body.split("\n")
    delete_mask = [False] * len(lines)
    for i, line in enumerate(lines):
        for kw in keywords:
            if kw in line:
                delete_mask[i] = True
                if i > 0 and lines[i - 1].strip():
                    delete_mask[i - 1] = True
                if i + 1 < len(lines) and lines[i + 1].strip():
                    delete_mask[i + 1] = True
                break

    kept = [l for i, l in enumerate(lines) if not delete_mask[i]]

    out: list[str] = []
    blank_count = 0
    for l in kept:
        if not l.strip():
            blank_count += 1
            if blank_count <= 2:
                out.append(l)
        else:
            blank_count = 0
            out.append(l)

    new_body = "\n".join(out).strip()
    if len(new_body) < len(body) * 0.5:
        logger.warning("顽固删除 [%s] 删掉了超过一半内容，取消", section_id)
        return body
    return new_body


def _stubborn_rewrite_prompt(
    item: str, body: str, title: str, node, tech_spec_text: str,
) -> list[str]:
    """构建重写类 prompt：注入章节约束。"""
    parts: list[str] = []

    if tech_spec_text:
        parts.append(tech_spec_text)

    parts.append(f"当前章节：{title}")

    if node:
        if node.bullets:
            parts.append(f"章节要点：{'；'.join(node.bullets[:6])}")
        if node.outline_detail:
            parts.append(f"写作约束：{node.outline_detail}")
        if node.scope_must_include:
            parts.append(f"必须包含：{'；'.join(node.scope_must_include)}")
        if node.scope_forbidden:
            parts.append(f"禁止出现：{'；'.join(node.scope_forbidden)}")

    exe_block = _executable_outline_prompt_section(node) if node else ""
    if exe_block:
        parts.append(exe_block)

    special_rule = _get_section_rule(node.section_id) if node else ""
    if special_rule:
        parts.append(f"\n{special_rule}")

    parts.append(f"\n【必须修复的问题】\n{item}")
    parts.append(f"\n当前正文：\n{body}")
    parts.append(
        "\n【任务】在正文中找到需要修改的段落，"
        "只输出该段落的 anchor 和 replacement，不要输出整章。"
    )
    return parts


def _parse_anchor_replace_json(response: dict | str) -> tuple[str | None, str | None]:
    """解析 LLM 输出的 JSON 格式「定位锚 + 修改后段落」。"""
    if isinstance(response, dict):
        d = response
    elif isinstance(response, str):
        import json
        try:
            d = json.loads(response)
        except json.JSONDecodeError:
            return _parse_anchor_replace(response)
    else:
        return None, None

    anchor = (d.get("anchor") or "").strip().strip('"').strip("'").strip("`")
    replacement = (d.get("replacement") or "").strip()
    if len(anchor) < 5 or len(replacement) < 10:
        return None, None
    return anchor, replacement


def _parse_anchor_replace(response: str) -> tuple[str | None, str | None]:
    """兜底：解析自由文本格式的「定位锚 + 修改后段落」。"""
    m_anchor = _re.search(r"定位锚[：:]\s*(.+?)(?:\n|$)", response)
    if not m_anchor:
        m_anchor = _re.search(r"Anchor[：:]\s*(.+?)(?:\n|$)", response)
    if not m_anchor:
        return None, None

    anchor = m_anchor.group(1).strip().strip('"').strip("'").strip("`")
    if len(anchor) < 5:
        return None, None

    after_anchor = response[m_anchor.end():]
    m_para = _re.search(r"修改后段落[：:]\s*\n?(.*)", after_anchor, _re.DOTALL)
    if not m_para:
        m_para = _re.search(r"Replacement[：:]\s*\n?(.*)", after_anchor, _re.DOTALL)
    if not m_para:
        return None, None

    new_paragraph = m_para.group(1).strip()
    if len(new_paragraph) < 10:
        return None, None

    return anchor, new_paragraph


def _replace_paragraph_by_anchor(
    body: str, anchor: str, new_paragraph: str, section_id: str
) -> str:
    """在 body 中搜索 anchor，找到其所在段落并替换为新段落。"""
    idx = body.find(anchor)
    if idx < 0:
        if len(anchor) > 16:
            core = anchor[len(anchor)//2 - 8 : len(anchor)//2 + 8]
            idx = body.find(core)
            if idx >= 0:
                idx = idx - (len(anchor)//2 - 8)

    if idx < 0:
        logger.warning(
            "顽固专项 [%s] anchor 匹配失败，跳过改写（宁可漏修也不破坏结构）"
            " anchor 前30字: %s", section_id, anchor[:30]
        )
        return body

    para_start = body.rfind("\n\n", 0, idx)
    para_start = 0 if para_start < 0 else para_start + 2

    para_end = body.find("\n\n", idx + len(anchor))
    if para_end < 0:
        para_end = len(body)

    line_start = body.rfind("\n", 0, idx)
    if line_start > para_start:
        para_start = line_start + 1

    return body[:para_start] + new_paragraph.strip() + body[para_end:]


# ── 修订自检 ─────────────────────────────────────────────────

def check_revision_compliance(
    manuscript: Manuscript,
    plan: WritingPlan,
    actionable_items: list,
) -> tuple[list, list]:
    """修订后自查：用极轻量 LLM 调用逐条检查 actionable_items 是否已被实际修正。"""
    if len(actionable_items) < 3:
        return list(actionable_items), []

    section_summaries = []
    for sec in manuscript.sections:
        body = sec.markdown_body
        if len(body) > 600:
            snippet = body[:400] + "\n...(中间省略)...\n" + body[-100:]
        else:
            snippet = body
        section_summaries.append(
            f"### [{sec.section_id}] {sec.title}\n{snippet}"
        )

    items_text = "\n".join(f"{i}. {item}" for i, item in enumerate(actionable_items))
    user_prompt = (
        f"## 论文各章节概要\n{' '.join(section_summaries)[:3000]}\n\n"
        f"## 修订建议清单\n{items_text}\n\n"
        "## 任务\n"
        "逐条检查以上修订建议是否已被实际修正。注意：只检查内容是否真的改了，不要猜测。\n"
        '输出 JSON：{"results": [{"index": 0, "fixed": true}, {"index": 1, "fixed": false}]}\n'
        "index 对应上方的编号，fixed=true 表示已修正，false 表示仍未修正。"
    )

    system = "你是论文审阅助手。逐条检查修订建议是否已被落实到正文。只输出 JSON。"
    messages = build_messages(system, user_prompt)

    try:
        raw = chat_json(messages, temperature=0.1, max_tokens=500)
    except Exception as e:
        logger.warning("修订自检 LLM 调用失败: %s，跳过", e)
        return list(actionable_items), []

    if not raw:
        logger.warning("修订自检 LLM 未返回有效 JSON，跳过自检")
        return list(actionable_items), []

    results = raw.get("results", [])
    if not isinstance(results, list):
        return list(actionable_items), []

    fixed = []
    unfixed = []
    covered: set[int] = set()
    for r in results:
        if not isinstance(r, dict):
            continue
        idx = r.get("index")
        if not isinstance(idx, int) or idx < 0 or idx >= len(actionable_items):
            continue
        covered.add(idx)
        if r.get("fixed", True):
            fixed.append(actionable_items[idx])
        else:
            unfixed.append(actionable_items[idx])

    for i, item in enumerate(actionable_items):
        if i not in covered:
            fixed.append(item)

    if unfixed:
        logger.info("修订自检：%d/%d 未修正", len(unfixed), len(actionable_items))
    else:
        logger.info("修订自检：%d 条全部通过", len(actionable_items))

    return fixed, unfixed
