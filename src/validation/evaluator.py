"""
质量评估模块
输入：Manuscript、WritingPlan、用户需求摘要
输出：Evaluation（经 Pydantic schema 校验）
降级：两次 JSON 解析失败 → 文本警告 + 保守默认分（5.0）

毕业论文模式额外检查：
  - 字数是否达标（总体 + 各章节）
  - 参考文献是否 ≥15 篇，外文 ≥5 篇
  - 摘要是否含有禁止内容（图表引用、论文题目重复）
  - 结论是否含有个人感悟/自我检讨
  - 关键词数量是否在 3-5 个
  - 引用一致性（[数字] 格式）
  - 摘要/关键词与正文的主控型号（STM32 系 / STC / ESP 等）是否一致
  - 正文中是否残留生成失败或截断占位标记

面向 Agent 的人文说明与同步约定见同目录 README.md。
"""

import logging
import re
from typing import List, Literal

from pydantic import ValidationError

from ..config import get
from ..llm import chat_json, build_messages
from ..models import (
    Evaluation,
    EvaluationDimensions,
    Manuscript,
    StaticRuleIssue,
    WritingPlan,
)
from ..writing.revision_helpers import dedupe_llm_against_static
from ..ref_store import ReferenceStore

logger = logging.getLogger(__name__)


# ── 普通评估 Prompt ────────────────────────────────────────────

_SYSTEM_EVAL_NORMAL = """你是一位严格的学术论文评审专家。
请对提供的论文草稿进行全面评估，从以下四个维度打分（各 0–10 分），
并给出可执行的修改建议。

输出必须是合法 JSON，结构如下：
{
  "score_total": 7.5,
  "dimensions": {
    "structure": 8.0,
    "logic": 7.5,
    "language": 7.0,
    "alignment": 7.5
  },
  "feedback": "总体评价文字",
  "actionable_items": [
    "建议1：具体修改方向",
    "建议2：具体修改方向"
  ]
}

评分标准：
- structure（结构）：章节完整性、逻辑层次、篇幅分配合理性
- logic（逻辑）：论点支撑、推理严密、前后呼应
- language（语言）：学术规范、语言精确、表达清晰
- alignment（匹配）：与用户需求和大纲要求的吻合程度
- score_total：四维度加权均值（权重可自定，但须反映综合水平）
"""

# ── 毕业论文评估 Prompt ───────────────────────────────────────

