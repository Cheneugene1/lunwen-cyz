"""
意图理解与规划模块
输入：DocumentBundle、对话摘要、用户自然语言、ReferenceStore 统计
输出：WritingPlan（含大纲、关键词、检索词、优先文献 id）
大纲节点 SectionNode 可含可执行字段（outline_detail、scope_*、subsections），供撰写阶段注入边界约束。

毕业论文模式（thesis_mode=true）：
  强制生成标准六章结构 + 摘要 + 致谢，关键词 3-5 个用"；"分隔
"""

import json
import logging
import re
from typing import Optional

from .config import get
from .llm import chat_json, build_messages
from .models import DocumentBundle, ThesisConfig, WritingPlan, SectionNode
from .ref_store import ReferenceStore

logger = logging.getLogger(__name__)

# ── 普通模式 Prompt ────────────────────────────────────────────
_SYSTEM_PROMPT = """你是一位严谨的学术写作规划专家。
根据用户提供的文档摘要、已有参考文献信息和需求描述，生成一份结构完整的学术论文写作规划。
输出必须是合法 JSON，结构如下：
{
  "outline": [
    {
      "section_id": "s1",
      "title": "章节标题",
      "bullets": ["要点1", "要点2"],
      "outline_detail": "可选：本章节段落展开顺序与须遵守的写作约束（一两句话）",
      "scope_must_include": ["可选：必须出现的术语或子主题"],
      "scope_forbidden": ["可选：本章禁止写的内容"],
      "subsections": []
    }
  ],
  "keywords": ["关键词1", "关键词2"],
  "search_queries": ["英文检索词1", "中文检索词2"],
  "manual_ref_ids": []
}
- outline 中至少包含：摘要、引言、研究背景/相关工作、方法/设计、实验/分析、结论，共 6 个以上节点
- keywords 为该论文核心技术关键词，5–10 个
- search_queries 为用于学术数据库检索的查询词，4–10 条（英文为主，可含中文意图）
- manual_ref_ids 可为空列表
- outline 每一项可选用 outline_detail / scope_must_include / scope_forbidden / subsections：
  outline_detail 把本章写到「可执行」颗粒度；scope_forbidden 显式禁止越界（如方法章禁止写实验数据）；
  subsections 为树状子节点，每项含 section_id、title、bullets，用于正文 ### 小标题对齐，勿与顶层重复。
"""

# ── 毕业论文模式 Prompt ───────────────────────────────────────
_SYSTEM_THESIS = """你是一位熟悉中国高校本科毕业论文规范的写作规划专家。
请根据用户的研究主题和文档内容，生成一份符合毕业论文要求的写作规划。

毕业论文固定结构（必须包含以下所有节点，section_id 不得更改）：
- abstract_zh     摘要（中文，500-800字）
- abstract_en     Abstract（英文，与中文对应）
- s1              第1章 引言（约2000字）
- s2              第2章（自拟标题，约3000字）
- s3              第3章（自拟标题，约4000字）
- s4              第4章（自拟标题，约4000字）
- s5              第5章（自拟标题，约3000字）
- s6              第6章 结论（约1000字，禁止写学习感悟、自我检讨）
- acknowledgment  致谢（约200字）

输出必须是合法 JSON：
{
  "title": "中文论文题目（20字以内，不超过25字）",
  "title_en": "English Title of the Thesis",
  "outline": [
    { "section_id": "abstract_zh", "title": "摘要", "bullets": ["研究目的", "研究方法", "主要结果", "结论"] },
    { "section_id": "abstract_en", "title": "Abstract", "bullets": [] },
    { "section_id": "s1", "title": "第1章 引言", "bullets": ["研究背景与意义", "国内外研究现状概览", "本文工作与贡献", "论文结构安排"],
      "outline_detail": "根据课题特点选择引言类型——饱满型：涵盖研究现状分析、技术路线概述，**则 s2 改为系统总体设计**（需求分析+整体方案+器件选型），不再写单独的技术综述章；简要型：仅说明目的、意义和拟用方法，**则 s2 需全面展开现存工作分析**。选定后在 outline_detail 中明确说明。先背景与意义，再问题与贡献，最后说明全文章节安排；不写系统设计与实验细节。",
      "scope_must_include": [],
      "scope_forbidden": ["第3章及以后的系统详细设计", "实验数据与代码实现"],
      "subsections": [] },
    { "section_id": "s2", "title": "第2章（自拟标题：饱满型引言→「系统总体设计」；简要型引言→「现存工作分析/相关技术综述」。须用「第2章」开头）",
      "bullets": ["需求分析/现有方法分析", "整体方案设计/代表方法对比", "器件选型/关键技术支持", "本研究的改进方向"] },
    { "section_id": "s3", "title": "第3章（自拟标题。嵌入式/硬件类→「系统硬件设计」；纯软件类→「系统设计」。须用「第3章」开头）",
      "bullets": ["硬件总体架构/系统架构", "各模块电路设计/核心算法设计", "关键技术实现细节"],
      "outline_detail": "嵌入式项目：按模块逐一说明电路设计（主控、传感器、通信、显示等），附原理图；纯软件项目：先总体架构与模块划分，再关键技术选型。不写完整源码与第5章测试结论。",
      "scope_must_include": [],
      "scope_forbidden": ["完整源代码", "详细实验数据与测试结论", "第4章/第5章的实现细节"],
      "subsections": [
        { "section_id": "s3_1", "title": "3.1 总体架构", "bullets": ["系统框图", "模块关系"] },
        { "section_id": "s3_2", "title": "3.2 核心模块", "bullets": ["功能划分", "接口/电路设计"] }
      ] },
    { "section_id": "s4", "title": "第4章（自拟标题。嵌入式→「系统软件设计」；纯软件→「系统实现」。须用「第4章」开头）",
      "bullets": ["软件开发平台/开发环境", "主程序流程", "各模块软件设计", "关键代码说明"] },
    { "section_id": "s5", "title": "第5章（自拟标题。「系统实现及调试」/「实验结果与分析」等。须用「第5章」开头）",
      "bullets": ["系统实现与集成", "功能模块测试", "性能分析与对比", "综合评价"] },
    { "section_id": "s6", "title": "第6章 结论", "bullets": ["主要工作总结", "创新贡献", "研究局限", "未来工作展望"] },
    { "section_id": "acknowledgment", "title": "致谢", "bullets": [] }
  ],
  "keywords": ["关键词1", "关键词2", "关键词3"],
  "keywords_en": ["English Keyword 1", "English Keyword 2", "English Keyword 3"],
  "search_queries": ["英文检索词1", "英文检索词2", "中文检索词"],
  "manual_ref_ids": []
}

重要规则：
- title: 中文题目，简洁准确，20字以内（不超过25字），不含标点；避免「的研究」「的应用」等冗余词
- title_en: 对应的英文题目，每个实词首字母大写
- keywords: 中文关键词，3-5个，**从用户研究主题中提炼独立的学术名词或名词性词组（每个关键词是一个概念，如"温湿度检测""土壤湿度检测""STM32"DHT11"），不含标点，用"；"分隔**。严禁将用户原始输入语句全文作为关键词；严禁使用"不要XXX"等否定/约束语气的短语作为关键词
- keywords_en: 与 keywords 一一对应的英文关键词（3-5个），学术术语，除全大写专有名词（如 PID、IoT、STM32、DHT11、ESP32）保持原样外，每个实词首字母大写其余小写（如 "Temperature and Humidity Detection" 而非全大写 "TEMPERATURE AND HUMIDITY DETECTION" 或全小写 "temperature and humidity detection"）；介词/连词小写（and、of、for、in 等）
- section_id 必须严格按上表，不能自创
- s2～s5 标题必须自拟：用「第X章」开头 + 反映课题实际内容的自拟文字（如「第2章 基于STM32的智能灌溉技术综述」），禁止用「第2章 相关工作」等泛称
- search_queries 英文为主，4-8 条，每条是独立的英文学术检索短语（如 "STM32 temperature humidity control""soil moisture detection system"），直接可用于 Google Scholar 等检索。严禁将用户的中文原始输入直接拼接 "survey""system design" 作为检索词；必须从研究主题中提炼独立的关键词组合
- bullets 中的内容要点要具体，结合用户研究主题和文档内容填写
- 对 s1～s6 建议填写 outline_detail 与 scope_forbidden，将「本章边界」说清楚，减少后文内容前置
- subsections 仅作小节结构提示（section_id 可用 s3_1 等形式），不得替代顶层节点；每节至少应有2个以上段落，不可仅含3-5行的单一段落成节
- **禁止**在顶层 outline 数组中独立列出 s3_1、s4_2 等子节 ID——它们必须通过 subsections 字段嵌套在父章节中"""


