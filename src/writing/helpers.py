"""
纯工具函数与常量 — 无 writing 包内依赖。

被 draft_engine / revision_engine / postprocess / abstract / term_map 共享。
"""
import logging
import re as _re
from typing import List, Optional

from ..config import get
from ..models import ManuscriptSection, SectionNode, WritingPlan
from ..ref_store import ReferenceStore

logger = logging.getLogger(__name__)

# ── 章节规则 ──────────────────────────────────────────────────

_SECTION_RULES: dict[str, str] = {
    "abstract_zh": (
        "【中文摘要严格规则】\n"
        "1. 字数：500-800字，不得少于500字\n"
        "2. 四要素缺一不可，按顺序展开：\n"
        "   （1）目的：说明研究背景和研究要解决的问题\n"
        "   （2）方法：说明采用的技术路线、设计方案\n"
        "   （3）结果：说明系统实现了什么功能，达到了哪些指标\n"
        "   （4）结论：说明本研究的价值和意义\n"
        "3. 绝对禁止：不得出现[1]等引用标记\n"
        "4. 绝对禁止：不得出现'如图X-X''见表X-X'等图表引用\n"
        "5. 绝对禁止：不得在末尾写'关键词：'（框架单独生成）\n"
        "6. 绝对禁止：不得重复论文题目\n"
        "7. 全部使用第三人称，语态以被动为主\n"
        "8. 直接输出摘要正文，不加'摘要'标题行"
    ),
    "abstract_en": (
        "【English Abstract Strict Rules】\n"
        "1. Length: 200-400 words, must accurately correspond to the Chinese abstract\n"
        "2. Four elements required: Objective, Methods, Results, Conclusion\n"
        "3. Grammar: past tense for methods/results; present tense for conclusions/significance\n"
        "4. Use 'this thesis' not 'this paper' or 'this article'\n"
        "5. No figure/table/equation references (no 'Fig.', 'Table', 'Eq.')\n"
        "6. No citation markers [1] etc.\n"
        "7. Do not write 'Keywords:' at the end\n"
        "8. Third person; avoid 'I', 'we', 'our'\n"
        "9. Check: each technical term in English must match what is stated in the Chinese abstract\n"
        "10. Output only the abstract body, no 'Abstract' heading"
    ),
    "s1": (
        "【第1章引言特殊规则】\n"
        "1. 末尾用2-3点归纳本文核心工作与创新贡献，使研究价值一目了然\n"
        "2. 创新贡献点应与第6章结论中的贡献呼应，保持一致性\n"
        "3. 严禁在此章展开系统设计、硬件选型、电路、实验结果等后续章节内容"
    ),
    "s2": (
        "【第2章相关工作特殊规则】\n"
        "1. 末尾禁止重复第1章已经说明的创新点和研究贡献，"
        "只需指出现有方法的不足如何引出本文的工作\n"
        "2. 综述文献时必须具体说明每篇文献的方法和局限性，"
        "不能泛泛而谈\n"
        "3. 每段引用的文献应与段落主题强相关，不得堆砌无关文献\n"
        "4. 段落结构：现象/方法描述 → 优点 → 局限性\n"
        "5. 目标字数：3500-4500字，超过4500字请精简，优先压缩与他章重叠的冗余背景\n"
        "6. 关键器件选型（MCU、传感器、通信模块）必须有选型对比与理由，说明为什么选A不选B"
    ),
    "s3": (
        "【第3章硬件设计特殊规则】\n"
        "1. 含总体架构图（用文字描述架构，标注【图3-1 系统硬件总体架构】）\n"
        "2. 每个关键硬件模块（MCU、传感器、通信、电源）须有选型理由或对比\n"
        "3. 电路设计要点须与选型结论呼应，不得出现与选型无关的器件\n"
        "4. 禁止在此章复述第2章的背景介绍或文献综述"
    ),
    "s4": (
        "【第4章软件设计特殊规则】\n"
        "1. 含主程序流程图（用文字描述，标注【图4-1 主程序流程图】）\n"
        "2. 核心算法（如PID、滤波）须给出选型依据和关键参数约定\n"
        "3. 各模块软件说明须与硬件选型对应，不得出现第3章未提及的硬件\n"
        "4. 禁止在此章复述第2章的背景介绍或文献综述"
    ),
    "s5": (
        "【第5章系统测试特殊规则】\n"
        "1. 每个被测传感器须有独立测试子节（测试目的→方法→数据→分析）\n"
        "2. 测试结果须与第1章提出的研究问题对照（如回应\"响应滞后\"则给出响应时间对比数据）\n"
        "3. 每个测试子节须明确引用前面章节（s3/s4）中设计的具体模块（如\"如第3.2节设计的YL-69采集电路\"）\n"
        "4. 所有测试数据使用[实测数据]占位，不编造具体数值\n"
        "5. 测试结论须为第6章的总结贡献提供素材"
    ),
    "s6": (
        "【结论章节严格规则】\n"
        "1. 只允许写四部分：主要工作总结、创新贡献点列举、研究局限性、未来工作展望\n"
        "2. 绝对禁止：不得写个人学习体会、感悟感慨\n"
        "3. 绝对禁止：不得写自我检讨（如'基础差'、'时间紧'、'能力有限'等）\n"
        "4. 绝对禁止：未完成的功能缺陷不是'未来工作'\n"
        "5. '主要工作总结'不得与'创新贡献点'内容重复，前者总结做了什么，"
        "后者强调创新在哪里\n"
        "6. '未来工作展望'须与全文核心技术方向有逻辑联系，不得跨度过大\n"
        "7. 语气：客观、简洁、学术化\n"
        "8. 目标字数：800-1200字"
    ),
    "acknowledgment": (
        "【致谢规则】\n"
        "1. 字数：200-400字\n"
        "2. 必须感谢：指导教师、实验室/课题组成员\n"
        "3. 可以感谢：学院/学校提供的平台资源\n"
        "4. 感谢家人时措辞要简短，不宜占主要篇幅\n"
        "5. 不要写个人成长感悟或学习心得\n"
        "6. 不使用参考文献引用\n"
        "7. 语气正式、诚恳，一两段即可"
    ),
}


