"""
初稿引擎 — 按 WritingPlan 逐章生成论文初稿。

包含 System Prompt、prompt 构建、分块生成、多候选择优、scope 重试、子节串行。
依赖 helpers / postprocess / abstract，不依赖 revision_engine（无循环导入）。
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

from ..config import get
from ..llm import chat, build_messages
from ..models import Manuscript, ManuscriptSection, SectionNode, ThesisConfig, WritingPlan
from ..ref_store import ReferenceStore
from .abstract import _generate_abstract_from_body
from .helpers import (
    _CHUNK_TARGET_WORDS,
    _CHUNK_TEMPERATURE,
    _CHUNK_WORD_THRESHOLD,
    _EXECUTION_PROTOCOL,
    _build_chapter_chain_context,
    _build_ref_context,
    _build_ref_list_section,
    _build_sensor_checklist,
    _chunk_body_looks_truncated,
    _ensure_subsections_present,
    _executable_outline_prompt_section,
    _filter_tech_spec_for_section,
    _format_keywords_text,
    _format_prev_chapter_excerpt,
    _get_section_rule,
    _get_section_target_words,
    _is_thesis_mode,
    _neighbor_chapter_boundary_hint,
    _split_bullets_into_chunks,
)
from .locked_tech_spec import load_locked_tech_spec, locked_layer_nonempty, merge_tech_specs
from .multi_candidate import (
    llm_pick_best_candidate,
    multi_candidate_settings,
    rule_prefilter_candidates,
    section_uses_multi_candidate,
)
from .postprocess import _clean_section_overflow, _fix_citation_position
from .scope_enforce import (
    flatten_subsections_depth_first,
    merge_outline_scope_overrides,
    scope_validation_settings,
    section_has_scope_constraints,
    subsections_sequential_settings,
    validate_section_body_scope,
)
from .tech_spec import generate_tech_spec, format_tech_spec_for_prompt

logger = logging.getLogger(__name__)

# ── Prompt 常量 ───────────────────────────────────────────────

_SYSTEM_DRAFT_NORMAL = """你是一位学术论文写作专家，擅长撰写严谨、逻辑清晰的中文学术论文。
你将根据提供的章节要求和参考文献池，撰写指定章节的 Markdown 正文。

规则：
1. 只能引用"可用参考文献"列表中存在的文献，不得虚构
2. 引用格式：{citation_style_desc}
3. 语言：中文学术风格，避免口语化
4. 章节内容须覆盖要点列表中的所有要点
5. 正文长度：800–2000 字（摘要 500–800 字，结论 400–800 字）
6. 不要输出章节标题（已由框架处理），直接从正文内容开始
"""

_SYSTEM_DRAFT_THESIS = """你是一位兼具本科毕业论文答辩评委（评审过300+本）与学术写作指导专家双重身份的写作专家。
你对逻辑漏洞和语言瑕疵零容忍，同时坚持学术规范的最高标准。
你将根据提供的章节要求、写作规范和参考文献池，撰写指定章节的正文。

【通用强制规则】
1. 引用：只引用"可用参考文献"列表中的文献，用序号[数字]标注，置于标点符号之前
   正确：……已被广泛应用[1]。  错误：……已被广泛应用。[1]
   重要：描述本文自身的设计目标、预期效果、系统参数时不得添加引用标记——引用仅用于引述他人工作
2. 语言：全文统一使用"本文"指代本研究，禁止使用"该文""本研究""笔者"
3. 句子长度：单句不超过80字，超长拆分
4. 格式：不输出章节大标题，直接从正文开始

【引用范围规则（违规将导致扣分）】
- 引用标记 [n] 仅允许在引言(s1)和相关工作/文献综述(s2)章节出现
- s3/s4/s5/s6（总体设计/硬件软件/实现调试/结论）正文中**绝对禁止**出现任何 [n] 引用标记
- 若本章"可用参考文献"为空，则说明本章不应引用——正文中不得出现任何 [n]
- 摘要(abstract_zh/abstract_en)禁止引用

【标点规范（违规将导致扣分）】
- 每个完整陈述句必须以句号（。）、问号（？）或感叹号（！）结尾，不得漏写标点
- 禁止使用英文句点（.）作为中文句子的结尾，必须使用中文句号（。）
- 禁止连续两个相同标点（如"。。"或"，，"）
- 分号（；）和冒号（：）不能作为句子结尾
- 引用标记 [n] 必须放在句末标点之前，如"……方法[1]。"
- 中英文标点不可混用（如"中文句号."或"English,中文"）