def _build_user_prompt(
    doc_summary: str,
    user_request: str,
    ref_store_summary: str,
    conversation_summary: str,
) -> str:
    parts = []
    if conversation_summary:
        parts.append(f"## 对话历史摘要\n{conversation_summary}")
    if doc_summary:
        parts.append(f"## 文档内容摘要\n{doc_summary[:3000]}")
    parts.append(f"## 用户需求\n{user_request}")
    parts.append(f"## 当前文献池\n{ref_store_summary}")
    parts.append("请根据以上信息生成写作规划（JSON 格式）。")
    return "\n\n".join(parts)


def _str_list(val) -> list[str]:
    if not isinstance(val, list):
        return []
    return [str(x).strip() for x in val if x is not None and str(x).strip()]


def _parse_outline_node(item: dict, fallback_id: str) -> SectionNode | None:
    if not isinstance(item, dict):
        return None
    section_id = str(item.get("section_id") or fallback_id).strip() or fallback_id
    title = str(item.get("title") or "章节").strip()
    bullets = _str_list(item.get("bullets"))
    outline_detail = str(item.get("outline_detail") or "").strip()
    scope_must_include = _str_list(item.get("scope_must_include"))
    scope_forbidden = _str_list(item.get("scope_forbidden"))
    subs_raw = item.get("subsections")
    subsections: list[SectionNode] = []
    if isinstance(subs_raw, list):
        for j, sub in enumerate(subs_raw):
            if isinstance(sub, dict):
                sn = _parse_outline_node(sub, f"{section_id}_{j+1}")
                if sn:
                    subsections.append(sn)
    return SectionNode(
        section_id=section_id,
        title=title,
        bullets=bullets,
        outline_detail=outline_detail,
        scope_must_include=scope_must_include,
        scope_forbidden=scope_forbidden,
        subsections=subsections,
    )


def _parse_plan(raw: dict, store: ReferenceStore) -> WritingPlan:
    """将 LLM 返回的字典解析为 WritingPlan"""
    outline_raw = raw.get("outline", [])
    outline = []
    if isinstance(outline_raw, list):
        for i, item in enumerate(outline_raw):
            node = _parse_outline_node(item, f"s{i+1}")
            if node:
                outline.append(node)

    keywords = [str(k) for k in raw.get("keywords", []) if k]
    keywords_en = [str(k) for k in raw.get("keywords_en", []) if k]
    search_queries = [str(q) for q in raw.get("search_queries", []) if q]

    # 尝试匹配 manual_ref_ids（LLM 可能返回标题，这里只取已知 id）
    raw_ids = raw.get("manual_ref_ids", [])
    manual_ref_ids = []
    for rid in raw_ids:
        ref = store.by_id(str(rid))
        if ref:
            manual_ref_ids.append(ref.id)

    # 提取题目（从引言章节标题或 raw 中的 title 字段）
    plan_title = raw.get("title", "")
    plan_title_en = raw.get("title_en", "")
    if not plan_title:
        # 尝试从 s1 章节 bullets 的第一行推断
        for node in outline:
            if node.section_id == "s1" and node.bullets:
                pass  # 无法从 bullets 推断，保留空
        # 从用户请求提取（超过60字则截断）
        pass

    return WritingPlan(
        title=plan_title,
        title_en=plan_title_en,
        outline=outline,
        keywords=keywords,
        keywords_en=keywords_en,
        search_queries=search_queries,
        manual_ref_ids=manual_ref_ids,
    )


def generate_plan(
    bundle: DocumentBundle,
    user_request: str,
    store: ReferenceStore,
    conversation_summary: str = "",
) -> WritingPlan:
    """
    调用 LLM 生成写作规划。
    毕业论文模式（thesis_mode=true）：使用专用 Prompt，强制 6 章结构。
    若 LLM 失败则返回最简降级规划（保证主干可跑通）。
    """
    thesis_mode = bool(get("thesis_mode", False))
    doc_text = bundle.full_text(max_chars=3000)

    user_prompt = _build_user_prompt(
        doc_summary=doc_text,
        user_request=user_request,
        ref_store_summary=store.summary(),
        conversation_summary=conversation_summary,
    )

    system_prompt = _SYSTEM_THESIS if thesis_mode else _SYSTEM_PROMPT
    messages = build_messages(system_prompt, user_prompt)

    try:
        raw = chat_json(messages, temperature=0.4)
    except Exception as e:
        logger.error("规划生成失败: %s，使用降级规划", e)
        raw = {}

    if not raw or "outline" not in raw:
        logger.warning("LLM 返回规划不完整，使用降级最简大纲")
        raw = _fallback_plan(user_request, thesis_mode=thesis_mode)

    plan = _parse_plan(raw, store)

    # 毕业论文模式：确保关键 section_id 存在
    if thesis_mode:
        plan = _ensure_thesis_sections(plan, user_request)

    if not plan.outline:
        plan = _default_plan(user_request)

    # 关键词容错：LLM 偶将多个关键词写入单个字符串（如"STM32；温湿度；DHT11"）
    # 拆分为独立关键词，避免后续硬规则误报
    import re as _kw_re
    if len(plan.keywords) <= 2:
        split_keywords: list[str] = []
        for kw in plan.keywords:
            parts = [s.strip() for s in _kw_re.split(r"[；;，,]", kw) if s.strip()]
            split_keywords.extend(parts)
        if len(split_keywords) > len(plan.keywords):
            plan.keywords = split_keywords
    if plan.keywords_en and len(plan.keywords_en) <= 2:
        split_en: list[str] = []
        for kw in plan.keywords_en:
            parts = [s.strip() for s in _kw_re.split(r"[;，,]", kw) if s.strip()]
            split_en.extend(parts)
        if len(split_en) > len(plan.keywords_en):
            plan.keywords_en = split_en

    # 限制关键词数量（3-5 个）
    if thesis_mode:
        kw_min = int(get("thesis_keywords_min", 3))
        kw_max = int(get("thesis_keywords_max", 5))
        if len(plan.keywords) > kw_max:
            plan.keywords = plan.keywords[:kw_max]
        elif len(plan.keywords) < kw_min and plan.keywords:
            pass  # 保留现有，不强制补充

    # 清理顶层子节节点 + 规范化章节标题
    _clean_top_level_subsections(plan)
    _normalize_outline_titles(plan)

    logger.info("规划生成完成：%d 章节，%d 条检索词（毕业论文模式：%s）",
                len(plan.outline), len(plan.search_queries), thesis_mode)
    return plan