_SYSTEM_EVAL_THESIS = """你是一位评审过300+本科毕业论文的资深评审专家，熟悉中国高校毕业论文规范。
请对提供的毕业论文草稿进行极其严格的评估，找出所有实质性缺陷。

输出必须是合法 JSON：
{
  "score_total": 7.5,
  "dimensions": {
    "structure": 8.0,
    "logic": 7.5,
    "language": 7.0,
    "alignment": 7.5
  },
  "feedback": "总体评价（2-3句）",
  "actionable_items": [
    "【结构】第2章后半段混入第3章内容，需将'### 3.1 ...'及以后内容移至第3章",
    "【逻辑】第1章提出的三个研究问题在结论中未逐一回应，请逐条补充",
    "【内容一致性】第1章提到DS18B20温度传感器，第3章却改用DHT11，请统一为一种型号并说明选型理由",
    "【引用】正文第3章3.2节中[46]未出现在参考文献列表，需核查"
  ]
}

【评分细则（0-10分）】

structure（结构）：
- 9-10：结构完整，章节层次清晰，各章篇幅合理，无遗漏章节
- 7-8：结构基本完整，个别章节篇幅稍短或标题不规范
- 5-6：缺少1-2个必要章节，或章节内容明显错位（如s1正文包含s2内容）
- 0-4：严重结构缺失或混乱

logic（逻辑）：
- 9-10：研究问题→方法→结果→结论逻辑严密，引言贡献与结论呼应
- 7-8：主体逻辑通顺，个别论断缺乏论证支撑（注意：排除实验数据缺失类）
- 5-6：多处逻辑跳跃或论证链断裂，贡献点与正文描述不符
- 0-4：逻辑混乱，大量前后矛盾
logic 评分请注意区分：
- "论证链缺失"（如选PID但未解释理由、公式推导缺步骤、方案比较缺依据）→ 扣分
- "实验数据缺失"（正文标记[实测数据]/[待测]/[待实验验证]/TBD等的）→ 不扣分，这些是留给作者后期补充实验数据的占位符

language（语言）：
- 9-10：学术语言规范，引用格式统一（[1]在标点前），无禁用内容
- 7-8：偶有口语化表达或格式小错
- 5-6：多处语言不规范，摘要/结论含违规内容
- 0-4：大量口语化，格式错误，禁用内容多

alignment（匹配）：
- 9-10：内容与研究主题高度一致，技术参数/型号前后一致（排除实验数据占位符）
- 7-8：整体匹配，个别细节偏差
- 5-6：部分章节偏离主题，关键型号/参数前后矛盾
- 0-4：大量内容偏离或矛盾
alignment 评分请注意区分：
- 技术参数不一致（如型号/通信协议/软件架构前后矛盾）→ 扣分
- 一处写具体数值、另一处写[实测数据]占位符 → 不扣分，不视为矛盾

【总分加权公式】
score_total = structure × 0.30 + logic × 0.30 + language × 0.25 + alignment × 0.15
请严格按此加权计算总分。

【数据占位符豁免（重要）】
以下标记是留给作者后期补充实验数据的占位符，不作为评分扣分项：
  [实测数据]、[待测]、[待实验验证]、[待补充]、[实测]、TBD、TODO
- 正文中出现上述标记不视为内容缺失或质量缺陷
- 但如果同一条信息在一处写实际数值、另一处写占位符，才属于技术矛盾需扣分
- 技术参数（型号、协议、架构、选型理由）不一致仍需正常扣分

【必须主动检查以下常见错误并在 actionable_items 中列出】
1. 技术型号不一致：如第1章提DS18B20，第3章用DHT11；或摘要写BLE正文字写Wi-Fi（语义级技术矛盾，需逐章核对）。
   ⚠ 正文中仅作为方案对比、竞品分析、替代方案评估出现的其他技术型号、控制算法或通信协议，
   不视为不一致（如主控为STC89C51但第2章对比分析了ESP32/Arduino方案，不属于矛盾）。
2. 参考文献格式：佚名作者、期刊未知、重复条目、缺卷期页码
3. 相关工作末尾重复第1章已说明的本文创新点
（说明：章节越界/截断/摘要违规/关键词格式/引用标点/结论个人感悟等已由自动化规则检查，
  此处无需重复检查，避免浪费评估资源）

【可操作项限制（重要）】
- 不要输出要求"补充实验数据"、"补全实测值"、"添加实验表格"、"补实验"、"做定标"
  等需要人工实验才能完成的修订项——这些占位符([实测数据]/[待测]等)是留给作者后期补充的
- 只输出可通过文本修改解决的项：逻辑补全、术语统一、章节重组、语言润色、论证加强、
  技术选型一致性、前后呼应等

actionable_items 要求：
- 每条以【分类标签】开头（【结构】【逻辑】【语言】【内容一致性】【引用】等）
- 必须指出具体位置（如"第3章3.2节"、"参考文献[12]"）和具体问题
- 不得含笼统说法（"加强逻辑"→无效；"第5章5.2节对比实验结论未指出p值或误差范围"→有效）
- 至少5条，最多12条，按严重程度从高到低排列

【输出前自查（仅内部执行，严禁输出到最终回答中）】
在输出最终 JSON 前，在后台进行以下检查——这些检查过程和结果不得写入输出：
1. 是否遗漏了明显的技术型号矛盾、引用格式错误或结论个人感悟？
2. 是否有笼统建议（如"加强逻辑"）混入了 actionable_items？
3. 是否错误地要求补充实验数据？
"""


# ── 毕业论文规则性检查（不依赖 LLM）────────────────────────────

_RULE_META: dict[str, tuple[str, Literal["error", "warning"]]] = {
    "word_count_body": ("structure", "error"),
    "word_count_section": ("structure", "warning"),
    "refs_count_low": ("refs", "error"),
    "refs_foreign_low": ("refs", "warning"),
    "abstract_citation_markers": ("abstract", "error"),
    "abstract_figure_ref": ("abstract", "error"),
    "abstract_too_short": ("abstract", "error"),
    "abstract_too_long": ("abstract", "error"),
    "conclusion_personal_tone": ("language", "error"),
    "conclusion_intro_gap": ("logic", "warning"),
    "keywords_count_low": ("keywords", "warning"),
    "keywords_count_high": ("keywords", "warning"),
    "missing_chapters": ("structure", "error"),
    "author_year_citation_format": ("citation", "error"),
    "citation_after_punct": ("citation", "error"),
    "citation_missing_ref": ("citation", "error"),
    "section_overflow": ("structure", "error"),
    "truncation": ("content", "error"),
    "keywords_line_zh_in_en": ("keywords", "error"),
    "refs_over_max": ("refs", "error"),
    "anon_refs_excess": ("refs", "warning"),
    "keywords_en_missing": ("keywords", "error"),
    "mcu_abstract_body_mismatch": ("mcu", "error"),
    "citation_out_of_scope": ("citation", "error"),
    "placeholder_residual": ("meta", "error"),
    "missing_punct": ("language", "warning"),
    "mixed_punctuation": ("language", "warning"),
    "double_punctuation": ("language", "warning"),
    "missing_subsections": ("structure", "warning"),
}


def _static_rule(rule_id: str, message: str, *, rule_version: str = "1") -> StaticRuleIssue:
    prefix = rule_id.split(":", 1)[0]
    cat, sev = _RULE_META.get(prefix, ("general", "error"))
    return StaticRuleIssue(
        rule_id=rule_id,
        message=message,
        rule_category=cat,
        severity=sev,
        rule_version=rule_version,
    )