def _get_section_rule(section_id: str) -> str:
    """获取章节特殊规则：优先读用户配置覆盖（override_section_rules），否则用硬编码默认值。"""
    overrides = get("override_section_rules") or {}
    if isinstance(overrides, dict) and section_id in overrides:
        return str(overrides[section_id])
    return _SECTION_RULES.get(section_id, "")


# 各章节目标字数（默认值，实际从 config 读取覆盖）
_DEFAULT_SECTION_WORDS = {
    "abstract_zh":    600,
    "abstract_en":    400,
    "s1":             2500,
    "s2":             4000,
    "s3":             5500,
    "s4":             5500,
    "s5":             4000,
    "s6":             1500,
    "acknowledgment":  300,
}


def _get_section_target_words(section_id: str) -> int:
    """获取章节目标字数"""
    cfg_words = get("thesis_section_words", {}) or {}
    return int(cfg_words.get(section_id, _DEFAULT_SECTION_WORDS.get(section_id, 1500)))


# ── 分块生成参数 ─────────────────────────────────────────────

_CHUNK_WORD_THRESHOLD = 2000
_CHUNK_TARGET_WORDS = 1500
_CHUNK_TEMPERATURE = 0.55


# ── 模式判断 ─────────────────────────────────────────────────

def _is_thesis_mode() -> bool:
    return bool(get("thesis_mode", False))


def _citation_style() -> str:
    if _is_thesis_mode():
        return "numeric"
    return get("citation_style", "author_year")


# ── 共享执行协议 ─────────────────────────────────────────────

_EXECUTION_PROTOCOL = """
【输出前自查（仅内部执行，严禁输出到最终回答中）】
在生成最终正文前，在后台进行以下检查——这些检查过程和结果不得写入输出：
1. 内容一致性：是否出现了正文未提及的技术术语或数据？
2. 规范符合度：是否有漏用中文句号、图表引用越界、型号不一致？
3. 最小干预：是否为了合规而改动了原本正确的内容？（如果是，请还原）
4. 去 AI 味：是否有"首先…其次…然后…最后"等机械套话或模板化过渡词？（如有，用自然语义过渡替代）
"""


