"""
摘要生成 — 基于已完成的正文章节生成中英文摘要。

包含摘要专用 System Prompt、正文摘录拼接、多候选择优、校验重试。
依赖 helpers.py（_EXECUTION_PROTOCOL），无其他 writing 包内循环依赖。
"""
import logging
from typing import List, Optional

from ..config import get
from ..llm import chat, build_messages
from ..models import ManuscriptSection, SectionNode, WritingPlan
from .helpers import _EXECUTION_PROTOCOL
from .multi_candidate import (
    llm_pick_best_candidate,
    multi_candidate_settings,
    rule_prefilter_candidates,
    section_uses_multi_candidate,
)
from .scope_enforce import scope_validation_settings, validate_abstract_against_tech_spec

logger = logging.getLogger(__name__)

# ── Prompt 常量 ───────────────────────────────────────────────

_SYSTEM_ABSTRACT_FROM_BODY = """# Role
你是一位评审过300+本科学位论文的审阅专家，同时是学术写作规范教材的执笔人。
你对摘要的四要素完整性、语言凝练度、信息密度有近乎苛刻的要求。

# Task
根据下方提供的论文正文素材，撰写符合规范的500—800字中文摘要。

# Constraints
1. 字数：500—800字，不得少于500字
2. 四要素（按顺序）：目的（研究背景与要解决的问题）→ 方法（技术路线、设计方案）→ 结果（实现的功能与指标）→ 结论（研究的价值和意义）
3. 绝对禁止：[1]等引用标记、图表引用（图X-X/表X-X）、关键词行、论文题目重复
4. 全程第三人称，被动语态为主
5. 直接输出摘要正文，不加"摘要"标题
6. 内容必须与正文一致，不得出现正文未提及的技术术语或数据
7. 去 AI 味：拒绝"首先…其次…然后…最后"等机械套话，用自然语义过渡

# Writing Tips
- 每个要素用独立段落展开，避免四要素挤在同一段
- 术语与 TechSpec（技术规范文档）严格一致
""" + _EXECUTION_PROTOCOL

_SYSTEM_EN_ABSTRACT_FROM_ZH = """# Role
You are a strict academic translator (Chinese → English). Your only job is to produce
an English rendering that preserves every fact, structure, technical term, and nuance
of the Chinese source abstract. You are NOT an editor or a polisher.

# Task
Translate the provided Chinese abstract into English sentence by sentence.

# Constraints
1. DO NOT summarize, compress, reorganize, or embellish — preserve ALL information
2. Each Chinese sentence must correspond to one English sentence (one-to-one mapping)
3. Technical terms must match the Chinese source exactly (do not expand acronyms —
   keep "DHT11" as "DHT11", "STM32" as "STM32")
4. Terminology: use "this thesis" (not "paper" or "article")
5. Voice: passive preferred; no "I", "we", "our"
6. Tense: past for methods/results; present for conclusions
7. No citation markers, no figure/table references, no "Keywords:" line

# Strictly Forbidden
- Shortening a 5-paragraph abstract into 2 paragraphs
- Deleting technical details to "be more concise"
- Reordering the four elements (Objective → Methods → Results → Conclusion)
- Adding commentary or evaluation not present in the Chinese source
- Rephrasing sentences that are already clear — translate them as-is

# Output
Output ONLY the English abstract body text. No "Abstract" heading, no commentary.
""" + _EXECUTION_PROTOCOL


# ── 正文摘录 ─────────────────────────────────────────────────

def _excerpt_for_abstract(body: str, head_chars: int = 600, tail_chars: int = 450) -> str:
    """为摘要生成拼接正文素材：长章截取「首段 + 尾段」。"""
    b = body.strip()
    if not b:
        return ""
    if len(b) <= head_chars + tail_chars + 40:
        return b.replace("\n", " ")
    return (
        b[:head_chars].replace("\n", " ")
        + " …（章节中段省略）… "
        + b[-tail_chars:].replace("\n", " ")
    )


# ── 摘要清理 ─────────────────────────────────────────────────