def _check_thesis_rules(
    manuscript: Manuscript,
    plan: WritingPlan,
    store: ReferenceStore | None = None,
) -> List[StaticRuleIssue]:
    """
    对毕业论文进行规则性静态检查。
    每条含稳定 rule_id（跨轮差分）与 message（展示 / 并入 actionable_items）。
    store 为 None 时参考文献相关规则自动跳过（用于离线测试场景）。
    """
    if store is None:
        store = ReferenceStore()
    issues: List[StaticRuleIssue] = []
    full_text = manuscript.to_markdown()

    # ── 1. 字数检查 ──────────────────────────────────────────
    # 排除参考文献和关键词章节后计算正文字数
    body_chars = sum(
        len(s.markdown_body)
        for s in manuscript.sections
        if s.section_id not in ("refs", "keywords")
    )
    min_words = int(get("thesis_target_words_min", 25000))
    if body_chars < min_words:
        issues.append(
            _static_rule(
                "word_count_body",
                f"字数不足：当前正文约 {body_chars} 字，目标最低 {min_words} 字。"
                f"建议在研究方法（第3章）和实验分析（第4/5章）章节中补充技术细节。",
            )
        )

    # 各章节字数检查
    section_min_map = {
        "s1": 2000, "s2": 3000, "s3": 4000, "s4": 4000,
        "s5": 3000, "s6": 1000, "abstract_zh": 500,
    }
    for sec in manuscript.sections:
        min_w = section_min_map.get(sec.section_id)
        if min_w and len(sec.markdown_body) < min_w:
            title_str = sec.title
            body_len = len(sec.markdown_body)
            issues.append(
                _static_rule(
                    f"word_count_section:{sec.section_id}",
                    f"章节[{title_str}]内容过短（约{body_len}字，建议至少{min_w}字）。"
                    "请在该章节中补充更多技术细节、分析和论证。",
                )
            )

    # ── 2. 参考文献数量检查 ──────────────────────────────────
    all_refs = store.all_refs()
    total_refs = len(all_refs)
    min_refs = int(get("min_references", 15))
    min_foreign = int(get("min_foreign_references", 5))

    if total_refs < min_refs:
        issues.append(
            _static_rule(
                "refs_count_low",
                f"参考文献不足：当前 {total_refs} 篇，要求至少 {min_refs} 篇。"
                f"请通过检索补充更多文献，特别是近5年发表的期刊论文。",
            )
        )

    # 判断外文文献数量（粗略：title/venue 含英文字母且非全中文）
    foreign_count = sum(
        1 for r in all_refs
        if any(c.isascii() and c.isalpha() for c in (r.title or ""))
        and not any("\u4e00" <= c <= "\u9fff" for c in (r.authors[0] if r.authors else ""))
    )
    if foreign_count < min_foreign:
        issues.append(
            _static_rule(
                "refs_foreign_low",
                f"外文文献不足：检测到约 {foreign_count} 篇外文文献，要求至少 {min_foreign} 篇。"
                f"请补充英文期刊论文（推荐检索 IEEE/ACM/Springer 等）。",
            )
        )

    # ── 3. 摘要合规性检查 ────────────────────────────────────
    abstract_sec = next(
        (s for s in manuscript.sections if s.section_id == "abstract_zh"), None
    )
    if abstract_sec:
        ab_text = abstract_sec.markdown_body

        # 检查是否含有引用标记（摘要不允许）
        if re.search(r"\[\d+\]", ab_text):
            issues.append(
                _static_rule(
                    "abstract_citation_markers",
                    "摘要中含有参考文献引用标记（如[1]），这违反规范。"
                    "请删除摘要中的所有引用标记，摘要应为独立的概括性文字。",
                )
            )

        # 检查是否含有图表引用
        if re.search(r"[图表][\d一二三四五六七八九十]|图\s*\d|表\s*\d", ab_text):
            issues.append(
                _static_rule(
                    "abstract_figure_ref",
                    "摘要中含有图表引用（如[图1]、[表2]），这违反规范。"
                    "请删除摘要中所有图表引用，改用文字描述结果。",
                )
            )

        # 检查摘要字数
        if len(ab_text) < 480:
            issues.append(
                _static_rule(
                    "abstract_too_short",
                    f"中文摘要过短（约{len(ab_text)}字，要求500-800字）。"
                    "摘要需包含：研究目的、研究方法、主要结果、结论四要素，请补充完整。",
                )
            )
        elif len(ab_text) > 850:
            issues.append(
                _static_rule(
                    "abstract_too_long",
                    f"中文摘要过长（约{len(ab_text)}字，要求500-800字）。"
                    "请精简摘要，控制在800字以内。",
                )
            )

    # ── 4. 结论合规性检查 ────────────────────────────────────
    conclusion_sec = next(
        (s for s in manuscript.sections if s.section_id == "s6"), None
    )
    if conclusion_sec:
        conc_text = conclusion_sec.markdown_body
        # 个人感悟/自我检讨的特征词
        bad_words = [
            "学到了", "学到的", "收获了", "感悟", "感慨", "努力学习",
            "基础差", "时间紧", "能力有限", "水平有限", "遗憾", "惭愧",
            "由于时间", "由于水平", "未能完成", "还有许多不足",
        ]
        found_bad = [w for w in bad_words if w in conc_text]
        if found_bad:
            bad_sample = "、".join(found_bad[:3])
            issues.append(
                _static_rule(
                    "conclusion_personal_tone",
                    f"结论章节包含不规范内容（如：{bad_sample}）等个人感悟/自我检讨语句。"
                    "结论只应包含：主要工作总结、创新贡献、研究局限性、未来工作展望。"
                    "请删除所有个人学习体会和自我评价。",
                )
            )

    # ── 5. 关键词检查 ────────────────────────────────────────
    kw_count = len(plan.keywords)
    if kw_count < 3:
        issues.append(
            _static_rule(
                "keywords_count_low",
                f"关键词过少（当前{kw_count}个，要求3-5个）。请补充到3-5个核心技术关键词。",
            )
        )
    elif kw_count > 5:
        issues.append(
            _static_rule(
                "keywords_count_high",
                f"关键词过多（当前{kw_count}个，要求3-5个）。请精选最能代表论文核心的3-5个关键词。",
            )
        )

    # ── 6. 章节结构完整性检查 ────────────────────────────────
    required_ids = {"s1", "s2", "s3", "s4", "s5", "s6", "abstract_zh"}
    existing_ids = {s.section_id for s in manuscript.sections}
    missing = required_ids - existing_ids
    if missing:
        missing_titles = {"s1": "引言", "s2": "相关工作", "s3": "系统设计",
                          "s4": "系统实现", "s5": "结果分析", "s6": "结论",
                          "abstract_zh": "中文摘要"}
        missing_names = [missing_titles.get(m, m) for m in missing]
        issues.append(
            _static_rule(
                "missing_chapters",
                f"缺少必要章节：{', '.join(missing_names)}。"
                "请确保论文包含所有规定章节。",
            )
        )

    # ── 7. 正文内引用检查（数字格式）────────────────────────
    author_year_pattern = re.findall(
        r"\([A-Z][a-z]+(?:\s+et\s+al\.)?,\s*\d{4}\)", full_text
    )
    if author_year_pattern[:3]:
        issues.append(
            _static_rule(
                "author_year_citation_format",
                f"【引用格式】发现 {len(author_year_pattern)} 处作者-年份引用格式"
                f"（如 {author_year_pattern[0]}）。"
                "毕业论文应统一使用数字格式 [1]，置于标点符号之前。请全部替换。",
            )
        )

    # 检查引用标记在标点之后（如"方法。[1]"）
    citation_after_punct = re.findall(r"[。；！？][^\n]*?\[\d+\]", full_text)
    if citation_after_punct:
        sample = citation_after_punct[0][:30]
        issues.append(
            _static_rule(
                "citation_after_punct",
                f"【引用位置】发现 {len(citation_after_punct)} 处引用标记在标点之后"
                f"（如：{sample!r}）。"
                "规范要求：引用标记应在句末标点之前，如'……方法[1]。'",
            )
        )

    # 检查正文引用序号是否超出文献池范围（如 [7] 不存在）
    cited_nums = {int(m) for m in re.findall(r"\[(\d+)\]", full_text) if m.isdigit()}
    ref_count = len(store.all_refs())
    missing_refs = sorted([n for n in cited_nums if n > ref_count])[:10]
    if missing_refs:
        sample = "、".join(f"[{n}]" for n in missing_refs[:5])
        issues.append(
            _static_rule(
                "citation_missing_ref",
                f"【引用缺失】正文引用了文献 {sample}"
                f" 等 {len([n for n in cited_nums if n > ref_count])} 个不存在的序号"
                f"（文献池共 {ref_count} 条）。"
                "请删除无效引用或补全对应文献。",
            )
        )

    # ── 7b. 引用范围越界检查（仅允许在 citation_enabled_sections 中引用）──
    from ..config import get as _cfg_get
    _enabled_sections = set(_cfg_get("citation_enabled_sections") or ["s1", "s2"])
    for sec in manuscript.sections:
        if sec.section_id in ("abstract_zh", "abstract_en"):
            continue  # abstract_citation_markers 已处理
        if sec.section_id in _enabled_sections:
            continue
        if sec.section_id in ("acknowledgment", "keywords", "refs"):
            continue
        if re.search(r"\[\d+\]", sec.markdown_body):
            issues.append(
                _static_rule(
                    "citation_out_of_scope",
                    f"【引用越界】{sec.title}正文中出现了引用标记。"
                    f"引用仅允许在以下章节出现：{', '.join(sorted(_enabled_sections))}。"
                    "请删除该章节中的所有引用标记。",
                )
            )

    # ── 8. 章节越界检查 ────────────────────────────────────
    # 检查章节 s1 正文中是否出现 ### 2. 开头的子标题（LLM 越界写入）
    for sec in manuscript.sections:
        sid = sec.section_id
        if not sid.startswith("s") or not sid[1:].isdigit():
            continue
        chapter_num = int(sid[1:])
        next_num = chapter_num + 1
        overflow_pat = re.compile(
            rf"(?m)^#{1,3}\s+{next_num}\.\d"
            rf"|^##\s+第{next_num}章"
            rf"|^#{1,3}\s+第\s*{next_num}\s*章"
        )
        overflow_md = overflow_pat.search(sec.markdown_body)
        tail_frac = float(get("section_overflow_tail_scan_fraction", 0.3))
        tail_frac = min(0.95, max(0.05, tail_frac))
        ts = int(len(sec.markdown_body) * (1.0 - tail_frac))
        para_ov = False
        para_pat_chk = re.compile(r"(?m)^(\d+)\.(\d+)\s+")
        for pm in para_pat_chk.finditer(sec.markdown_body):
            if pm.start() < ts:
                continue
            try:
                mj = int(pm.group(1))
            except ValueError:
                continue
            if mj > chapter_num and mj <= 20:
                para_ov = True
                break
        if overflow_md or para_ov:
            issues.append(
                _static_rule(
                    f"section_overflow:{sid}",
                    f"【结构】{sec.title}正文中检测到第{chapter_num + 1}章及以后的结构痕迹"
                    f"（Markdown 标题、或**末节**行首小节编号如「{chapter_num + 1}.1」）。"
                    f"原因：LLM 越界写入了下一章内容。"
                    f"请删除{sec.title}中自越界处起的后续内容。",
                )
            )

    # ── 9. 章节截断检查 ────────────────────────────────────
    # 检查章节末尾是否为不完整句子（以"首先"/"其次"/"然后"/"因此"/"此外"等关联词结尾）
    truncation_endings = ["首先", "其次", "然后", "因此", "此外", "另外", "同时", "其中", "为了"]
    for sec in manuscript.sections:
        if sec.section_id in ("refs", "keywords"):
            continue
        body = sec.markdown_body.strip()
        if body and len(body) > 100:  # 非空章节
            last_100 = body[-100:]
            for word in truncation_endings:
                # 末尾100字中出现关联词且后面没有完整句子
                if last_100.endswith(word) or last_100.endswith(word + "，"):
                    issues.append(
                        _static_rule(
                            f"truncation:{sec.section_id}",
                            f"【内容截断】{sec.title}末尾可能被截断"
                            f"（结尾为'{last_100[-30:]}'）。请检查该章节是否完整，补全被截断的内容。",
                        )
                    )
                    break

    # ── 10. 关键词英文行检查 ────────────────────────────────
    kw_sec = next((s for s in manuscript.sections if s.section_id == "keywords"), None)
    if kw_sec:
        kw_body = kw_sec.markdown_body
        # 检查 "Keywords:" 行是否含有中文
        kw_en_line = ""
        for line in kw_body.splitlines():
            if line.strip().lower().startswith("keywords"):
                kw_en_line = line
                break
        if kw_en_line and any("\u4e00" <= c <= "\u9fff" for c in kw_en_line):
            issues.append(
                _static_rule(
                    "keywords_line_zh_in_en",
                    "【关键词格式】'Keywords:'行中出现中文字符，这是严重格式错误。"
                    "Keywords行应为纯英文关键词，如'Keywords: Microcontroller; Fuzzy PID Control'。",
                )
            )

    # ── 11. 参考文献质量检查 ────────────────────────────────
    all_refs = store.all_refs()
    max_total = int(get("max_refs_total", 60))
    if len(all_refs) > max_total:
        issues.append(
            _static_rule(
                "refs_over_max",
                f"【参考文献】参考文献数量({len(all_refs)}篇)超过上限({max_total}篇)。"
                f"本科毕业论文建议25-40篇高质量文献，大量低质文献会影响评分。"
                "请删减与主题无关的文献（特别是'佚名'作者、'期刊未知'、非相关领域的文献）。",
            )
        )

    # 佚名作者检查
    anon_refs = [r for r in all_refs if "佚名" in (r.authors[0] if r.authors else "")]
    if len(anon_refs) > 3:
        issues.append(
            _static_rule(
                "anon_refs_excess",
                f"【参考文献质量】发现{len(anon_refs)}篇'佚名'作者文献"
                f"（如：{anon_refs[0].title[:30]}...）。"
                "佚名文献可信度低，请替换为有明确作者的正规期刊/会议论文。",
            )
        )

    # ── 12. 英文关键词缺失检查 ──────────────────────────────
    if not plan.keywords_en:
        issues.append(
            _static_rule(
                "keywords_en_missing",
                "【关键词格式】缺少英文关键词（keywords_en）。"
                "规范要求：关键词部分需同时列出中文关键词和英文关键词（Keywords）。"
                "请在规划阶段提供英文关键词，或手动补充。",
            )
        )

    # ── 13. 摘要/关键词 vs 正文的主控型号一致性 ────────────────
    def _mcu_label_set(text: str) -> set:
        labs = set()
        if re.search(r"STM32[A-Za-z0-9]*", text, re.I):
            labs.add("STM32系")
        if "STC89C52" in text:
            labs.add("STC89C52")
        if "STC89C51" in text:
            labs.add("STC89C51")
        if "AT89C51" in text:
            labs.add("AT89C51")
        if "AT89C52" in text:
            labs.add("AT89C52")
        if re.search(r"(?<![A-Za-z0-9_])ESP32(?![A-Za-z0-9_])", text):
            labs.add("ESP32")
        if re.search(r"(?<![A-Za-z0-9_])ESP8266(?![A-Za-z0-9_])", text):
            labs.add("ESP8266")
        return labs

    front_chunks: List[str] = []
    if abstract_sec:
        front_chunks.append(abstract_sec.markdown_body)
    kw_sec_chk = next((s for s in manuscript.sections if s.section_id == "keywords"), None)
    if kw_sec_chk:
        front_chunks.append(kw_sec_chk.markdown_body)
    front_mcu = _mcu_label_set("\n".join(front_chunks))
    body_mcu = _mcu_label_set(
        "\n".join(
            s.markdown_body
            for s in manuscript.sections
            if s.section_id
            not in ("abstract_zh", "abstract_en", "keywords", "refs")
        )
    )
    if front_mcu and body_mcu:
        missing_from_body = front_mcu - body_mcu
        if missing_from_body:
            issues.append(
                _static_rule(
                    "mcu_abstract_body_mismatch",
                    "【型号一致性】摘要/关键词中出现的主控相关表述为："
                    f"{', '.join(sorted(front_mcu))}；正文主要为：{', '.join(sorted(body_mcu))}。"
                    f"摘要/关键词中存在正文未出现的型号（{', '.join(sorted(missing_from_body))}），"
                    "请全文统一为一种硬件平台，并同步修改摘要、关键词与各章节。",
                )
            )

    # ── 14. 生成失败或截断占位标记 ────────────────────────────
    bad_marks = (
        "（章节中段省略）",
        "（本部分生成失败",
        "（本章节生成失败",
        "（摘要生成失败",
        "...（已截断）",
    )
    for sec in manuscript.sections:
        for mark in bad_marks:
            if mark in sec.markdown_body:
                issues.append(
                    _static_rule(
                        f"placeholder_residual:{sec.section_id}",
                        f"【占位/失败残迹】{sec.title}中含有「{mark}」等标记，"
                        "须改写为正式内容或重新生成该节。",
                    )
                )
                break

    # ── 14b. 小节缺失检查（对比 plan.subsections 与实际正文 ### 标题） ──
    import re as _re_sub
    from ..writing.scope_enforce import flatten_subsections_depth_first as _flat_sub
    for sec in manuscript.sections:
        if sec.section_id not in ("s1", "s2", "s3", "s4", "s5", "s6"):
            continue
        node = next((s for s in plan.outline if s.section_id == sec.section_id), None)
        if not node or not node.subsections:
            continue
        flat = _flat_sub(node.subsections)
        body_h = {h.strip() for h in _re_sub.findall(r"^###\s+(.+)$", sec.markdown_body, _re_sub.MULTILINE)}
        missing = []
        for sub in flat:
            plan_clean = _re_sub.sub(r"[^\w\u4e00-\u9fff]", "", sub.title)
            if not plan_clean:
                continue
            if not any(
                plan_clean in _re_sub.sub(r"[^\w\u4e00-\u9fff]", "", h)
                or _re_sub.sub(r"[^\w\u4e00-\u9fff]", "", h) in plan_clean
                for h in body_h
            ):
                missing.append(sub.title)
        if missing:
            issues.append(
                _static_rule(
                    "missing_subsections",
                    f"【小节缺失】{sec.title}中缺少以下小节（共{len(missing)}个）："
                    f"{'、'.join(missing[:6])}"
                    f"{'等' if len(missing) > 6 else ''}。请补写这些小节内容。",
                )
            )

    # ── 15. 标点缺失检查 ──────────────────────────────────
    missing_punct_count = 0
    for sec in manuscript.sections:
        if sec.section_id == "abstract_en":
            continue
        for line in sec.markdown_body.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            if re.match(r"^[\-\*\•]|\d+[\.\、\)]", stripped):
                continue
            if "$" in stripped or "|" in stripped:
                continue
            cn_chars = len(re.findall(r"[\u4e00-\u9fff]", stripped))
            if cn_chars < 20:
                continue
            if re.search(r"[。！？\.!\?][\"\'\"』]?\s*$", stripped):
                continue
            if re.search(r"\[\d+(?:[,，]\d+)*\]\s*$", stripped):
                continue
            missing_punct_count += 1
    if missing_punct_count >= 2:
        issues.append(
            _static_rule(
                "missing_punct",
                f"【标点缺失】全文约 {missing_punct_count} 处疑似缺少句末标点"
                "（中文字符>20且末尾无句号/问号/感叹号的非列表行）。"
                "请逐句检查并补全句号。",
            )
        )

    # ── 16. 中英文标点混用检查 ─────────────────────────────
    mixed_count = 0
    for sec in manuscript.sections:
        if sec.section_id in ("abstract_en",):
            continue
        # 中文正文中出现英文句点 . 后跟空格+大写字母（典型的英文句子结尾混入）
        mixed_cn = re.findall(r"[\u4e00-\u9fff]\s*\.(?:\s|\n)", sec.markdown_body)
        if mixed_cn:
            mixed_count += len(mixed_cn)
    if mixed_count >= 3:
        issues.append(
            _static_rule(
                "mixed_punctuation",
                f"【标点混用】发现约 {mixed_count} 处中文正文中的英文句点（.），"
                "应统一为中文句号（。）。请将中文叙述中的英文句点替换为中文句号。",
            )
        )

    # ── 17. 连续重复标点检查 ───────────────────────────────
    dup_count = len(re.findall(r"[。，；：！？、]{2,}", full_text))
    if dup_count >= 3:
        issues.append(
            _static_rule(
                "double_punctuation",
                f"【标点重复】发现约 {dup_count} 处连续重复标点（如。。或，；），"
                "请检查并清理冗余标点。",
            )
        )

    # ── 18. 结论-引言回应对照检查 ─────────────────────────────
    s1_sec = next((s for s in manuscript.sections if s.section_id == "s1"), None)
    s6_sec = next((s for s in manuscript.sections if s.section_id == "s6"), None)
    s1_node = next((n for n in plan.outline if n.section_id == "s1"), None)
    if s1_sec and s6_sec and s1_node and s1_node.bullets:
        problem_bullets = [b for b in s1_node.bullets
                          if any(kw in b for kw in ["问题", "不足", "挑战", "缺陷", "滞后", "缺乏"])]
        if not problem_bullets:
            problem_bullets = [b for b in s1_node.bullets[-3:]
                              if not any(mw in b for mw in ["本文工作", "论文结构", "章节安排", "结构安排", "组织架构", "贡献", "论文安排"])]
        if problem_bullets:
            s6_text = s6_sec.markdown_body
            # 对每个问题 bullet，检查是否至少有 1 个 ≥4 字的片段在 s6 中出现
            responded = 0
            for b in problem_bullets:
                for i in range(len(b) - 3):
                    for j in range(i + 4, min(i + 10, len(b) + 1)):
                        sub = b[i:j]
                        if sub in s6_text:
                            responded += 1
                            break
                    else:
                        continue
                    break
            ratio = responded / len(problem_bullets)
            if ratio < 0.5:
                issues.append(
                    _static_rule(
                        "conclusion_intro_gap",
                        f"【逻辑】第6章结论未充分回应第1章引言提出的问题"
                        f"（问题要点 {len(problem_bullets)} 个，仅回应 {responded} 个，回应率 {ratio:.0%}）。"
                        f"问题包括：{'；'.join(problem_bullets[:3])}{'…' if len(problem_bullets) > 3 else ''}。"
                        "请逐一回应并说明本文工作如何解决或改善这些不足。",
                    )
                )

    return issues