# ── 引用与文献上下文 ─────────────────────────────────────────

def _ref_limit_for_section(section_id: str) -> int:
    """按章节控制参考文献上下文注入量。优先读取 citation_enabled_sections 配置。"""
    if section_id in ("abstract_zh", "abstract_en", "acknowledgment", "keywords", "refs"):
        return 0
    enabled = get("citation_enabled_sections") or ["s1", "s2"]
    if section_id not in enabled:
        return 0
    if section_id == "s2":
        return 50
    return 10


def _build_ref_context(store: ReferenceStore, section: SectionNode) -> str:
    """为当前章节返回文献上下文（根据章节类型控制数量，附序号供 LLM 使用）"""
    limit = _ref_limit_for_section(section.section_id)
    if limit <= 0:
        return ""
    return store.as_context_text(max_refs=limit)


# ── 大纲与章节边界 ───────────────────────────────────────────

def _subsections_prompt_lines(section: SectionNode, indent: str = "") -> list[str]:
    """子结构展平为 prompt 行（撰写阶段仍生成整章，用 ### 对齐小节）。"""
    lines: list[str] = []
    for sub in section.subsections:
        sub_b = "；".join(sub.bullets[:10]) if sub.bullets else "（结合要点展开）"
        lines.append(f"{indent}- **{sub.title}**（`{sub.section_id}`）：{sub_b}")
        lines.extend(_subsections_prompt_lines(sub, indent + "  "))
    return lines


def _executable_outline_prompt_section(section: SectionNode) -> str:
    """将规划阶段的可执行大纲注入撰写 prompt，约束本章边界。"""
    chunks: list[str] = []
    if section.outline_detail.strip():
        chunks.append(f"【段落展开顺序与约束】\n{section.outline_detail.strip()}")
    if section.scope_must_include:
        chunks.append(
            "【本章必须包含】\n" + "\n".join(f"- {x}" for x in section.scope_must_include)
        )
    if section.scope_forbidden:
        chunks.append(
            "【本章禁止出现/禁止提前展开】\n" + "\n".join(f"- {x}" for x in section.scope_forbidden)
        )
    sub_lines = _subsections_prompt_lines(section)
    if sub_lines:
        chunks.append(
            "【强制小节结构】（必须使用 Markdown `###` / `####` 设小标题，每个小标题必须出现，不得遗漏或合并）\n"
            + "\n".join(sub_lines)
        )
    if not chunks:
        return ""
    return "\n## 可执行大纲与边界（须严格遵守）\n" + "\n\n".join(chunks) + "\n"


def _neighbor_chapter_boundary_hint(plan: WritingPlan, section: SectionNode) -> str:
    """仅提示下一章标题，避免模型提前撰写后章正文。"""
    sid = section.section_id
    if not (sid.startswith("s") and sid[1:].isdigit()):
        return ""
    ids = [s.section_id for s in plan.outline]
    try:
        idx = ids.index(sid)
    except ValueError:
        return ""
    if idx + 1 >= len(plan.outline):
        return ""
    nxt = plan.outline[idx + 1]
    return (
        f"\n【后文边界】下一章为「{nxt.title}」（`{nxt.section_id}`），"
        "禁止在本章写出该章才应出现的正文级内容（如属于下一章的实现细节、实验大段、结论性总评）。\n"
    )


def _format_prev_chapter_excerpt(
    completed_bodies: dict[str, str],
    plan: WritingPlan,
    section: SectionNode,
) -> str:
    """上一章正文极短摘录，供衔接与防重复（不替代 TechSpec）。"""
    ids = [s.section_id for s in plan.outline]
    try:
        idx = ids.index(section.section_id)
    except ValueError:
        return ""
    for j in range(idx - 1, -1, -1):
        sid = ids[j]
        if sid in ("abstract_zh", "abstract_en"):
            continue
        body = completed_bodies.get(sid, "")
        if not body.strip():
            continue
        t = body.strip().replace("\n", " ")
        if len(t) > 280:
            t = t[:280] + "…"
        prev_node = plan.outline[j]
        return (
            f"\n【前文摘要（仅衔接、防重复；勿整段复述）】"
            f"上一章「{prev_node.title}」要旨摘录：{t}\n"
        )
    return ""