【图表与数据规则（重要）】
- 正文中需要放图表时，用以下格式占位，不要虚构数据：
  图片：【图X-X 图题：简要描述图的内容】
  表格：【表X-X 表题：简要描述表的内容 | 表格数据由实验实测后填入】
- 引用占位图时写"如图X-X所示"，引用公式时写"如式(X-X)所示"（不写"如公式"）
- 禁止编造具体的实验数据（如响应时间、准确率等），使用"[实测数据]"占位

【公式规则】
- 每个公式后必须逐一解释所有变量的含义和单位
- 引用公式格式：如式(X-X)所示

【标题格式（强制）】
- 正文必须使用 ### 二级标题和 #### 三级标题组织内容，不得产出无小节结构的长文
- 小节结构以「强制小节结构」中列出的为准，不得少写、不得另起计划外大节
- 二级标题：### X.X 标题（如 ### 3.1 总体架构）
- 三级标题：#### X.X.X 标题（如 #### 3.1.1 硬件选型）
- 每个标题下必须先有一段过渡文字，然后才能继续子标题
- 代码块前后必须有解释性文字

【小节归属规则（强制）】
- 每个 ### 小节的内容必须直接属于其所在 ## 章的主题范围，不得跑题
- 若某个小节的内容可以作为独立的一章存在，说明该小节写错了位置
- 判断标准：删掉小节标题后，读者能否从内容本身判断它属于当前章的主题？
- 小节第一句必须声明该部分在整章论证链条中的位置
  （如"本节设计 DQN 的网络结构，作为本章模型设计的第一部分"）
- 禁止将「环境/场景/平台描述」作为独立小节展开——它应是设计/方法章节的简短前置，
  而非内容主体

【一致性规则】
- 章节内对同一技术/模块的称呼要一致（如始终叫"STM32F103C8T6"，不能忽而"STM32"忽而"ARM微控制器"）
- 全文型号必须以 TechSpec（技术规范文档）为准，不得自创或改写型号名称
- 所有功能描述要与前文（引言、总体设计）保持一致，不得在不同章节给出矛盾的数据
- 致谢章节中不得出现新文献引用；文献引用仅限在前文（引言、相关工作章节）使用