# ── 主 Prompt 构建 ────────────────────────────────────────────

def _build_eval_prompt(
    manuscript: Manuscript,
    plan: WritingPlan,
    user_requirement: str,
    thesis_mode: bool,
) -> str:
    """
    构造评估提示。
    毕业论文模式：使用"章节摘要"方式，每章只取前500字+末尾100字，
    让 LLM 能看到所有章节的开头和结尾（方便检测截断和越界），
    同时控制总长度。
    """
    if thesis_mode:
        # 每章摘要：开头 400 字 + 结尾 100 字（用于检测截断/越界）
        section_summaries = []
        for sec in manuscript.sections:
            body = sec.markdown_body
            if len(body) > 600:
                snippet = body[:400] + "\n...(中间省略)...\n" + body[-100:]
            else:
                snippet = body
            section_summaries.append(
                f"### [{sec.section_id}] {sec.title}（共约{len(body)}字）\n{snippet}"
            )
        full_text = "\n\n".join(section_summaries)
    else:
        full_text = manuscript.to_markdown()
        max_chars = 6000
        if len(full_text) > max_chars:
            full_text = full_text[:max_chars] + f"\n\n...（已截断）"

    outline_summary = "\n".join(
        f"- {s.section_id}: {s.title}" for s in plan.outline
    )
    kw_sep = "；" if thesis_mode else "、"
    kw_text = kw_sep.join(plan.keywords[:5])
    kw_en_text = "; ".join(plan.keywords_en[:5]) if plan.keywords_en else "（缺失）"

    return (
        f"## 用户研究需求\n{user_requirement[:500]}\n\n"
        f"## 预期大纲\n{outline_summary}\n\n"
        f"## 中文关键词\n{kw_text}\n"
        f"## 英文关键词\n{kw_en_text}\n\n"
        f"## 各章节内容摘要（开头400字+末尾100字）\n{full_text}\n\n"
        f"## 评估任务\n"
        "请严格按照评分细则和常见错误清单，找出所有实质性缺陷，输出 JSON 格式评估结果。"
        "重点检查：章节越界（s1正文含s2内容）、内容截断、型号前后矛盾、关键词格式。"
    )