# ── TechSpec 辅助 ────────────────────────────────────────────

def _build_sensor_checklist(tech_spec_text: str) -> str:
    """从 TechSpec 文本中提取传感器列表，生成测试覆盖清单（供 s5 注入）。"""
    if not tech_spec_text:
        return ""
    sensor_lines: list[str] = []
    for line in tech_spec_text.splitlines():
        stripped = line.strip()
        if "：型号 " in stripped and ("类型：" in stripped):
            sensor_lines.append(stripped)
    if not sensor_lines:
        return ""
    checklist = [
        "\n【传感器测试覆盖清单 - 以下硬件必须在测试章节有对应实验数据】",
    ]
    for sl in sensor_lines:
        checklist.append(f"  ✓ {sl}")
    checklist.append(
        "  要求：每个传感器需有独立的测试子节，包含测试目的、方法、数据与分析。"
    )
    return "\n".join(checklist)


def _filter_tech_spec_for_section(tech_spec_text: str, section_id: str) -> str:
    """按章节过滤 TechSpec：s1/s6 不传，s2 只传需求段，s3-s5 完整传。"""
    if section_id in ("s1", "s6", "abstract_zh", "abstract_en", "acknowledgment"):
        return ""
    if section_id == "s2":
        lines = tech_spec_text.split("\n")
        cut = 0
        for i, line in enumerate(lines):
            if "：型号 " in line or "型号：" in line:
                cut = i
                break
        if cut > 3:
            return "\n".join(lines[:cut]).strip()
    return tech_spec_text


# ── 跨章上下文 ──────────────────────────────────────────────

def _build_chapter_chain_context(
    section_id: str,
    plan: "WritingPlan",
    prev_chapter_excerpt: str,
) -> str:
    """构建跨章上下文：前章摘要 + 后章要点 + 研究问题牵引。"""
    if not section_id.startswith("s") or not section_id[1:].isdigit():
        return ""
    ch = int(section_id[1:])

    outline_map = {s.section_id: s for s in plan.outline}

    s1_node = outline_map.get("s1")
    problem_bullets: list[str] = []
    if s1_node and s1_node.bullets:
        problem_bullets = [b for b in s1_node.bullets
                          if any(kw in b for kw in ["问题", "不足", "挑战", "缺陷", "滞后", "缺乏"])]
        if not problem_bullets and len(s1_node.bullets) >= 2:
            problem_bullets = s1_node.bullets[-2:]

    parts: list[str] = []
    parts.append("## 你在全文中的位置\n")

    # 前章
    prev_id = f"s{ch - 1}" if ch > 1 else None
    if prev_id and prev_chapter_excerpt.strip():
        parts.append(
            f"**第{ch - 1}章已完成**，核心内容摘要：\n{prev_chapter_excerpt.strip()[:400]}\n"
            f"你的章节是第{ch}章，请在写作中承接上文铺垫，"
            f"并在自然过渡处提及与第{ch - 1}章的衔接关系。\n"
        )

    # 后章
    next_id = f"s{ch + 1}" if ch < 6 else None
    if next_id and next_id in outline_map:
        next_node = outline_map[next_id]
        next_bullets = next_node.bullets[:3] if next_node.bullets else []
        parts.append(
            f"**下一章是第{ch + 1}章「{next_node.title.replace(f'第{ch+1}章 ', '')}」**，"
            "你应为它提供以下基础："
        )
        for b in next_bullets:
            parts.append(f"  - {b}")
        parts.append(
            "在你的章节末尾，确保下一章可以直接基于你的内容展开，不留逻辑缺口。\n"
        )

    # 研究问题
    if problem_bullets:
        if ch == 6:
            parts.append(
                "\n**⚠ 你必须逐一回应以下问题（来自第1章引言）：**\n"
                + "\n".join(f"- {b}" for b in problem_bullets)
                + "\n请用连贯段落回应，不必标号但勿遗漏。"
            )
        else:
            parts.append(
                "\n**第1章提出的研究问题：**\n"
                + "\n".join(f"- {b}" for b in problem_bullets)
                + "\n你的章节应针对性地为这些问题提供解决方案或技术支撑。"
                + "写作中请适时回溯问题，说明你的设计如何帮助解决它们。"
            )

    return "\n".join(parts)