【写作风格（去 AI 味）】
- 禁止使用"首先/其次/然后/最后"作为段落级连接词，用自然语义过渡替代
- 同段内"值得注意的是""总而言之""此外""与此同时"等模板化过渡词至多出现1次
- 拒绝"本文首先介绍了...然后分析了...接着设计了...最后验证了..."的流水账句式
- 每个段落一个核心观点，用主题句开头，展开句支撑
""" + _EXECUTION_PROTOCOL


# ── 引用描述 ─────────────────────────────────────────────────

def _citation_desc_for_thesis() -> str:
    return (
        "数字序号格式：在句子中引用时用 [序号]，"
        "如'……研究表明[1]，……'或'……已有方法[1,2]存在……'。"
        "引用标记放在被引句末标点符号之前。"
        "只引用参考文献列表中已有的文献，禁止编造。"
    )


def _citation_style_desc(style: str) -> str:
    if style == "numeric":
        return _citation_desc_for_thesis()
    return "作者-年份格式，如 (Smith, 2020)、(Zhang et al., 2021)"


# ── Prompt 构建 ───────────────────────────────────────────────

def _build_draft_prompt(
    section: SectionNode,
    plan: WritingPlan,
    ref_context: str,
    thesis_mode: bool,
    thesis_cfg: Optional["ThesisConfig"] = None,
    tech_spec_text: str = "",
    prev_chapter_excerpt: str = "",
) -> str:
    bullets_text = "\n".join(f"- {b}" for b in section.bullets) or "（无具体要点，请自行发挥）"
    kw_sep = "；" if thesis_mode else "、"
    kw_text = kw_sep.join(plan.keywords[:5]) if plan.keywords else "（无）"

    special_rule = _get_section_rule(section.section_id)
    target_words = _get_section_target_words(section.section_id)
    word_hint = f"\n目标字数：约 {target_words} 字（中文字数，摘要严格控制，正文章节可适当超出）"

    parts = []

    # ── 全文主线
    if thesis_mode and section.section_id.startswith("s") and section.section_id[1:].isdigit():
        ch_num = int(section.section_id[1:])
        all_chapters = [s for s in plan.outline if s.section_id.startswith("s") and s.section_id[1:].isdigit()]
        all_chapters.sort(key=lambda s: int(s.section_id[1:]))
        if all_chapters:
            backbone_parts: list[str] = []
            for s in all_chapters:
                ch = int(s.section_id[1:])
                label = s.title.replace(f"第{ch}章", "").strip()
                if not label or not any("一" <= c <= "鿿" for c in label):
                    label = f"Chapter {ch}"
                backbone_parts.append(f"第{ch}章{label[:8]}")
            backbone = " → ".join(backbone_parts)
        else:
            backbone = "第1章 → 第2章 → 第3章 → 第4章 → 第5章 → 第6章"
        parts.append(
            f"**论文主线**：{backbone}。"
            f"你当前撰写第{ch_num}章，请确保内容服务于这条主线。\n"
        )

    # TechSpec
    if tech_spec_text:
        filtered_ts = _filter_tech_spec_for_section(tech_spec_text, section.section_id)
        if filtered_ts:
            parts.append(filtered_ts)

    parts += [
        f"## 章节信息",
        f"章节标题：{section.title}",
        f"章节编号（section_id）：{section.section_id}",
        f"论文关键词：{kw_text}",
        word_hint,
    ]

    if special_rule:
        parts.append(f"\n{special_rule}")

    # 跨章上下文
    if thesis_mode and section.section_id.startswith("s") and section.section_id[1:].isdigit():
        chain_ctx = _build_chapter_chain_context(section.section_id, plan, prev_chapter_excerpt)
        if chain_ctx:
            parts.append(chain_ctx)

    # 传感器测试覆盖清单
    sensor_checklist_sections = get("sensor_checklist_sections") or ["s5"]
    if section.section_id in sensor_checklist_sections and tech_spec_text:
        sensor_checklist = _build_sensor_checklist(tech_spec_text)
        if sensor_checklist:
            parts.append(sensor_checklist)

    parts.append(f"\n## 章节要点（必须覆盖）\n{bullets_text}")

    exe_block = _executable_outline_prompt_section(section)
    if exe_block:
        parts.append(exe_block)

    nb_hint = _neighbor_chapter_boundary_hint(plan, section)
    if nb_hint:
        parts.append(nb_hint)

    if (prev_chapter_excerpt or "").strip():
        parts.append((prev_chapter_excerpt or "").strip())

    parts += [
        f"\n## 可用参考文献（序号对应正文引用 [序号]）",
        ref_context,
    ] if ref_context else []
    parts += [
        f"\n## 写作任务",
        "请撰写该章节的正文（使用 Markdown 格式），直接从内容开始，不要输出章节大标题。",
    ]
    if section.subsections:
        flat_subs = flatten_subsections_depth_first(section.subsections)
        if flat_subs:
            sub_list = "\n".join(f"  ### {sub.title}" for sub in flat_subs)
            parts.append(
                f"\n【强制】本章正文必须包含以下小节标题（### 开头），按顺序逐一展开，"
                f"不得跳过或合并：\n{sub_list}"
            )
            # ── 小节-章节主题对齐声明 ──
            ch_num = int(section.section_id[1:]) if section.section_id[1:].isdigit() else 0
            chapter_topic = section.title
            if ch_num:
                chapter_topic = chapter_topic.replace(f"第{ch_num}章 ", "").strip()
            align_lines = [
                "\n【小节-章节主题对齐（强制）】",
                f"本章总主题：「{chapter_topic}」。以下每个小节必须直接服务于该主题：",
            ]
            for sub in section.subsections:
                align_lines.append(
                    f"- {sub.title} → 该小节的第一句话必须说明本部分"
                    f"在「{chapter_topic}」中的角色与必要性"
                )
            align_lines.extend([
                "禁止将本可独立成章的内容降级为一个小节"
                "（如将'通信环境建模'写成独立小节，放在'模型设计'章下）",
                "若某个小节的内容已构成独立主题领域，应重新审视其章节归属，而非强行塞入当前章",
            ])
            parts.append("\n".join(align_lines))

    if thesis_mode and section.section_id not in ("abstract_zh", "abstract_en", "acknowledgment"):
        parts.append(
            "正文中的图表请用【图X-X 图题：...】占位；"
            "每个二级标题（### 开头）下必须有至少一段正文再进入三级标题。"
        )

    # 引用范围提示
    enabled = get("citation_enabled_sections") or ["s1", "s2"]
    if thesis_mode and section.section_id not in enabled and section.section_id not in ("abstract_zh", "abstract_en", "acknowledgment"):
        parts.append(
            "\n⚠ **本章禁止引用**：本章描述自己的设计/实现/调试/结论，"
            "正文中不得出现任何 [1] 等引用标记。"
        )

    # 关键约束：只写当前章节
    if section.section_id.startswith("s") and section.section_id[1:].isdigit():
        chapter_num = int(section.section_id[1:])
        next_num = chapter_num + 1
        parts.append(
            f"\n【重要】只写第{chapter_num}章的内容，"
            f"绝对不要输出'第{next_num}章'或'### {next_num}.'等内容，写完本章即停止。"
        )

    return "\n".join(parts)


# ── 分块生成 ─────────────────────────────────────────────────

def _draft_section_chunked(
    section: SectionNode,
    plan: WritingPlan,
    system_prompt: str,
    ref_context: str,
    thesis_mode: bool,
    thesis_cfg,
    tech_spec_text: str = "",
    prev_chapter_excerpt: str = "",
) -> str:
    """大章节分块生成策略。"""
    bullets = section.bullets or ["（按研究主题展开）"]
    chunks = _split_bullets_into_chunks(bullets, chunk_size=2)

    results = []
    prev_ending = ""

    for idx, chunk_bullets in enumerate(chunks):
        is_first = idx == 0
        is_last = idx == len(chunks) - 1

        chunk_node = SectionNode(
            section_id=section.section_id,
            title=section.title,
            bullets=chunk_bullets,
            outline_detail=section.outline_detail,
            scope_must_include=section.scope_must_include,
            scope_forbidden=section.scope_forbidden,
            subsections=section.subsections,
        )

        chunk_prompt = _build_draft_prompt(
            chunk_node, plan, ref_context, thesis_mode, thesis_cfg,
            tech_spec_text=tech_spec_text,
            prev_chapter_excerpt=prev_chapter_excerpt,
        )

        chunk_info_lines = [
            f"\n## 分块写作说明",
            f"本章共 {len(chunks)} 块，当前为第 {idx+1}/{len(chunks)} 块。",
        ]
        if not is_first and prev_ending:
            chunk_info_lines.append(
                f"【上一块结尾】（请自然衔接，不要重复）：\n{prev_ending}"
            )
        if is_first:
            chunk_info_lines.append("请先写好本块开头（章节引入段），然后展开要点。")
        if is_last:
            chunk_info_lines.append("这是最后一块，请在结尾写一段小结，引出下一章。")
        else:
            chunk_info_lines.append("写完本块后不要写小结，直接结束，由后续块继续。")
        if tech_spec_text:
            chunk_info_lines.append(
                "【一致性】须与上文及技术规范中的硬件型号、通信协议、关键参数等保持一致，不得擅自改写。"
            )

        chunk_prompt += "\n".join(chunk_info_lines)

        max_tok = min(3500, max(1200, int(_CHUNK_TARGET_WORDS * 1.8)))

        try:
            chunk_body = chat(
                build_messages(system_prompt, chunk_prompt),
                temperature=_CHUNK_TEMPERATURE,
                max_tokens=max_tok,
            )
            if not chunk_body.strip():
                raise ValueError("空内容")

            if is_last and _chunk_body_looks_truncated(chunk_body):
                tail = chunk_body[-500:]
                cont_prompt = (
                    "上文可能在句末被截断。请**仅输出续写**：补完未结束的句子并自然收束（1～4句），"
                    "不要重复上文，不要小标题：\n\n" + tail
                )
                try:
                    cont = chat(
                        build_messages(system_prompt, cont_prompt),
                        temperature=0.35,
                        max_tokens=min(1200, max(600, max_tok // 2)),
                    )
                    if cont.strip():
                        chunk_body = chunk_body.rstrip() + "\n" + cont.strip()
                        logger.info("  块 %d/%d 已尝试结尾续写", idx + 1, len(chunks))
                except Exception as e2:
                    logger.warning("  块 %d/%d 结尾续写失败: %s", idx + 1, len(chunks), e2)

            prev_ending = chunk_body[-200:].replace("\n", " ")
            results.append(chunk_body)
            logger.info("  块 %d/%d 生成成功（%d 字）", idx + 1, len(chunks), len(chunk_body))

        except Exception as e:
            logger.warning("  块 %d/%d 生成失败: %s，使用占位", idx + 1, len(chunks), e)
            fallback = f"\n（本部分生成失败，请手动补写：{', '.join(chunk_bullets)}）\n"
            results.append(fallback)

    return "\n\n".join(results)


# ── 多候选初稿 ───────────────────────────────────────────────

def _draft_section_body_multi_candidate(
    section: SectionNode,
    plan: WritingPlan,
    system_prompt: str,
    ref_context: str,
    thesis_mode: bool,
    thesis_cfg: Optional[ThesisConfig],
    tech_spec_text: str,
    mc: dict,
    temp: float,
    target_words: int,
    prev_chapter_excerpt: str = "",
) -> str:
    """多候选初稿：同一 prompt 多次采样 → 规则预筛 → LLM 打分取最优。"""
    user_prompt = _build_draft_prompt(
        section, plan, ref_context, thesis_mode, thesis_cfg,
        tech_spec_text=tech_spec_text,
        prev_chapter_excerpt=prev_chapter_excerpt,
    )
    messages = build_messages(system_prompt, user_prompt)
    max_tok = min(8000, max(1500, int(target_words * 1.8)))
    n = int(mc["n"])
    jitter = float(mc["temperature_jitter"])

    def _one(k: int) -> str:
        t = min(0.95, max(0.15, temp + k * jitter))
        try:
            out = chat(messages, temperature=t, max_tokens=max_tok)
            return (out or "").strip()
        except Exception as e:
            logger.warning("章节 [%s] 多候选 #%d 生成失败: %s", section.title, k, e)
            return ""

    bodies: List[str] = []
    if mc.get("parallel"):
        max_workers = max(1, min(n, 4))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs = {pool.submit(_one, k): k for k in range(n)}
            pairs: list[tuple[int, str]] = []
            for fu in as_completed(futs):
                k = futs[fu]
                b = fu.result()
                if b:
                    pairs.append((k, b))
            pairs.sort(key=lambda x: x[0])
            bodies = [p[1] for p in pairs]
    else:
        for k in range(n):
            b = _one(k)
            if b:
                bodies.append(b)

    if not bodies:
        logger.warning("章节 [%s] 多候选全部失败，尝试单次生成", section.title)
        try:
            one = chat(messages, temperature=temp, max_tokens=max_tok)
            if one.strip():
                return one.strip()
        except Exception as e:
            logger.warning("章节 [%s] 单次生成仍失败: %s", section.title, e)
        return f"（本章节生成失败，请手动补写。要点：{', '.join(section.bullets)}）"

    if mc.get("rule_prefilter", True):
        filtered = rule_prefilter_candidates(bodies, section.section_id, target_words)
        if filtered:
            bodies = filtered
        else:
            logger.warning("章节 [%s] 多候选规则筛空，回退未筛版本", section.title)

    if len(bodies) == 1:
        return bodies[0]

    best_i, scores = llm_pick_best_candidate(
        bodies, section, plan, tech_spec_text, float(mc["llm_score_threshold"]),
    )
    if scores:
        logger.info("章节 [%s] 多候选 LLM 分数: %s", section.title, scores)
    return bodies[best_i]


# ── Scope 重试 ───────────────────────────────────────────────

def _maybe_retry_after_scope_fail(
    body: str,
    section: SectionNode,
    plan: WritingPlan,
    system_prompt: str,
    ref_context: str,
    thesis_mode: bool,
    thesis_cfg: Optional[ThesisConfig],
    tech_spec_text: str,
    temp: float,
    target_words: int,
    max_tok: int,
    prev_chapter_excerpt: str,
) -> str:
    """scope 规则校验失败时最多全文重生成一次。"""
    st = scope_validation_settings()
    if not st["enabled"] or not section_has_scope_constraints(section):
        return body
    ok, issues = validate_section_body_scope(body, section)
    if ok:
        return body
    logger.warning("章节 [%s] scope 未通过：%s", section.title, issues)
    if not st.get("retry_once", True):
        return body
    fix = (
        "\n\n## 系统自动校验反馈（须全文重写本章正文）\n"
        + "\n".join(issues)
        + "\n请严格满足「可执行大纲与边界」与技术规范，输出完整本章 Markdown 正文"
        "（不要只写补充说明或检讨）。\n"
    )
    user_prompt = _build_draft_prompt(
        section, plan, ref_context, thesis_mode, thesis_cfg,
        tech_spec_text=tech_spec_text,
        prev_chapter_excerpt=prev_chapter_excerpt,
    ) + fix
    try:
        body2 = chat(
            build_messages(system_prompt, user_prompt),
            temperature=max(0.35, temp - 0.2),
            max_tokens=max_tok,
        ).strip()
        if body2:
            ok2, issues2 = validate_section_body_scope(body2, section)
            if ok2:
                logger.info("章节 [%s] scope 重试后通过", section.title)
            else:
                logger.warning("章节 [%s] scope 重试后仍有问题：%s", section.title, issues2)
            return body2
    except Exception as e:
        logger.warning("章节 [%s] scope 重试失败: %s", section.title, e)
    return body


# ── 子节串行 ─────────────────────────────────────────────────

def _draft_section_sequential_subsections(
    section: SectionNode,
    plan: WritingPlan,
    system_prompt: str,
    ref_context: str,
    thesis_mode: bool,
    thesis_cfg: Optional[ThesisConfig],
    tech_spec_text: str,
    temp: float,
    target_words: int,
    prev_chapter_excerpt: str,
) -> str:
    """按 subsections 树深度优先，每小节独立调用 LLM，再拼接为一章。"""
    flat = flatten_subsections_depth_first(section.subsections)
    if not flat:
        return ""
    logger.info("章节 [%s] 启用子节串行撰写（%d 个小节）", section.title, len(flat))
    n = len(flat)
    per = max(380, target_words // max(n, 1))
    parts: list[str] = []
    parent_exe = _executable_outline_prompt_section(section)
    nb = _neighbor_chapter_boundary_hint(plan, section)
    for i, sub in enumerate(flat):
        sub_b = "；".join(sub.bullets[:12]) if sub.bullets else "（紧扣子要点展开）"
        task_parts = []
        if tech_spec_text:
            task_parts.append(tech_spec_text)
        task_parts.extend([
            f"## 子节撰写（整章中的一段）\n",
            f"所属章：{section.title}（`{section.section_id}`）\n",
            f"当前小节：`{sub.section_id}` **{sub.title}**\n",
            f"小节要点：{sub_b}\n",
        ])
        if parent_exe.strip():
            task_parts.append(parent_exe)
        if nb:
            task_parts.append(nb)
        if prev_chapter_excerpt.strip():
            task_parts.append(prev_chapter_excerpt.strip())
        task_parts.extend([
            "\n## 参考文献\n", ref_context,
            "\n## 任务\n",
            f"只写本小节：正文开头第一行必须是三级标题 `### {sub.title}`，然后换行写正文。",
            "禁止在本段中写其他小节的标题；禁止写下一整章内容。",
            f"约 {per} 字。\n",
        ])
        user_prompt = "\n".join(task_parts)
        max_tok = min(5000, max(900, int(per * 2.6)))
        try:
            piece = chat(
                build_messages(system_prompt, user_prompt),
                temperature=min(0.72, temp + 0.02),
                max_tokens=max_tok,
            ).strip()
            if piece:
                parts.append(piece)
        except Exception as e:
            logger.warning("小节 [%s] 生成失败: %s", sub.title, e)
            parts.append(f"### {sub.title}\n\n（本小节生成失败，请手动补写。）")
    return "\n\n".join(parts)


# ── 单章正文核心逻辑 ─────────────────────────────────────────

def _draft_section_body_inner(
    section: SectionNode,
    plan: WritingPlan,
    system_prompt: str,
    ref_context: str,
    thesis_mode: bool,
    thesis_cfg,
    tech_spec_text: str,
    temp: float,
    target_words: int,
    max_tok: int,
    prev_excerpt: str,
) -> str:
    """单章正文生成核心逻辑（串行/并行共用）：子节串行 → 分块 → 多候选 → 默认。"""
    seq_cfg = subsections_sequential_settings()
    use_sequential = (
        seq_cfg["enabled"]
        and bool(section.subsections)
        and thesis_mode
        and section.section_id not in ("acknowledgment",)
    )
    use_chunked = (
        not use_sequential
        and thesis_mode
        and target_words > _CHUNK_WORD_THRESHOLD
        and len(section.bullets) >= 2
        and section.section_id not in ("acknowledgment",)
    )

    if use_sequential:
        logger.info("章节 [%s] 启用子节串行撰写", section.title)
        body = _draft_section_sequential_subsections(
            section, plan, system_prompt, ref_context,
            thesis_mode, thesis_cfg, tech_spec_text, temp, target_words, prev_excerpt,
        )
        if not body.strip():
            body = f"（本章节生成失败，请手动补写。要点：{', '.join(section.bullets)}）"
        return body

    if use_chunked:
        logger.info("章节 [%s] 目标 %d 字，启用分块生成", section.title, target_words)
        try:
            body = _draft_section_chunked(
                section, plan, system_prompt, ref_context, thesis_mode, thesis_cfg,
                tech_spec_text=tech_spec_text,
                prev_chapter_excerpt=prev_excerpt,
            )
            if body.strip():
                return body
            raise ValueError("分块生成返回空")
        except Exception as e:
            logger.warning("章节 [%s] 分块生成失败: %s，降级为单次调用", section.title, e)

    mc = multi_candidate_settings()
    use_multi = section_uses_multi_candidate(section.section_id, thesis_mode, mc)
    if use_multi:
        logger.info("章节 [%s] 启用多候选初稿（N=%d）", section.title, mc["n"])
        body = _draft_section_body_multi_candidate(
            section, plan, system_prompt, ref_context, thesis_mode, thesis_cfg,
            tech_spec_text, mc, temp, target_words,
            prev_chapter_excerpt=prev_excerpt,
        )
        if not body.strip():
            body = f"（本章节生成失败，请手动补写。要点：{', '.join(section.bullets)}）"
        return body

    user_prompt = _build_draft_prompt(
        section, plan, ref_context, thesis_mode, thesis_cfg,
        tech_spec_text=tech_spec_text,
        prev_chapter_excerpt=prev_excerpt,
    )
    messages = build_messages(system_prompt, user_prompt)
    try:
        body = chat(messages, temperature=temp, max_tokens=max_tok)
        if not body.strip():
            raise ValueError("LLM 返回空内容")
    except Exception as e:
        logger.warning("章节 [%s] 生成失败: %s，使用占位文本", section.title, e)
        body = f"（本章节生成失败，请手动补写。要点：{', '.join(section.bullets)}）"
    return body


# ── 主入口 ───────────────────────────────────────────────────

def draft_manuscript(
    plan: WritingPlan,
    store: ReferenceStore,
    user_request: str = "",
    locked_tech_spec_path: str | None = None,
) -> Manuscript:
    """按大纲逐章生成初稿，返回完整 Manuscript。"""
    thesis_mode = _is_thesis_mode()
    thesis_cfg = ThesisConfig.from_config() if thesis_mode else None

    if thesis_mode:
        system_prompt = _SYSTEM_DRAFT_THESIS
    else:
        from .helpers import _citation_style
        system_prompt = _SYSTEM_DRAFT_NORMAL.format(
            citation_style_desc=_citation_style_desc(_citation_style())
        )

    # ── 阶段0：生成 TechSpec
    tech_spec = {}       # type: dict
    tech_spec_text = ""  # type: str
    tech_spec_stored: dict | None = None
    if thesis_mode:
        logger.info("正在生成技术规范文档（TechSpec）...")
        tech_spec_llm = generate_tech_spec(plan, store, user_request)
        locked_raw = load_locked_tech_spec(locked_tech_spec_path)
        tech_spec = merge_tech_specs(tech_spec_llm, locked_raw)
        tech_spec_stored = tech_spec
        if locked_layer_nonempty(locked_raw):
            logger.info("已合并用户锁定 TechSpec（锁定字段覆盖 LLM 同键；文件路径来自参数或配置）")
        tech_spec_text = format_tech_spec_for_prompt(tech_spec)
        if tech_spec_text:
            logger.info("TechSpec 生成成功，将注入所有章节 prompt")

    ABSTRACT_IDS = {"abstract_zh", "abstract_en"}
    plan = merge_outline_scope_overrides(plan)

    # ── 阶段1：生成正文章节
    body_sections: List[ManuscriptSection] = []
    body_sections_map: dict[str, ManuscriptSection] = {}

    draft_parallel = bool(get("parallel_draft", False))
    body_plan_sections = [s for s in plan.outline if s.section_id not in ABSTRACT_IDS]

    if draft_parallel and len(body_plan_sections) > 1:
        logger.info("DRAFT 阶段启用并行撰写（%d 章）", len(body_plan_sections))

        def _draft_one_section(section: SectionNode) -> ManuscriptSection:
            ref_context = _build_ref_context(store, section)
            target_words = _get_section_target_words(section.section_id)
            temp = 0.5 if section.section_id == "acknowledgment" else 0.7
            prev_excerpt = ""
            max_tok = min(8000, max(1500, int(target_words * 1.8)))
            body = _draft_section_body_inner(
                section, plan, system_prompt, ref_context,
                thesis_mode, thesis_cfg, tech_spec_text, temp=temp,
                target_words=target_words, max_tok=max_tok, prev_excerpt=prev_excerpt,
            )
            if not body.strip():
                body = f"（本章节生成失败，请手动补写。要点：{', '.join(section.bullets)}）"
            body = _clean_section_overflow(body, section.section_id)
            body = _fix_citation_position(body)
            body = _maybe_retry_after_scope_fail(
                body, section, plan, system_prompt, ref_context, thesis_mode, thesis_cfg,
                tech_spec_text, temp=temp, target_words=target_words, max_tok=max_tok,
                prev_chapter_excerpt=prev_excerpt,
            )
            logger.info("章节 [%s] 撰写完成（约 %d 字）", section.title, len(body))
            return ManuscriptSection(
                section_id=section.section_id,
                title=section.title,
                markdown_body=body,
            )

        with ThreadPoolExecutor(max_workers=min(6, len(body_plan_sections))) as ex:
            futures = {ex.submit(_draft_one_section, s): s for s in body_plan_sections}
            for fut in as_completed(futures):
                sec = fut.result()
                body_sections_map[sec.section_id] = sec
                logger.info("并行章节 [%s] 完成", sec.title)
    else:
        completed_bodies: dict[str, str] = {}
        for section in body_plan_sections:
            ref_context = _build_ref_context(store, section)
            target_words = _get_section_target_words(section.section_id)
            temp = 0.5 if section.section_id == "acknowledgment" else 0.7
            prev_excerpt = _format_prev_chapter_excerpt(completed_bodies, plan, section)
            max_tok = min(8000, max(1500, int(target_words * 1.8)))
            body = _draft_section_body_inner(
                section, plan, system_prompt, ref_context,
                thesis_mode, thesis_cfg, tech_spec_text, temp=temp,
                target_words=target_words, max_tok=max_tok, prev_excerpt=prev_excerpt,
            )
            if not body.strip():
                body = f"（本章节生成失败，请手动补写。要点：{', '.join(section.bullets)}）"
            body = _clean_section_overflow(body, section.section_id)
            body = _fix_citation_position(body)
            body = _maybe_retry_after_scope_fail(
                body, section, plan, system_prompt, ref_context, thesis_mode, thesis_cfg,
                tech_spec_text, temp=temp, target_words=target_words, max_tok=max_tok,
                prev_chapter_excerpt=prev_excerpt,
            )
            body = _ensure_subsections_present(body, section)
            completed_bodies[section.section_id] = body
            body_sections_map[section.section_id] = ManuscriptSection(
                section_id=section.section_id,
                title=section.title,
                markdown_body=body,
            )
            logger.info("章节 [%s] 撰写完成（约 %d 字）", section.title, len(body))

    for section in body_plan_sections:
        if section.section_id in body_sections_map:
            body_sections.append(body_sections_map[section.section_id])

    # ── 阶段2：基于正文生成中英文摘要
    if thesis_mode:
        abstract_zh, abstract_en = _generate_abstract_from_body(
            plan, body_sections, thesis_mode,
            tech_spec_text=tech_spec_text, tech_spec=tech_spec_stored,
        )
    else:
        abstract_zh = abstract_en = None
        for section in plan.outline:
            if section.section_id in ABSTRACT_IDS:
                ref_context = _build_ref_context(store, section)
                user_prompt = _build_draft_prompt(section, plan, ref_context, False, None)
                messages = build_messages(system_prompt, user_prompt)
                try:
                    body = chat(messages, temperature=0.5, max_tokens=2000)
                except Exception:
                    body = "（摘要生成失败）"
                sec = ManuscriptSection(
                    section_id=section.section_id,
                    title=section.title,
                    markdown_body=body,
                )
                if section.section_id == "abstract_zh":
                    abstract_zh = sec
                else:
                    abstract_en = sec

    # ── 阶段3：按规范顺序组装章节
    sections: List[ManuscriptSection] = []

    if abstract_zh:
        sections.append(abstract_zh)
    if abstract_en:
        sections.append(abstract_en)

    outline_order = [s.section_id for s in plan.outline if s.section_id not in ABSTRACT_IDS]
    body_map = {s.section_id: s for s in body_sections}
    for sid in outline_order:
        if sid in body_map:
            sections.append(body_map[sid])

    refs_sec = _build_ref_list_section(store, thesis_mode)
    refs_before_ack = bool(get("reference_before_acknowledgment", False))
    if refs_before_ack:
        ack_idx = next((i for i, s in enumerate(sections) if s.section_id == "acknowledgment"), -1)
        if ack_idx >= 0:
            sections.insert(ack_idx, refs_sec)
        else:
            sections.append(refs_sec)
    else:
        sections.append(refs_sec)

    kw_zh, kw_en = _format_keywords_text(plan, thesis_mode) if thesis_mode else ("", "")
    return Manuscript(
        sections=sections,
        version=1,
        thesis_title=plan.title if thesis_mode else "",
        keywords_zh_text=kw_zh,
        keywords_en_text=kw_en,
        tech_spec=tech_spec_stored if thesis_mode else None,
    )