def _fallback_plan(user_request: str, thesis_mode: bool = False) -> dict:
    """LLM 失败时的降级最简规划"""
    topic = user_request[:40] if user_request else "研究主题"
    if thesis_mode:
        return {
            "outline": [
                {"section_id": "abstract_zh", "title": "摘要",
                 "bullets": ["研究目的", "研究方法", "主要结果", "结论"]},
                {"section_id": "abstract_en", "title": "Abstract", "bullets": []},
                {"section_id": "s1", "title": "第1章 引言",
                 "bullets": ["研究背景与意义", "国内外研究现状", "本文工作与贡献", "论文结构安排"]},
                {"section_id": "s2", "title": "第2章 现存工作分析与技术综述",
                 "bullets": ["核心技术领域综述", "代表方法分析", "各自优缺点", "本研究的改进方向"]},
                {"section_id": "s3", "title": "第3章 系统总体设计",
                 "bullets": ["总体架构设计", "功能模块划分", "关键技术方案", "系统流程设计"]},
                {"section_id": "s4", "title": "第4章 系统详细实现",
                 "bullets": ["开发环境搭建", "核心功能实现", "关键代码说明", "系统测试方案"]},
                {"section_id": "s5", "title": "第5章 实验测试与结果分析",
                 "bullets": ["实验环境配置", "功能测试结果", "性能对比分析", "综合评价"]},
                {"section_id": "s6", "title": "第6章 结论",
                 "bullets": ["主要工作总结", "创新贡献", "研究局限", "未来工作展望"]},
                {"section_id": "acknowledgment", "title": "致谢", "bullets": []},
            ],
            "keywords": [topic],
            "search_queries": [topic, f"{topic} survey", f"{topic} system design"],
            "manual_ref_ids": [],
        }
    return {
        "outline": [
            {"section_id": "s1", "title": "摘要", "bullets": ["研究目标", "主要方法", "主要结论"]},
            {"section_id": "s2", "title": "引言", "bullets": ["研究背景", "研究问题", "论文贡献"]},
            {"section_id": "s3", "title": "相关工作", "bullets": ["现有方法综述", "本文与现有方法的区别"]},
            {"section_id": "s4", "title": "研究方法", "bullets": ["方法框架", "核心算法/设计", "技术细节"]},
            {"section_id": "s5", "title": "实验与分析", "bullets": ["实验设置", "结果对比", "消融实验"]},
            {"section_id": "s6", "title": "结论", "bullets": ["研究总结", "局限性", "未来工作"]},
        ],
        "keywords": [topic],
        "search_queries": [topic, f"{topic} survey", f"{topic} methods"],
        "manual_ref_ids": [],
    }


# 毕业论文标准章节定义（section_id → (title, bullets)）
_THESIS_REQUIRED_SECTIONS = [
    ("abstract_zh",    "摘要",
     ["研究目的", "研究方法", "主要结果", "结论"]),
    ("abstract_en",    "Abstract", []),
    ("s1",  "第1章 引言",
     ["研究背景与意义", "国内外研究现状", "本文工作与贡献", "论文结构安排"]),
    ("s2",  "第2章 现存工作分析与技术综述",
     ["核心技术领域综述", "代表方法分析", "各自优缺点"]),
    ("s3",  "第3章 系统总体设计",
     ["总体架构设计", "功能模块划分", "关键技术方案"]),
    ("s4",  "第4章 系统详细实现",
     ["开发环境搭建", "核心功能实现", "系统测试"]),
    ("s5",  "第5章 实验测试与结果分析",
     ["实验环境配置", "结果展示", "对比分析"]),
    ("s6",  "第6章 结论",
     ["主要工作总结", "创新贡献", "未来工作展望"]),
    ("acknowledgment", "致谢", []),
]


def _ensure_thesis_sections(plan: WritingPlan, user_request: str) -> WritingPlan:
    """
    确保毕业论文规划包含所有必要章节。
    若 LLM 生成的规划缺少某章节，补充默认值；
    若 section_id 不在规范列表中，映射到最近的规范 id。
    """
    existing_ids = {s.section_id for s in plan.outline}
    required_ids = [sid for sid, _, _ in _THESIS_REQUIRED_SECTIONS]

    # 按规范顺序重建
    new_outline = []
    plan_map = {s.section_id: s for s in plan.outline}

    for sid, default_title, default_bullets in _THESIS_REQUIRED_SECTIONS:
        if sid in plan_map:
            new_outline.append(plan_map[sid])
        else:
            new_outline.append(SectionNode(
                section_id=sid,
                title=default_title,
                bullets=default_bullets,
            ))

    plan.outline = new_outline
    return plan


def _default_plan(user_request: str) -> WritingPlan:
    """返回默认 WritingPlan 对象"""
    raw = _fallback_plan(user_request)
    sections = [
        SectionNode(
            section_id=s["section_id"],
            title=s["title"],
            bullets=s["bullets"],
        )
        for s in raw["outline"]
    ]
    return WritingPlan(
        outline=sections,
        keywords=raw["keywords"],
        search_queries=raw["search_queries"],
        manual_ref_ids=[],
    )


def outline_to_markdown(plan: WritingPlan) -> str:
    """将大纲格式化为 Markdown，供用户审阅"""
    thesis_mode = bool(get("thesis_mode", False))
    lines = ["# 论文大纲\n"]

    def _emit_sec(sec: SectionNode, depth: int) -> None:
        prefix = "##" + "#" * max(0, depth - 1)
        lines.append(f"{prefix} {sec.title} (`{sec.section_id}`)")
        if sec.outline_detail:
            lines.append(f"*可执行说明：{sec.outline_detail}*")
        if sec.scope_must_include:
            lines.append("**须包含**：" + "；".join(sec.scope_must_include))
        if sec.scope_forbidden:
            lines.append("**禁止**：" + "；".join(sec.scope_forbidden))
        for bullet in sec.bullets:
            lines.append(f"- {bullet}")
        for sub in sec.subsections:
            _emit_sec(sub, depth + 1)
        lines.append("")

    for sec in plan.outline:
        _emit_sec(sec, 1)

    lines.append("---")
    # 毕业论文：关键词用中文分号"；"分隔
    kw_sep = "；" if thesis_mode else "、"
    lines.append(f"**关键词**：{kw_sep.join(plan.keywords)}")
    lines.append(f"**检索词**：{', '.join(plan.search_queries)}")
    return "\n".join(lines)