# ── 分块生成辅助 ─────────────────────────────────────────────

def _chunk_body_looks_truncated(body: str) -> bool:
    """末尾既无句末标点又足够长时，怀疑因 token 限制被截断。"""
    t = body.rstrip()
    if len(t) < 120:
        return False
    return not bool(_re.search(r"[。！？…」』]\s*$", t))


def _split_bullets_into_chunks(
    bullets: list[str],
    chunk_size: int = 2,
) -> list[list[str]]:
    """将 bullets 列表按 chunk_size 分组"""
    return [bullets[i: i + chunk_size] for i in range(0, max(len(bullets), 1), chunk_size)]


# ── 关键词格式化 ─────────────────────────────────────────────

def _format_keywords_text(
    plan: "WritingPlan",
    thesis_mode: bool,
) -> tuple[str, str]:
    """
    返回 (zh_text, en_text) 关键词纯文本对。
    zh_text 不含"关键词："前缀时不自动补。
    """
    if not thesis_mode or not plan.keywords:
        if thesis_mode and not plan.keywords:
            logger.warning("plan.keywords 为空，输出将缺少关键词；请检查规划阶段是否正确生成关键词")
        return "", ""

    kws_zh = plan.keywords[:5]
    kws_en = plan.keywords_en[:5] if plan.keywords_en else []
    n = min(len(kws_zh), len(kws_en)) if kws_en else len(kws_zh)
    kws_zh = kws_zh[:n]
    kws_en = kws_en[:n]

    zh_line = "关键词：" + "；".join(kws_zh)

    def _normalize_en_kw(kw: str) -> str:
        words = kw.split()
        result = []
        for w in words:
            if w.upper() == w and len(w) >= 2:
                result.append(w)
            else:
                result.append(w.lower())
        return " ".join(result)

    if kws_en:
        en_line = "Keywords: " + "; ".join(_normalize_en_kw(k) for k in kws_en)
    else:
        en_line = "（英文关键词待补充：Keywords: ...）"

    return zh_line, en_line


def _format_keywords_section(
    plan: "WritingPlan",
    thesis_mode: bool,
) -> Optional[ManuscriptSection]:
    """生成关键词文本块（附在摘要章节之后）。"""
    zh_line, en_line = _format_keywords_text(plan, thesis_mode)
    if not zh_line:
        return None

    body = f"{zh_line}\n\n{en_line}" if en_line else zh_line

    return ManuscriptSection(
        section_id="keywords",
        title="关键词",
        markdown_body=body,
    )


# ── 参考文献列表 ─────────────────────────────────────────────

def _authors_line_for_plain_ref(authors: list[str]) -> str:
    """非毕业论文模式下参考文献作者列表：含中文名用「等」，否则用 et al.。"""
    if not authors:
        return ""
    has_cjk = any(_re.search(r"[一-鿿]", (a or "")) for a in authors)
    if len(authors) <= 3:
        return "; ".join(authors)
    head = "; ".join(authors[:3])
    return head + (" 等" if has_cjk else " et al.")


def _build_ref_list_section(store: ReferenceStore, thesis_mode: bool, order_map: dict | None = None) -> ManuscriptSection:
    """生成参考文献列表章节。order_map 非空时按出现顺序重排并仅列出被引用文献。"""
    if thesis_mode:
        body = store.format_thesis_ref_list(order_map=order_map)
    else:
        refs = store.all_refs()
        lines = []
        for i, ref in enumerate(refs, 1):
            authors_str = _authors_line_for_plain_ref(ref.authors)
            year = ref.year or "n.d."
            doi_str = f" DOI: {ref.doi}" if ref.doi else ""
            lines.append(f"[{i}] {authors_str}. {ref.title}. {ref.venue or ''} ({year}).{doi_str}")
        body = "\n".join(lines) if lines else "（暂无参考文献）"

    return ManuscriptSection(
        section_id="refs",
        title="参考文献",
        markdown_body=body,
    )