def _strip_keywords_from_abstract(text: str) -> str:
    """清理摘要中不允许出现的内容：关键词行、图表引用、文献引用标记。"""
    import re as _re

    # 移除关键词行
    lines = text.strip().splitlines()
    clean = []
    for line in lines:
        stripped = line.strip()
        if _re.match(r"^(关键词|keywords?)\s*[：:：]", stripped, _re.I):
            continue
        clean.append(line)
    text = "\n".join(clean).strip()

    text = _re.sub(r"[，。；\s]*关键词[：:：][^\n]+", "", text)
    text = _re.sub(r"[，。；\s]*[Kk]eywords?\s*[：:：][^\n]+", "", text)

    # 移除图表引用
    text = _re.sub(
        r"(?:如图|见图|由图)\s*\d+[-‐]\d+\s*(?:所示|中|可知|标明)[，,。、]?",
        "本研究所述方案",
        text,
    )
    text = _re.sub(r"图\s*\d+[-‐]\d+\s*(?:所示|中|可知|标明)[，,。、]?", "该图", text)
    text = _re.sub(
        r"(?:见表|如表)\s*\d+[-‐]\d+\s*(?:所示|中|所列)?[，,。、]?",
        "相关数据",
        text,
    )
    text = _re.sub(r"表\s*\d+[-‐]\d+\s*(?:所示|中|所列)?[，,。、]?", "相关数据", text)
    text = _re.sub(
        r"如式\s*[（(]\s*\d+[-‐]\d+\s*[）)]\s*所示[，,。、]?",
        "如公式所示",
        text,
    )

    # 移除引用标记 [数字]
    text = _re.sub(r"\[\d+(?:[,，]\d+)*\]", "", text)

    # 清理多余空格
    text = _re.sub(r"  +", " ", text)
    text = _re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


# ── 摘要校验与重试 ───────────────────────────────────────────

def _abstract_zh_validate_and_retry(
    zh_body: str,
    zh_prompt: str,
    tech_spec: dict | None,
) -> str:
    """中文摘要：TechSpec/规范硬校验，失败则追加说明再生成一次。"""
    st = scope_validation_settings()
    if not st.get("enabled", True):
        return zh_body
    ok, issues = validate_abstract_against_tech_spec(zh_body, tech_spec)
    if ok:
        return zh_body
    if not st.get("retry_once", True):
        logger.warning("摘要校验未通过（不重试）：%s", issues)
        return zh_body
    logger.warning("摘要校验未通过，将重试一次：%s", issues)
    fix = (
        "\n\n## 系统校验反馈（须严格遵守并重写摘要）\n"
        + "\n".join(issues)
        + "\n请输出完整中文摘要正文（500-800字，四要素齐全）。\n"
    )
    try:
        raw = chat(
            build_messages(_SYSTEM_ABSTRACT_FROM_BODY, zh_prompt + fix),
            temperature=0.35,
            max_tokens=1200,
        )
        zh2 = _strip_keywords_from_abstract(raw)
        if zh2.strip():
            ok2, issues2 = validate_abstract_against_tech_spec(zh2, tech_spec)
            if ok2:
                logger.info("摘要校验重试后通过")
            else:
                logger.warning("摘要校验重试后仍有问题：%s", issues2)
            return zh2
    except Exception as e:
        logger.warning("摘要校验重试失败: %s", e)
    return zh_body


# ── 主入口 ───────────────────────────────────────────────────