_OUTLINE_PARSE_USER_SYSTEM_PROMPT = """你是一位学术写作规划专家。用户粘贴了一段自由格式的论文大纲（可能含英文章节名、编号、子节、内联注释等），请将其解析为标准 JSON。

你需要智能识别章节并映射到如下固定 section_id：

| 用户可能的写法 | section_id |
|--------------|-----------|
| 摘要 / Abstract / 摘要（中文）| abstract_zh |
| Abstract（英文）/ English Abstract | abstract_en |
| 第1章 / 1. / 1 Introduction / 引言 / 绪论 | s1 |
| 第2章 / 2. / 2 Literature Review / 相关工作 / 技术综述 / 系统总体设计 / 需求分析 | s2 |
| 第3章 / 3. / 3 Research Methodology / 系统设计 / 硬件设计 / 总体设计 | s3 |
| 第4章 / 4. / 4 Implementation / 系统实现 / 软件设计 / 详细设计 | s4 |
| 第5章 / 5. / 5 Experiment / 测试 / 调试 / 系统实现及调试 / 实验结果与分析 | s5 |
| 第6章 / 6. / 6 Conclusion / 结论 / 总结与展望 / 建议与展望 | s6 |
| 致谢 / Acknowledgment | acknowledgment |

解析规则：
1. 用户可能同时写了英文章节名和中文说明（如\"1 Introduction \n 1.1 课题研究背景与意义\"），应提取核心语义作为 title（中文优先）
2. 子节（1.1、2.1 等）内容收为 bullets；用户的内联注释（如"这里只简单引出，不要展开太多"）写入 outline_detail
3. 每个正文章节至少 2 个 bullets；无法从文本中提取时，根据章节类型合理推断
4. 若某标准章节在用户输入中完全未出现，仍应生成（保持结构完整），使用默认 title 和 bullets
5. 标题保留用户原意但规范化：s1 固定为"第1章 引言"，s2-s5 取用户自拟标题（补全"第X章"前缀若缺失），s6 固定为"第6章 结论"

输出必须是合法 JSON，格式如下：
{
  "outline": [
    {"section_id": "abstract_zh", "title": "摘要", "bullets": ["研究目的", "研究方法", "主要结果", "结论"]},
    {"section_id": "abstract_en", "title": "Abstract", "bullets": []},
    {"section_id": "s1", "title": "第1章 引言", "bullets": [...], "outline_detail": "..."},
    {"section_id": "s2", "title": "第2章 ...", "bullets": [...], "outline_detail": "..."},
    ...
    {"section_id": "acknowledgment", "title": "致谢", "bullets": []}
  ]
}

注意：keywords、search_queries 不在本 JSON 中——它们由原 plan 保留，不需要你输出。
"""


def update_plan_from_user(
    plan: WritingPlan,
    user_outline_text: str,
) -> WritingPlan:
    """
    用户在 OUTLINE_REVIEW 阶段提交修改后的大纲（任意格式：Markdown / 数字编号 / 中英混排）。
    优先调用 LLM 做语义解析；LLM 失败时回退到旧版字符串解析器。
    保持 keywords / search_queries 不变。
    """
    # ── 优先：LLM 语义解析 ──
    try:
        raw = chat_json(
            messages=build_messages(
                _OUTLINE_PARSE_USER_SYSTEM_PROMPT,
                f"## 用户提交的大纲文本\n\n{user_outline_text}\n\n请解析为 JSON。",
            ),
            temperature=0.1,
            max_tokens=4000,
        )
        outline_raw = raw.get("outline", [])
        if isinstance(outline_raw, list) and outline_raw:
            new_sections = _parse_user_outline_with_llm(outline_raw, plan)
            if new_sections:
                plan.outline = new_sections
                logger.info("大纲已通过 LLM 解析更新为 %d 章节（用户提交）", len(new_sections))
                return plan
    except Exception as e:
        logger.warning("LLM 解析用户大纲失败，回退到字符串解析: %s", e)

    # ── 兜底：旧版字符串解析器 ──
    return _update_plan_from_user_fallback(plan, user_outline_text)


def _parse_user_outline_with_llm(
    outline_raw: list[dict],
    plan: WritingPlan,
) -> list[SectionNode]:
    """将 LLM 解析的输出转换为 SectionNode 列表，并补全缺失的标准章节。"""
    parsed: dict[str, SectionNode] = {}
    for item in outline_raw:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("section_id", "")).strip()
        if not sid:
            continue
        parsed[sid] = SectionNode(
            section_id=sid,
            title=str(item.get("title", "")).strip(),
            bullets=_str_list(item.get("bullets")),
            outline_detail=str(item.get("outline_detail", "")).strip(),
            scope_must_include=_str_list(item.get("scope_must_include")),
            scope_forbidden=_str_list(item.get("scope_forbidden")),
            subsections=[],
        )

    if not parsed:
        return []

    # 补全标准章节（用原 plan 中已有的兜底）
    old_map = {s.section_id: s for s in plan.outline}
    _STANDARD_SECTIONS = [
        ("abstract_zh", "摘要", ["研究目的", "研究方法", "主要结果", "结论"]),
        ("abstract_en", "Abstract", []),
        ("s1", "第1章 引言", ["研究背景与意义", "国内外研究现状概览", "本文工作与贡献", "论文结构安排"]),
        ("s2", "第2章", ["核心技术分析", "方法对比", "改进方向"]),
        ("s3", "第3章", ["总体架构", "模块划分", "关键技术选型"]),
        ("s4", "第4章", ["详细设计", "具体实现", "关键流程"]),
        ("s5", "第5章", ["测试方案", "结果分析", "综合评估"]),
        ("s6", "第6章 结论", ["主要工作总结", "创新贡献", "研究局限", "未来工作展望"]),
        ("acknowledgment", "致谢", []),
    ]
    result: list[SectionNode] = []
    for sid, default_title, default_bullets in _STANDARD_SECTIONS:
        if sid in parsed:
            node = parsed[sid]
            if not node.title:
                node.title = old_map.get(sid, SectionNode(section_id=sid, title=default_title)).title or default_title
            if not node.bullets:
                node.bullets = list(default_bullets)
            result.append(node)
        elif sid in old_map:
            result.append(old_map[sid])
        else:
            result.append(SectionNode(
                section_id=sid,
                title=default_title,
                bullets=list(default_bullets),
            ))
    return result


def _update_plan_from_user_fallback(
    plan: WritingPlan,
    user_outline_text: str,
) -> WritingPlan:
    """
    旧版纯字符串解析器 — LLM 不可用时的兜底。
    解析出章节列表并更新到 plan 中，保持 keywords / search_queries 不变。
    """
    lines = user_outline_text.strip().splitlines()
    new_sections = []
    current_title = None
    current_bullets: list[str] = []
    section_counter = 0

    def _flush():
        nonlocal current_title, current_bullets
        if current_title:
            section_counter_val = len(new_sections) + 1
            new_sections.append(SectionNode(
                section_id=f"s{section_counter_val}",
                title=current_title,
                bullets=list(current_bullets),
            ))
        current_title = None
        current_bullets = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            _flush()
            current_title = stripped.lstrip("#").strip()
        elif stripped.startswith("-") or stripped.startswith("•"):
            if current_title:
                current_bullets.append(stripped.lstrip("-•").strip())
        else:
            if not current_title:
                current_title = stripped
    _flush()

    if new_sections:
        plan.outline = new_sections
        logger.info("大纲已更新为 %d 章节（用户提交-兜底解析）", len(new_sections))
    else:
        logger.warning("无法解析用户提交的大纲，保留原大纲")

    return plan