# ── 降级默认分 ─────────────────────────────────────────────────

_CONSERVATIVE_DEFAULT = Evaluation(
    score_total=5.0,
    dimensions=EvaluationDimensions(
        structure=5.0, logic=5.0, language=5.0, alignment=5.0
    ),
    feedback="评估服务暂时不可用，使用保守默认分。请手动检查论文质量。",
    actionable_items=["请人工审阅论文结构与内容"],
)


# ── 主入口 ────────────────────────────────────────────────────

def evaluate(
    manuscript: Manuscript,
    plan: WritingPlan,
    user_requirement: str,
    store: ReferenceStore,
) -> Evaluation:
    """
    对论文草稿进行质量评估，返回经校验的 Evaluation 对象。
    毕业论文模式：先做规则性静态检查，再调 LLM 评分；
    静态检查问题追加到 actionable_items，并写入 static_rule_issues（非 thesis 模式为空列表）。
    """
    thesis_mode = bool(get("thesis_mode", False))

    # ── 静态规则检查（毕业论文模式）
    static_issues: List[StaticRuleIssue] = []
    if thesis_mode:
        static_issues = _check_thesis_rules(manuscript, plan, store)
        if static_issues:
            n_err = sum(1 for s in static_issues if s.severity == "error")
            n_warn = sum(1 for s in static_issues if s.severity == "warning")
            logger.info(
                "静态规则检查发现 %d 个问题（error=%d, warning=%d）",
                len(static_issues), n_err, n_warn,
            )

    static_messages = [s.message for s in static_issues]

    # ── LLM 评分
    system_prompt = _SYSTEM_EVAL_THESIS if thesis_mode else _SYSTEM_EVAL_NORMAL
    prompt = _build_eval_prompt(manuscript, plan, user_requirement, thesis_mode)
    messages = build_messages(system_prompt, prompt)

    raw = {}
    try:
        raw = chat_json(messages, temperature=0.2)
    except Exception as e:
        logger.error("评估 LLM 调用失败（prompt 估算 %d 字符）: %s，使用默认分",
                     len(system_prompt) + len(prompt), e)
        base = _CONSERVATIVE_DEFAULT
        return base.model_copy(
            update={
                "actionable_items": static_messages or list(base.actionable_items),
                "static_rule_issues": list(static_issues),
            }
        )

    if not raw:
        logger.warning("评估返回空数据，使用默认分")
        base = _CONSERVATIVE_DEFAULT
        return base.model_copy(
            update={
                "actionable_items": static_messages or list(base.actionable_items),
                "static_rule_issues": list(static_issues),
            }
        )

    # ── Pydantic 校验
    try:
        if "score_total" in raw:
            try:
                raw["score_total"] = float(raw["score_total"])
            except (TypeError, ValueError):
                raw["score_total"] = 5.0

        dims_raw = raw.get("dimensions", {})
        if isinstance(dims_raw, dict):
            for k in ("structure", "logic", "language", "alignment"):
                try:
                    dims_raw[k] = float(dims_raw.get(k, 5.0))
                except (TypeError, ValueError):
                    dims_raw[k] = 5.0
            raw["dimensions"] = dims_raw

        # 合并：静态检查问题优先排在前面
        llm_items = raw.get("actionable_items", [])
        if not isinstance(llm_items, list):
            llm_items = []
        llm_items = dedupe_llm_against_static(static_issues, llm_items)
        all_items = static_messages + [
            item for item in llm_items if item not in static_messages
        ]
        raw["actionable_items"] = all_items
        raw.pop("static_rule_issues", None)  # 不允许评估 LLM 覆盖静态规则结果

        err_n = sum(1 for s in static_issues if s.severity == "error")
        if err_n >= 3:
            raw["score_total"] = max(3.0, raw["score_total"] - err_n * 0.3)

        evaluation = Evaluation.model_validate(raw)
        evaluation = evaluation.model_copy(update={"static_rule_issues": list(static_issues)})
        logger.info("评估完成：总分 %.1f（静态问题 %d 条）",
                    evaluation.score_total, len(static_issues))
        return evaluation

    except ValidationError as e:
        logger.error("Evaluation schema 校验失败: %s，使用默认分", e)
        base = _CONSERVATIVE_DEFAULT
        return base.model_copy(
            update={
                "actionable_items": static_messages or list(base.actionable_items),
                "static_rule_issues": list(static_issues),
            }
        )