# ── 小节对齐 ────────────────────────────────────────────────

def _ensure_subsections_present(body: str, section: "SectionNode") -> str:
    """如果正文中缺失大纲要求的某个小节标题，追加可见占位符（仅 DRAFT 阶段调用）。"""
    from .scope_enforce import flatten_subsections_depth_first as _flatten
    flat = _flatten(section.subsections)
    if not flat:
        return body

    body_headings = {h.strip() for h in _re.findall(r"^###\s+(.+)$", body, _re.MULTILINE)}

    for sub in flat:
        plan_title_clean = _re.sub(r"[^\w一-鿿]", "", sub.title)
        if not plan_title_clean:
            continue

        found = False
        for h in body_headings:
            h_clean = _re.sub(r"[^\w一-鿿]", "", h)
            if plan_title_clean in h_clean or h_clean in plan_title_clean:
                found = True
                break
        if not found:
            body += f"\n\n### {sub.title}\n\n> **待撰写：{sub.title}**\n"
            logger.info("章节 [%s] 补全缺失小节: %s", section.title, sub.title)
    return body


def _align_subsections_titles(body: str, section: "SectionNode") -> None:
    """
    修订后，将 plan.subsections 中的标题对齐到 LLM 实际使用的 ### 标题。
    若 LLM 标题包含 plan 标题的全部核心字符，则更新 plan 为 LLM 版本。
    带 1.5 倍长度限制，防止 LLM 过长的描述性标题被同步。
    """
    from .scope_enforce import flatten_subsections_depth_first as _flatten2
    flat = _flatten2(section.subsections)
    if not flat:
        return

    body_headings = {h.strip() for h in _re.findall(r"^###\s+(.+)$", body, _re.MULTILINE)}
    for sub in flat:
        plan_clean = _re.sub(r"[^\w一-鿿]", "", sub.title)
        if not plan_clean:
            continue
        for h in body_headings:
            h_clean = _re.sub(r"[^\w一-鿿]", "", h)
            if plan_clean in h_clean and len(h) <= len(sub.title) * 1.5:
                if h.strip() != sub.title:
                    logger.debug("小节标题对齐: %r → %r", sub.title, h.strip())
                    sub.title = h.strip()
                break


# ── 修订产物清洗 ─────────────────────────────────────────────

def _strip_revision_artifacts(body: str) -> str:
    """清除 LLM 修订输出中的格式产物：Part 1/Part 2 标签、修改日志、修订说明等。"""
    # 预先编译常用模式
    _PT_PART1 = _re.compile(
        r"^\s*(?:Part\s*(?:1|One)|【正文】|\[正文\])\s*"
        r"(?:[\[（【][^\]）】]*[\]）】]\s*)?(?:正文\s*)?"
        r"[：:]?\s*",
        _re.I,
    )
    _PT_PART2_CUT = _re.compile(
        r"\n\s*(?:Part\s*(?:2|Two)|【修改日志】|##\s*修改日志|修订说明)\s*"
        r"(?:[\[（【][^\]）】]*[\]）】]\s*)?"
        r"[：:]*",
        _re.I,
    )
    _PT_PART1_INLINE = _re.compile(
        r"^\s*(?:Part\s*(?:1|One)|【正文】|\[正文\])\s*"
        r"(?:[\[（【][^\]）】]*[\]）】]\s*)?(?:正文\s*)?"
        r"[：:]?\s*",
        _re.I | _re.M,
    )

    # 1. 跳过开头的纯标签行（直到遇到实质内容行）
    lines = body.splitlines()
    start_idx = 0
    for i, line in enumerate(lines):
        if _PT_PART1.match(line) and not line[len(_PT_PART1.match(line).group()):].strip():
            continue
        if line.strip():
            start_idx = i
            break
    body = "\n".join(lines[start_idx:])

    # 2. 截断 Part 2 / 修改日志及之后的内容
    body = _PT_PART2_CUT.split(body, maxsplit=1)[0]

    # 3. 去除行内残留的 "Part 1：" 等标签前缀
    body = _PT_PART1_INLINE.sub("", body)

    return body.strip()