# ═══════════════════════════════════════════════════════════════
# 大纲评分体系
# ═══════════════════════════════════════════════════════════════

_OUTLINE_EVAL_SYSTEM_PROMPT = """你是一名严谨的学术论文评审专家。请根据给定的论文大纲，从以下五个维度评分（每个维度0-10分，可带一位小数）：

1. 逻辑结构 (logic)：章节设置是否完整、递进是否清晰，标题能否概括内容。
2. 内容与深度 (content_depth)：研究问题是否明确，论证路径是否充分，文献综述是否到位。
3. 可行性与工作量 (feasibility)：技术路线是否清晰，工作安排是否合理，工作量是否饱满。
4. 规范符合度 (format_compliance)：标题层级是否规范，每节是否有足够段落，术语表达是否准确，有无低级格式错误。请灵活判断，不宜机械扣分——例如"基于STM32的智能水杯设计"这种工科常见标题可接受，但明显不规范的表述应指出。
5. 创新性与价值 (novelty)：选题是否有新意，是否具有实际或理论价值。（本科论文不要求重大创新，此维度权重较低）

权重：总分 = logic*0.30 + content_depth*0.25 + feasibility*0.25 + format_compliance*0.15 + novelty*0.05

输出必须为合法 JSON，格式如下：
{
  "logic": 数字,
  "content_depth": 数字,
  "feasibility": 数字,
  "format_compliance": 数字,
  "novelty": 数字,
  "actionable_items": ["具体修改建议1", "建议2", ...]
}

注意：
- actionable_items 必须是具体、可执行的修改建议（如"第2章缺少需求分析，建议增加一节'2.1 系统需求分析'"），不要泛泛而谈。
- 如果大纲质量很高（总分≥8.5），actionable_items 可为空列表。
"""

_OUTLINE_REVISE_SYSTEM_PROMPT = """你是一位学术写作规划专家。用户提供了一份论文大纲和具体的修改建议，请根据建议修订大纲。

重要规则：
1. 保持所有 section_id 不变
2. 仅根据修改建议调整标题、要点（bullets）、outline_detail 等内容
3. 不要新增或删除顶层章节
4. 如果修改建议涉及结构调整（如拆分/合并小节），通过 subsections 字段体现

输出必须是合法 JSON，格式与原始大纲一致：
{
  "outline": [
    {
      "section_id": "原始id",
      "title": "修改后的标题",
      "bullets": ["修改后的要点"],
      "outline_detail": "修改后的可执行说明",
      "scope_must_include": [],
      "scope_forbidden": [],
      "subsections": []
    }
  ],
  "keywords": ["关键词"],
  "search_queries": ["检索词"],
  "manual_ref_ids": []
}
"""

_OUTLINE_FIX_ERRORS_SYSTEM_PROMPT = """你是一位严格的学术写作规划专家。用户大纲中存在格式规范硬性错误，你必须逐一修正这些错误，不能遗漏。

【最高优先级 · 必须逐一修正】
1. 每一条「【必须修正】」标记的问题都是致命格式错误，必须修正后再输出
2. 保持所有 section_id 不变，不要新增或删除顶层章节
3. 标题修正示例：
   - "第2章 相关技术" → 必须改为 "第2章 相关技术"（确保"第X章"后有一个空格，且X在2-5之间）
   - 缺少"第X章"前缀的标题必须补上（如 s2→"第2章 xxx"、s3→"第3章 xxx"）
4. 中文题目超过25字必须精简；中文题目含标点符号必须去除
5. 中文关键词不足3个的必须补充到3-5个；含分隔符的关键词必须拆分
6. 中英文关键词数量必须一致
7. s1 要点中不得包含"系统设计/硬件设计/电路/代码实现/实验数据/测试结果"等内容
8. s6 要点中不得包含"学习感悟/自我检讨/感谢导师/心得体会"等个人化表述
9. 英文摘要（abstract_en）要点可为空列表，中文摘要（abstract_zh）要点至少覆盖目的/方法/结果/结论中的3项
10. 检索词至少4条

请注意：其他非错误类的建议（如"建议增加xxx"）可以酌情处理，但以上错误必须立即修正。

输出必须是合法 JSON，格式与原始大纲一致：
{
  "outline": [
    {
      "section_id": "原始id（不可改）",
      "title": "修正后的标题",
      "bullets": ["修正后的要点"],
      "outline_detail": "修正后的可执行说明",
      "scope_must_include": [],
      "scope_forbidden": [],
      "subsections": []
    }
  ],
  "keywords": ["关键词"],
  "search_queries": ["检索词"],
  "manual_ref_ids": []
}
"""

# 毕业论文标准章节 section_id 集合
def _normalize_outline_titles(plan: WritingPlan) -> None:
    """将 outline 中 s1-s6 的标题统一为「第X章 原始标题」格式。"""
    import re as _re
    for sec in plan.outline:
        if sec.section_id in {"s1", "s2", "s3", "s4", "s5", "s6"}:
            ch_num = int(sec.section_id[1:])
            stripped = _re.sub(r"^(?:第\d+章\s*)+", "", sec.title).strip()
            new_title = f"第{ch_num}章 {stripped}" if stripped else f"第{ch_num}章"
            if new_title != sec.title:
                logger.debug("大纲标题规范化: %r → %r", sec.title, new_title)
                sec.title = new_title


def _clean_top_level_subsections(plan: WritingPlan) -> None:
    """将 outline 顶层中形如 s3_1 的子节节点迁移到对应父章节的 subsections 中。"""
    import re as _re

    # 1. 收集顶层子节，按父章节分组
    top_sub_pattern = _re.compile(r"^s(\d+)_(\d+)$")
    orphans: dict[str, list[SectionNode]] = {}
    new_outline: list[SectionNode] = []
    for sec in plan.outline:
        m = top_sub_pattern.match(sec.section_id)
        if m:
            parent_id = f"s{m.group(1)}"
            orphans.setdefault(parent_id, []).append(sec)
        else:
            new_outline.append(sec)

    if not orphans:
        return

    # 2. 迁移到父章节 subsections
    outline_map = {s.section_id: s for s in new_outline}
    for parent_id, subs in orphans.items():
        subs.sort(key=lambda x: int(top_sub_pattern.match(x.section_id).group(2)))
        parent = outline_map.get(parent_id)
        if not parent:
            parent = SectionNode(
                section_id=parent_id,
                title=f"第{parent_id[1:]}章（自动补全）",
                subsections=[],
            )
            new_outline.append(parent)
            outline_map[parent_id] = parent
            logger.warning("父章节 %s 不存在，自动创建占位", parent_id)

        # 合并时去重：已有相同 section_id 的跳过
        existing_ids = {s.section_id for s in parent.subsections}
        for sub in subs:
            if sub.section_id not in existing_ids:
                parent.subsections.append(sub)
                existing_ids.add(sub.section_id)
                logger.warning("迁移顶层子节 %s → %s.subsections", sub.section_id, parent_id)

    plan.outline = new_outline
    logger.info("清理并迁移顶层子节: %d 个节点 → 归入 %d 个父章节",
                sum(len(v) for v in orphans.values()), len(orphans))


_THESIS_REQUIRED_IDS = {"abstract_zh", "abstract_en", "s1", "s2", "s3", "s4", "s5", "s6", "acknowledgment"}