def _generate_abstract_from_body(
    plan: WritingPlan,
    body_sections: List[ManuscriptSection],
    thesis_mode: bool,
    tech_spec_text: str = "",
    tech_spec: dict | None = None,
) -> tuple[ManuscriptSection, ManuscriptSection]:
    """
    基于已生成的正文章节生成中英文摘要。
    返回：(abstract_zh_section, abstract_en_section)
    """
    body_map = {s.section_id: s for s in body_sections}
    numeric_body_ids = sorted(
        (sid for sid in body_map if sid.startswith("s") and sid[1:].isdigit()),
        key=lambda x: int(x[1:]),
    )
    body_excerpt = []
    for sid in numeric_body_ids:
        sec = body_map[sid]
        excerpt = _excerpt_for_abstract(sec.markdown_body)
        body_excerpt.append(f"[{sec.title}] {excerpt}")

    body_summary = "\n".join(body_excerpt)
    kw_sep = "；" if thesis_mode else "、"
    kw_text = kw_sep.join(plan.keywords[:5]) if plan.keywords else ""

    # ── 生成中文摘要
    spec_block = f"{tech_spec_text}\n" if tech_spec_text else ""
    zh_prompt = (
        f"{spec_block}"
        f"## 论文关键词\n{kw_text}\n\n"
        f"## 论文正文摘要（各章节节选：首尾拼接）\n{body_summary}\n\n"
        "## 任务\n请根据以上内容撰写500-800字的中文摘要（四要素：目的/方法/结果/结论）。\n"
        "⚠ 摘要中传感器类型、通信协议、软件架构等必须与上方技术规范文档完全一致。"
    )
    abstract_node = next(
        (s for s in plan.outline if s.section_id == "abstract_zh"), None
    )
    node_bullets = "、".join(abstract_node.bullets) if abstract_node else ""
    if node_bullets:
        zh_prompt += f"\n重点覆盖：{node_bullets}"

    zh_failed = False
    mc_ab = multi_candidate_settings()
    use_multi_zh = section_uses_multi_candidate("abstract_zh", thesis_mode, mc_ab)

    if use_multi_zh:
        logger.info("中文摘要启用多候选初稿（N=%d）", mc_ab["n"])
        zh_bodies: List[str] = []
        jitter = float(mc_ab["temperature_jitter"])
        for k in range(int(mc_ab["n"])):
            t = min(0.95, max(0.1, 0.4 + k * jitter))
            try:
                raw = chat(
                    build_messages(_SYSTEM_ABSTRACT_FROM_BODY, zh_prompt),
                    temperature=t, max_tokens=1500,
                )
                piece = _strip_keywords_from_abstract(raw)
                if piece.strip():
                    zh_bodies.append(piece.strip())
            except Exception as e:
                logger.warning("中文摘要多候选 #%d 失败: %s", k, e)
        if mc_ab.get("rule_prefilter", True):
            filtered = rule_prefilter_candidates(zh_bodies, "abstract_zh", 600)
            if filtered:
                zh_bodies = filtered
            elif zh_bodies:
                logger.warning("中文摘要多候选规则筛空，回退未筛版本")
        if len(zh_bodies) >= 2:
            if abstract_node:
                best_i, sc = llm_pick_best_candidate(
                    zh_bodies, abstract_node, plan, tech_spec_text,
                    float(mc_ab["llm_score_threshold"]),
                )
                if sc:
                    logger.info("中文摘要多候选 LLM 分数: %s", sc)
                zh_body = zh_bodies[best_i]
            else:
                logger.warning("大纲缺少 abstract_zh 节点，多候选摘要去掉 LLM 择优，保留首条")
                zh_body = zh_bodies[0]
        elif len(zh_bodies) == 1:
            zh_body = zh_bodies[0]
        else:
            zh_failed = True
            zh_body = "（中文摘要生成失败，请手动撰写500-800字摘要，包含目的/方法/结果/结论四要素）"
    else:
        try:
            zh_body = chat(
                build_messages(_SYSTEM_ABSTRACT_FROM_BODY, zh_prompt),
                temperature=0.4, max_tokens=1500,
            )
            zh_body = _strip_keywords_from_abstract(zh_body)
            if not zh_body.strip():
                raise ValueError("摘要生成返回空")
        except Exception as e:
            logger.warning("中文摘要生成失败: %s，使用占位", e)
            zh_failed = True
            zh_body = "（中文摘要生成失败，请手动撰写500-800字摘要，包含目的/方法/结果/结论四要素）"

    if not zh_failed and not zh_body.strip().startswith("（中文摘要生成失败"):
        zh_body = _abstract_zh_validate_and_retry(zh_body, zh_prompt, tech_spec)

    # ── 生成英文摘要
    if zh_failed or zh_body.strip().startswith("（中文摘要生成失败"):
        en_body = (
            "Chinese abstract was not generated successfully. Please write a 200–400 word "
            "English abstract aligned with the thesis body (Objective, Methods, "
            "Results, Conclusion), with no citations or figure references."
        )
    else:
        en_prompt = (
            f"Please translate the following Chinese abstract into an English abstract.\n\n"
            f"Chinese abstract:\n{zh_body}"
        )
        try:
            en_body = chat(
                build_messages(_SYSTEM_EN_ABSTRACT_FROM_ZH, en_prompt),
                temperature=0.3, max_tokens=1500,
            )
            en_body = _strip_keywords_from_abstract(en_body)
            if not en_body.strip():
                raise ValueError("英文摘要生成返回空")
        except Exception as e:
            logger.warning("英文摘要生成失败: %s，使用占位", e)
            en_body = (
                "(Abstract translation failed. Please manually write a 200-400 word English "
                "abstract.)"
            )

    zh_section = ManuscriptSection(
        section_id="abstract_zh", title="摘要", markdown_body=zh_body
    )
    en_section = ManuscriptSection(
        section_id="abstract_en", title="Abstract", markdown_body=en_body
    )
    logger.info("摘要生成完成：中文约%d字，英文约%d字", len(zh_body), len(en_body))
    return zh_section, en_section