# s3/s4 相关关键词（不应出现在 s1）
_S1_FORBIDDEN_KEYWORDS = ["系统设计", "硬件设计", "电路", "代码实现", "实验数据", "测试结果", "原理图", "PCB"]

# s6 禁止内容关键词
_S6_FORBIDDEN_KEYWORDS = ["学习感悟", "自我检讨", "致谢", "感谢导师", "感谢老师", "心得体会", "收获很大"]


def _check_outline_hard_rules(plan: WritingPlan, thesis_mode: bool) -> list[dict]:
    """
    对大纲进行硬规则检查（不依赖 LLM）。
    返回问题列表，每项：{rule_id, message, severity: error|warning}
    """
    issues: list[dict] = []

    # ── 通用规则 ──

    # 1. 大纲非空
    if not plan.outline:
        issues.append({"rule_id": "OUTLINE_EMPTY", "message": "大纲为空，无法继续", "severity": "error"})
        return issues

    # 2. section_id 唯一性
    all_ids: list[str] = []

    def _collect_ids(sec: SectionNode):
        all_ids.append(sec.section_id)
        for sub in sec.subsections:
            _collect_ids(sub)

    for sec in plan.outline:
        _collect_ids(sec)
    dup_ids = sorted(set(sid for sid in all_ids if all_ids.count(sid) > 1))
    if dup_ids:
        issues.append({"rule_id": "SECTION_ID_DUP",
                       "message": f"section_id 重复：{', '.join(dup_ids)}", "severity": "error"})

    # 3. 每章有 title
    for sec in plan.outline:
        if not sec.title.strip():
            issues.append({"rule_id": "SECTION_NO_TITLE",
                           "message": f"章节 {sec.section_id} 缺少标题", "severity": "error"})
        for sub in sec.subsections:
            if not sub.title.strip():
                issues.append({"rule_id": "SUBSECTION_NO_TITLE",
                               "message": f"子节 {sub.section_id}（父节点 {sec.section_id}）缺少标题", "severity": "warning"})

    # 4. 顶层章节至少 2 条 bullets
    for sec in plan.outline:
        if len(sec.bullets) < 2:
            issues.append({"rule_id": "SECTION_FEW_BULLETS",
                           "message": f"章节 {sec.section_id}（{sec.title}）要点不足（{len(sec.bullets)} 条，至少 2 条）",
                           "severity": "warning"})

    # 5. subsections section_id 不与祖先节点冲突
    def _check_sub_ids(sec: SectionNode, ancestor_ids: set):
        for sub in sec.subsections:
            if sub.section_id in ancestor_ids:
                issues.append({"rule_id": "SUBSECTION_ID_CONFLICT",
                               "message": f"子节点 section_id '{sub.section_id}' 与祖先节点冲突",
                               "severity": "error"})
            _check_sub_ids(sub, ancestor_ids | {sub.section_id})

    for sec in plan.outline:
        _check_sub_ids(sec, {sec.section_id})

    # 6. keywords 不为空
    if not plan.keywords:
        issues.append({"rule_id": "KEYWORDS_EMPTY", "message": "关键词列表为空", "severity": "error"})

    # 7. search_queries 不为空
    if not plan.search_queries:
        issues.append({"rule_id": "SEARCH_QUERIES_EMPTY", "message": "检索词列表为空", "severity": "error"})

    # 8. 标题长度合理性
    for sec in plan.outline:
        if len(sec.title) > 80:
            issues.append({"rule_id": "TITLE_TOO_LONG",
                           "message": f"章节 {sec.section_id} 标题过长（{len(sec.title)} 字）：{sec.title[:60]}...",
                           "severity": "warning"})

    # ── 毕业论文模式专用规则 ──
    if thesis_mode:
        existing_ids = {s.section_id for s in plan.outline}

        # 9. 必要章节齐全
        missing_ids = _THESIS_REQUIRED_IDS - existing_ids
        if missing_ids:
            issues.append({"rule_id": "THESIS_MISSING_SECTIONS",
                           "message": f"缺少必要章节：{', '.join(sorted(missing_ids))}",
                           "severity": "error"})

        # 10. 无多余 section_id
        extra_ids = existing_ids - _THESIS_REQUIRED_IDS
        if extra_ids:
            issues.append({"rule_id": "THESIS_EXTRA_SECTIONS",
                           "message": f"存在非标准章节：{', '.join(sorted(extra_ids))}",
                           "severity": "warning"})

        # 11. 中文题目 ≤25 字
        if plan.title:
            title_clean = plan.title.strip()
            if len(title_clean) > 25:
                issues.append({"rule_id": "THESIS_TITLE_TOO_LONG",
                               "message": f"中文题目超过 25 字（{len(title_clean)} 字）：{title_clean}",
                               "severity": "error"})
            # 题目含标点（中文标点）
            if re.search(r'[，。！？、；：""''（）《》…—]', title_clean):
                issues.append({"rule_id": "THESIS_TITLE_HAS_PUNCT",
                               "message": f"中文题目含标点符号：{title_clean}", "severity": "warning"})

        # 12. 题目冗余词
        if plan.title:
            for suffix in ["的研究", "的应用", "的设计与实现", "的设计", "的实现"]:
                if plan.title.strip().endswith(suffix):
                    issues.append({"rule_id": "THESIS_TITLE_REDUNDANT",
                                   "message": f"中文题目以「{suffix}」结尾，建议精简", "severity": "warning"})
                    break

        # 13. s2~s5 标题以「第X章」开头
        for sid in ["s2", "s3", "s4", "s5"]:
            sec = next((s for s in plan.outline if s.section_id == sid), None)
            if sec and not re.match(r'^第[2-5]章\s', sec.title):
                issues.append({"rule_id": "THESIS_CHAPTER_PREFIX",
                               "message": f"章节 {sid} 标题未以「第X章」开头：{sec.title}", "severity": "error"})

        # 14. 中文关键词 3-5 个
        kw_count = len(plan.keywords)
        if kw_count < 3:
            issues.append({"rule_id": "THESIS_KEYWORDS_TOO_FEW",
                           "message": f"中文关键词不足 3 个（当前 {kw_count} 个）", "severity": "error"})
        elif kw_count > 5:
            issues.append({"rule_id": "THESIS_KEYWORDS_TOO_MANY",
                           "message": f"中文关键词超过 5 个（当前 {kw_count} 个）", "severity": "warning"})

        # 15. 中英文关键词一一对应
        if plan.keywords_en and len(plan.keywords_en) != len(plan.keywords):
            issues.append({"rule_id": "THESIS_KEYWORDS_EN_MISMATCH",
                           "message": f"中英文关键词数量不一致（中文 {len(plan.keywords)} 个，英文 {len(plan.keywords_en)} 个）",
                           "severity": "error"})

        # 16. 关键词不含分隔符
        for kw in plan.keywords:
            if any(sep in kw for sep in ["；", "，", "、"]):
                issues.append({"rule_id": "THESIS_KEYWORD_HAS_SEP",
                               "message": f"关键词「{kw}」内含分隔符，应拆分为独立关键词", "severity": "warning"})
                break

        # 17. s1 bullets 检查（须含背景/意义，不含 s3/s4 关键词）
        s1 = next((s for s in plan.outline if s.section_id == "s1"), None)
        if s1:
            s1_text = " ".join(s1.bullets)
            has_background = any(kw in s1_text for kw in ["背景", "意义", "现状", "问题", "贡献"])
            if not has_background:
                issues.append({"rule_id": "THESIS_S1_NO_BACKGROUND",
                               "message": "第1章引言要点缺少「研究背景/意义/现状/贡献」相关内容", "severity": "warning"})
            for fkw in _S1_FORBIDDEN_KEYWORDS:
                if fkw in s1_text:
                    issues.append({"rule_id": "THESIS_S1_FORBIDDEN_CONTENT",
                                   "message": f"第1章引言要点包含不应出现的内容：「{fkw}」，应在后续章节展开",
                                   "severity": "error"})
                    break

        # 18. s6 结论无禁止内容
        s6 = next((s for s in plan.outline if s.section_id == "s6"), None)
        if s6:
            s6_text = " ".join(s6.bullets)
            for fkw in _S6_FORBIDDEN_KEYWORDS:
                if fkw in s6_text:
                    issues.append({"rule_id": "THESIS_S6_FORBIDDEN_CONTENT",
                                   "message": f"第6章结论包含禁止内容：「{fkw}」", "severity": "error"})
                    break

        # 19. s6 bullets 须含总结/贡献
        if s6:
            has_summary = any(kw in s6_text for kw in ["总结", "贡献", "工作", "创新"])
            if not has_summary:
                issues.append({"rule_id": "THESIS_S6_NO_SUMMARY",
                               "message": "第6章结论缺少「工作总结/创新贡献」相关内容", "severity": "warning"})

        # 20. search_queries ≥ 4 条
        if len(plan.search_queries) < 4:
            issues.append({"rule_id": "THESIS_SEARCH_QUERIES_FEW",
                           "message": f"检索词不足 4 条（当前 {len(plan.search_queries)} 条）", "severity": "warning"})

        # 21. 摘要 bullets 完整（须含目的/方法/结果/结论中至少 3 个）
        abstract_zh = next((s for s in plan.outline if s.section_id == "abstract_zh"), None)
        if abstract_zh:
            ab_text = " ".join(abstract_zh.bullets)
            hits = sum(1 for kw in ["目的", "方法", "结果", "结论"] if kw in ab_text)
            if hits < 3:
                issues.append({"rule_id": "THESIS_ABSTRACT_INCOMPLETE",
                               "message": f"中文摘要要点覆盖不全（目的/方法/结果/结论仅命中 {hits}/4）",
                               "severity": "warning"})

        # 22. s3 含 subsections 则至少 2 个
        s3 = next((s for s in plan.outline if s.section_id == "s3"), None)
        if s3 and s3.subsections and len(s3.subsections) < 2:
            issues.append({"rule_id": "THESIS_S3_SUBSECTIONS_FEW",
                           "message": "第3章仅有 1 个子节，建议至少 2 个或移除 subsections", "severity": "warning"})

        # 28. s1 outline_detail 须声明引言类型（饱满型/简要型）
        if s1 and s1.outline_detail:
            if "饱满型" not in s1.outline_detail and "简要型" not in s1.outline_detail:
                issues.append({"rule_id": "THESIS_S1_NO_INTRO_TYPE",
                               "message": "第1章 outline_detail 未声明引言类型（饱满型/简要型），将影响第2章方向",
                               "severity": "warning"})

    # ── 普通模式专用规则 ──
    else:
        # 24. 最少 6 个顶层章节
        if len(plan.outline) < 6:
            issues.append({"rule_id": "NORMAL_TOO_FEW_SECTIONS",
                           "message": f"大纲章节不足（{len(plan.outline)} 章，至少 6 章：摘要/引言/相关工作/方法/实验/结论）",
                           "severity": "error"})

        # 25. keywords 5-10 个
        if len(plan.keywords) < 5:
            issues.append({"rule_id": "NORMAL_KEYWORDS_FEW",
                           "message": f"关键词不足 5 个（当前 {len(plan.keywords)} 个）", "severity": "warning"})

        # 26. search_queries 4-10 条
        sq_count = len(plan.search_queries)
        if sq_count < 4:
            issues.append({"rule_id": "NORMAL_SEARCH_QUERIES_FEW",
                           "message": f"检索词不足 4 条（当前 {sq_count} 条）", "severity": "warning"})
        elif sq_count > 10:
            issues.append({"rule_id": "NORMAL_SEARCH_QUERIES_MANY",
                           "message": f"检索词超过 10 条（当前 {sq_count} 条）", "severity": "warning"})

        # 27. 关键章节语义检查
        title_text = " ".join(s.title for s in plan.outline)
        for expected in ["摘要", "引言", "方法", "实验", "结论"]:
            if expected not in title_text:
                issues.append({"rule_id": "NORMAL_MISSING_SEMANTIC_SECTION",
                               "message": f"大纲未找到「{expected}」相关章节，建议补充", "severity": "warning"})
                break

    return issues


def evaluate_outline(plan: WritingPlan, threshold: float = 7.5) -> dict:
    """
    大纲质量评估（硬规则 + LLM 语义评分）。

    返回：
    {
        "total_score": float,           # 加权总分（0-10），含 warning 惩罚后的最终分
        "raw_llm_score": float,         # 纯 LLM 加权分（惩罚前）
        "dimension_scores": {           # 各维度得分
            "logic": float,
            "content_depth": float,
            "feasibility": float,
            "format_compliance": float,
            "novelty": float,
        },
        "hard_rule_issues": [...],      # 硬规则检查结果
        "hard_errors": int,             # 硬规则 error 数量
        "hard_warnings": int,           # 硬规则 warning 数量
        "warning_penalty": float,       # warning 惩罚扣分（warning≥5 时生效）
        "actionable_items": [...],      # 修改建议（硬规则 + LLM 合并）
        "passed": bool,                 # total_score >= threshold 且无 hard errors
    }
    """
    thesis_mode = bool(get("thesis_mode", False))
    hard_issues = _check_outline_hard_rules(plan, thesis_mode)
    hard_errors = sum(1 for i in hard_issues if i["severity"] == "error")
    hard_warnings = sum(1 for i in hard_issues if i["severity"] == "warning")
    hard_suggestions = [f"【硬规则】{i['message']}" for i in hard_issues]

    # 结构级硬 error 才阻断语义评分（缺失章节/ID重复等 LLM 无法合法生成 JSON 修复的）
    _STRUCTURAL_ERROR_IDS = {
        "OUTLINE_EMPTY", "SECTION_ID_DUP", "SUBSECTION_ID_CONFLICT",
        "THESIS_MISSING_SECTIONS",
    }
    structural_errors = [i for i in hard_issues
                         if i["severity"] == "error" and i["rule_id"] in _STRUCTURAL_ERROR_IDS]

    if structural_errors:
        return {
            "total_score": 0.0,
            "raw_llm_score": 0.0,
            "dimension_scores": {
                "logic": 0.0, "content_depth": 0.0, "feasibility": 0.0,
                "format_compliance": 0.0, "novelty": 0.0,
            },
            "hard_rule_issues": hard_issues,
            "hard_errors": hard_errors,
            "hard_warnings": hard_warnings,
            "warning_penalty": 0.0,
            "actionable_items": hard_suggestions,
            "passed": False,
        }

    # LLM 语义评分
    outline_md = outline_to_markdown(plan)
    user_prompt = f"请评估以下论文大纲：\n\n{outline_md}"

    try:
        raw = chat_json(
            messages=build_messages(_OUTLINE_EVAL_SYSTEM_PROMPT, user_prompt),
            temperature=0.2,
            max_tokens=2000,
        )
    except Exception as e:
        logger.error("大纲语义评分失败: %s", e)
        raw = {}

    if not raw:
        # LLM 完全失败：仅返回硬规则结果
        return {
            "total_score": 5.0,
            "raw_llm_score": 5.0,
            "dimension_scores": {
                "logic": 5.0, "content_depth": 5.0, "feasibility": 5.0,
                "format_compliance": 5.0, "novelty": 5.0,
            },
            "hard_rule_issues": hard_issues,
            "hard_errors": hard_errors,
            "hard_warnings": hard_warnings,
            "warning_penalty": 0.0,
            "actionable_items": hard_suggestions,
            "passed": False,
        }

    logic = float(raw.get("logic", 5))
    content_depth = float(raw.get("content_depth", 5))
    feasibility = float(raw.get("feasibility", 5))
    format_compliance = float(raw.get("format_compliance", 5))
    novelty = float(raw.get("novelty", 5))

    raw_score = (
        logic * 0.30
        + content_depth * 0.25
        + feasibility * 0.25
        + format_compliance * 0.15
        + novelty * 0.05
    )

    # warning 惩罚：warning ≥5 时扣分，每 warning -0.1，上限 -1.0
    warning_penalty = min(hard_warnings * 0.1, 1.0) if hard_warnings >= 5 else 0.0
    total = max(raw_score - warning_penalty, 0.0)

    llm_items = raw.get("actionable_items", [])
    if not isinstance(llm_items, list):
        llm_items = []
    llm_items = [str(x).strip() for x in llm_items if str(x).strip()]

    all_items = hard_suggestions + llm_items

    return {
        "total_score": round(total, 1),
        "raw_llm_score": round(raw_score, 1),
        "dimension_scores": {
            "logic": round(logic, 1),
            "content_depth": round(content_depth, 1),
            "feasibility": round(feasibility, 1),
            "format_compliance": round(format_compliance, 1),
            "novelty": round(novelty, 1),
        },
        "hard_rule_issues": hard_issues,
        "hard_errors": hard_errors,
        "hard_warnings": hard_warnings,
        "warning_penalty": round(warning_penalty, 1),
        "actionable_items": all_items,
        "passed": (total >= threshold),
    }


def revise_outline_fix_errors(
    plan: WritingPlan,
    hard_issues: list[dict],
) -> WritingPlan:
    """
    优先修复大纲硬规则 error（如标题缺少「第X章」前缀、关键词不足等）。
    将 error 列表以【必须修正】前缀强制发给 LLM，要求逐条修正。
    返回修订后的 WritingPlan；解析失败则返回原 plan。
    """
    errors = [i for i in hard_issues if i["severity"] == "error"]
    if not errors:
        return plan

    outline_md = outline_to_markdown(plan)
    error_lines = "\n".join(
        f"- 【必须修正】{e['rule_id']}: {e['message']}" for e in errors
    )
    user_prompt = (
        f"## 当前大纲\n\n{outline_md}\n\n"
        f"## 必须修正的格式错误（逐条修正，不容遗漏）\n\n{error_lines}\n\n"
        f"请修正以上所有错误后输出完整 JSON。"
    )

    try:
        raw = chat_json(
            messages=build_messages(_OUTLINE_FIX_ERRORS_SYSTEM_PROMPT, user_prompt),
            temperature=0.15,
            max_tokens=8000,
        )
    except Exception as e:
        logger.error("硬规则错误修复失败: %s", e)
        return plan

    if not raw or "outline" not in raw:
        logger.warning("硬规则错误修复返回不完整，保留原大纲")
        return plan

    revised_outline: list[SectionNode] = []
    outline_raw = raw.get("outline", [])
    if isinstance(outline_raw, list):
        for i, item in enumerate(outline_raw):
            node = _parse_outline_node(item, f"s{i+1}")
            if node:
                revised_outline.append(node)

    if not revised_outline:
        logger.warning("硬规则错误修复后无法解析 outline，保留原大纲")
        return plan

    old_map = {s.section_id: s for s in plan.outline}
    new_map = {s.section_id: s for s in revised_outline}
    merged: list[SectionNode] = []

    for old_sec in plan.outline:
        sid = old_sec.section_id
        if sid in new_map:
            merged.append(new_map[sid])
        else:
            merged.append(old_sec)

    for new_sec in revised_outline:
        if new_sec.section_id not in {s.section_id for s in merged}:
            merged.append(new_sec)

    merged.sort(key=lambda s: list(old_map.keys()).index(s.section_id)
                if s.section_id in old_map else len(merged))

    plan.outline = merged
    logger.info("硬规则错误修复完成：%d 章节", len(merged))
    return plan


def revise_outline(
    plan: WritingPlan,
    actionable_items: list[str],
) -> WritingPlan:
    """
    根据修改建议，调用 LLM 修订大纲。
    保持 section_id 不变，调整标题、bullets、outline_detail 等内容。
    返回修订后的 WritingPlan；解析失败则返回原 plan。
    """
    if not actionable_items:
        return plan

    outline_md = outline_to_markdown(plan)
    suggestions = "\n".join(f"- {item}" for item in actionable_items)
    user_prompt = (
        f"## 当前大纲\n\n{outline_md}\n\n"
        f"## 修改建议\n\n{suggestions}\n\n"
        f"请根据以上建议修订大纲，保持所有 section_id 不变，输出完整 JSON。"
    )

    try:
        raw = chat_json(
            messages=build_messages(_OUTLINE_REVISE_SYSTEM_PROMPT, user_prompt),
            temperature=0.3,
            max_tokens=8000,
        )
    except Exception as e:
        logger.error("大纲修订失败: %s", e)
        return plan

    if not raw or "outline" not in raw:
        logger.warning("大纲修订返回不完整，保留原大纲")
        return plan

    # 解析修订后的大纲
    revised_outline: list[SectionNode] = []
    outline_raw = raw.get("outline", [])
    if isinstance(outline_raw, list):
        for i, item in enumerate(outline_raw):
            node = _parse_outline_node(item, f"s{i+1}")
            if node:
                revised_outline.append(node)

    if not revised_outline:
        logger.warning("大纲修订后无法解析 outline，保留原大纲")
        return plan

    # 合并：保持原 section_id，用修订内容更新
    old_map = {s.section_id: s for s in plan.outline}
    new_map = {s.section_id: s for s in revised_outline}
    merged: list[SectionNode] = []

    for old_sec in plan.outline:
        sid = old_sec.section_id
        if sid in new_map:
            merged.append(new_map[sid])
        else:
            merged.append(old_sec)

    # 处理新增章节（修订后多出来的）
    for new_sec in revised_outline:
        if new_sec.section_id not in {s.section_id for s in merged}:
            merged.append(new_sec)

    # 保持原有章节顺序
    merged.sort(key=lambda s: list(old_map.keys()).index(s.section_id)
                if s.section_id in old_map else len(merged))

    plan.outline = merged
    logger.info("大纲修订完成：%d 章节", len(merged))
    return plan
