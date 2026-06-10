"""
全文后处理 — 引用修正、标点清理、章节越界、术语统一、个人感悟删除。

包含：
- postprocess_manuscript: 核心后处理入口
- reorder_citations_by_first_appearance: 引用按首次出现重编号
- _finalize_manuscript_postprocess: term_map + postprocess 一站式

依赖 helpers.py 和 term_map.py，不依赖 draft_engine / revision_engine（无循环导入）。
"""
import logging
import re as _re
from typing import Optional

from ..config import get
from ..models import Manuscript, ManuscriptSection, WritingPlan
from ..ref_store import ReferenceStore
from .helpers import _build_ref_list_section
from .scope_enforce import build_l3a_term_map
from .term_map import build_global_term_map

logger = logging.getLogger(__name__)


# ── 引用位置修正 ─────────────────────────────────────────────

def _clean_double_punctuation(text: str) -> str:
    """清理连续重复标点：同类重复用正则压缩，异类保留最后一个。"""
    # 连续相同标点 → 去重
    text = _re.sub(r"([。，；：！？、])\1+", r"\1", text)
    # 异类连续标点 → 保留最后一个
    text = _re.sub(r"([，；：])([。！？])", r"\2", text)
    text = _re.sub(r"([。！？])([，；：])", r"\1", text)
    return text


def _check_missing_punct(text: str, section_id: str = "") -> str:
    """启发式补全句末缺失标点：中文字符连续>80且末尾无标点/引用的非列表行，在末尾加句号。"""
    if section_id == "abstract_en":
        return text

    lines = text.split("\n")
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        if _re.match(r"^[\-\*\•]|\d+[\.\、\)]", stripped):
            out.append(line)
            continue
        if "$" in stripped or "|" in stripped:
            out.append(line)
            continue
        cn_chars = len(_re.findall(r"[一-鿿]", stripped))
        if cn_chars < 20:
            out.append(line)
            continue
        if _re.search(r"[。！？\.!\?][”\"\'』]?\s*$", stripped):
            out.append(line)
            continue
        if _re.search(r"\[\d+(?:[,，]\d+)*\]\s*$", stripped):
            out.append(line)
            continue
        indent = line[:len(line) - len(line.lstrip())]
        out.append(indent + stripped + "。")
    return "\n".join(out)


def _clean_section_overflow(body: str, section_id: str) -> str:
    """
    防止 LLM 把下一章节内容写入当前章节。
    仅对正文章节 s1-s6 生效。
    """
    if not section_id.startswith("s") or not section_id[1:].isdigit():
        return body

    current_num = int(section_id[1:])
    next_num = current_num + 1
    _PARA_OVERFLOW_MAJOR_MAX = 20

    patterns = [
        rf"(?m)^#{{1,3}}\s+{next_num}\.\d",
        rf"(?m)^##\s+第{next_num}章",
        rf"(?m)^##\s+{next_num}\s",
        rf"(?m)^#+\s+{next_num}\.",
        rf"(?m)^#{{1,3}}\s+第\s*{next_num}\s*章",
    ]

    earliest = len(body)
    for pat in patterns:
        m = _re.search(pat, body)
        if m and m.start() < earliest:
            earliest = m.start()

    tail_frac = float(get("section_overflow_tail_scan_fraction", 0.3))
    tail_frac = min(0.95, max(0.05, tail_frac))
    tail_start = int(len(body) * (1.0 - tail_frac))
    tail = body[tail_start:]
    tail_patterns = [
        rf"(?m)^第\s*{next_num}\s*章(?:\s|$|一-鿿)",
        rf"(?m)^{next_num}\s+[一-鿿]",
    ]
    for pat in tail_patterns:
        m = _re.search(pat, tail)
        if m:
            pos = tail_start + m.start()
            if pos < earliest:
                earliest = pos

    para_pat = _re.compile(r"(?m)^(\d+)\.(\d+)\s+")
    for m in para_pat.finditer(body):
        if m.start() < tail_start:
            continue
        try:
            major = int(m.group(1))
        except ValueError:
            continue
        if major <= current_num or major > _PARA_OVERFLOW_MAJOR_MAX:
            continue
        if m.start() < earliest:
            earliest = m.start()

    if earliest < len(body):
        truncated = body[:earliest].rstrip()
        last_end = max(
            [truncated.rfind(ch) for ch in ["。", "！", "？", "」"]] or [0]
        )
        if last_end > 0 and (len(truncated) - last_end) < 200:
            truncated = truncated[:last_end + 1].rstrip()
            logger.info("章节 [%s] 截断回溯到完整句末（距离截断点 %d 字）",
                        section_id, len(body) - earliest)
        logger.warning(
            "章节 [%s] 检测到 LLM 越界写入下一章内容，已截断（截断位置 %d/%d 字）",
            section_id, earliest, len(body)
        )
        return truncated

    return body


def _truncate_abstract(body: str, section_id: str, max_chars: int = 800) -> str:
    """摘要字数超标时在最后一个完整句子处截断。"""
    if section_id not in ("abstract_zh", "abstract_en"):
        return body
    if len(body) <= max_chars:
        return body
    trunk = body[:max_chars]
    end_punc = "。" if section_id == "abstract_zh" else "."
    last_punc = trunk.rfind(end_punc)
    if last_punc == -1:
        return trunk + end_punc
    return trunk[:last_punc + 1]


# ── 引用位置修正 ─────────────────────────────────────────────

def _fix_citation_line_zh_eval_aligned(line: str) -> str:
    """
    与 evaluator 的「[。；！？][^\n]*?\\[\\d+\\]」对齐：将句末标点之后的 [n] 前移到该标点之前。
    对单行反复应用直至无匹配。
    """
    pat = _re.compile(r"^(.*?)([。；！？])([^[]*?)(\[\d+(?:[,，]\d+)*\])(.*)$")
    changed = True
    while changed:
        changed = False
        m = pat.match(line)
        if not m:
            break
        prefix, punc, mid, cite, suffix = m.groups()
        new_line = prefix + mid + cite + punc + suffix
        if new_line != line:
            line = new_line
            changed = True
    return line


def _fix_citation_position(text: str) -> str:
    """
    修正引用标记位置：将「标点[数字]」改为「[数字]标点」。
    先按行做与评估器一致的 [。；！？]…[n] 前移，再处理标点紧邻引用及英文句点情形。
    """
    # 0. 跨行预处理：中文标点在行末，引用在下一行行首
    text = _re.sub(r"([。；！？])\s*\n\s*((?:\[\d+(?:[,，]\d+)*\])+)", r"\2\1\n", text)

    lines = text.split("\n")
    text = "\n".join(_fix_citation_line_zh_eval_aligned(L) for L in lines)

    # 标点与引用相邻
    pattern = _re.compile(r"([。，；：！？、])\s*((?:\[\d+(?:[,，]\d+)*\])+)")

    def swap(m: _re.Match) -> str:
        return m.group(2) + m.group(1)

    text = pattern.sub(swap, text)

    # 英文句末：`. [1]` → `[1].`
    dot_cite = _re.compile(
        r"(\.)(\s*)((?:\[\d+(?:[,，]\d+)*\])+)",
        flags=_re.MULTILINE,
    )
    text = dot_cite.sub(lambda m: m.group(3) + m.group(1) + (m.group(2) or ""), text)

    # swap 后可能产生形如 [1]，。 的异类双标点
    text = _clean_double_punctuation(text)
    return text


# ── 章节越界 ─────────────────────────────────────────────────

# （从 helpers 导入 _clean_section_overflow，此处不重复定义）


# ── 结构清理 ─────────────────────────────────────────────────

def _clean_horizontal_rules(body: str) -> str:
    """删除正文中的水平线（^---$），不处理表格分隔线 |---|。"""
    return _re.sub(r"^---+$", "", body, flags=_re.MULTILINE)


def _downgrade_body_h1(body: str) -> str:
    """将正文内意外出现的 # H1 降级为 ## H2。排除代码块内的 # 行。"""
    lines = body.split("\n")
    in_code_block = False
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            result.append(line)
            continue
        if not in_code_block and line.startswith("# ") and not line.startswith("## "):
            result.append("##" + line[1:])
        else:
            result.append(line)
    return "\n".join(result)


# ── 个人感悟删除 ─────────────────────────────────────────────

def _remove_personal_remarks(text: str) -> str:
    """删除论文中常见的个人感悟、主观表达句子。"""
    PERSONAL_PATTERNS = [
        r"[^。\n]*由于本人[^。\n]*[。]?",
        r"[^。\n]*本人基础[^。\n]*[。]?",
        r"[^。\n]*本人[^。\n]*尚[^。\n]*不足[^。\n]*[。]?",
        r"[^。\n]*笔者.{0,16}不足[^。\n]*[。]?",
        r"[^。\n]*笔者.{0,16}有限[^。\n]*[。]?",
        r"[^。\n]*知识有限[^。\n]*[。]?",
        r"[^。\n]*能力尚有不足[^。\n]*[。]?",
        r"[^。\n]*水平有限[^。\n]*[。]?",
        r"[^。\n]*能力有限[^。\n]*[。]?",
        r"[^。\n]*精力有限[^。\n]*[。]?",
        r"[^。\n]*时间仓促[^。\n]*[。]?",
        r"[^。\n]*受限于时间[^。\n]*[。]?",
        r"[^。\n]*受限于[^。\n]*水平[^。\n]*[。]?",
        r"[^。\n]*本研究还存在[^。\n]*不足[^。\n]*[。]?",
        r"[^。\n]*还存[^。\n]*不足[^。\n]*[。]?",
        r"[^。\n]*恳请[^。\n]*指正[^。\n]*[。]?",
        r"[^。\n]*敬请[^。\n]*批评[^。\n]*[。]?",
    ]
    for pat in PERSONAL_PATTERNS:
        text = _re.sub(pat, "", text)
    return text


# ── MCU 平台统一（可选）──────────────────────────────────────

def _mcu_normalize_esp_to_spec_mcu(
    body: str,
    section_id: str,
    tech_spec: Optional[dict],
) -> str:
    """配置 mcu_platform_normalize.enabled 且 TechSpec 主控为 STM32 时，将 ESP 替换为该型号。"""
    cfg = get("mcu_platform_normalize") or {}
    if not isinstance(cfg, dict) or not cfg.get("enabled"):
        return body
    if section_id == "refs":
        return body
    only = cfg.get("only_section_ids") or []
    if only and section_id not in only:
        return body
    skip_ids = set(cfg.get("skip_section_ids") or [])
    if section_id in skip_ids:
        return body
    if not tech_spec:
        return body
    mcu = (tech_spec.get("hardware") or {}).get("mcu") or {}
    model = (mcu.get("model") or "").strip()
    if not model or not _re.search(r"STM32", model, _re.I):
        return body
    protect = cfg.get("protect_line_substrings") or [
        "文献", "指出", "该文", "论文中", "对比", "参考文献",
    ]

    def _sub_line(line: str) -> str:
        if any(p in line for p in protect):
            return line
        s = line
        s = _re.sub(r"(?<![A-Za-z0-9_])ESP8266(?![A-Za-z0-9_])", model, s)
        s = _re.sub(r"(?<![A-Za-z0-9_])ESP32(?![A-Za-z0-9_])", model, s)
        return s

    return "\n".join(_sub_line(L) for L in body.split("\n"))


# ── 主入口：全文后处理 ──────────────────────────────────────

def postprocess_manuscript(
    manuscript: Manuscript,
    plan: Optional[WritingPlan] = None,
    term_map: Optional[dict] = None,
    tech_spec: Optional[dict] = None,
    stc_dominant: Optional[str] = None,
) -> Manuscript:
    """
    最终输出前 / 每轮修订后对整篇论文进行全文后处理。

    参数：
        term_map: {"错误术语": "正确术语"} 字典，用于全文替换
        tech_spec: 技术规范（默认使用 manuscript.tech_spec）
        stc_dominant: 来自 build_global_term_map 的 8051 主导型号（None 表示无需正则替换）
    """
    spec = tech_spec if tech_spec is not None else manuscript.tech_spec
    l3a_map = build_l3a_term_map(spec)
    merged_map: dict[str, str] = {}
    merged_map.update(l3a_map)
    if term_map:
        merged_map.update(term_map)

    new_sections = []
    for sec in manuscript.sections:
        body = sec.markdown_body

        # 0a. 清理 body 内部结构污染
        body = _clean_horizontal_rules(body)
        body = _downgrade_body_h1(body)

        # 参考文献、关键词等特殊章节：跳过引用位置修正
        if sec.section_id in ("refs", "keywords"):
            if merged_map:
                for wrong, correct in merged_map.items():
                    body = body.replace(wrong, correct)
            new_sections.append(ManuscriptSection(
                section_id=sec.section_id,
                title=sec.title,
                markdown_body=body,
            ))
            continue

        # 0. 摘要章节：强制删除所有引用标记
        if sec.section_id in ("abstract_zh", "abstract_en"):
            before = body
            body = _re.sub(r"\[\d+(?:[,，]\d+)*\]", "", body)
            body = _re.sub(r"\[(?:\d+[-,，]\d+)+\]", "", body)
            if body != before:
                logger.info("摘要 [%s] 强制删除引用标记", sec.section_id)

        # 0b. 非引用章节：强制删除引用标记
        _enabled_cite = get("citation_enabled_sections") or ["s1", "s2"]
        if sec.section_id not in _enabled_cite and sec.section_id not in ("abstract_zh", "abstract_en", "acknowledgment", "keywords", "refs"):
            before_cs = body
            body = _re.sub(r"\[\d+(?:[,，]\d+)*\]", "", body)
            body = _re.sub(r"\[(?:\d+[-,，]\d+)+\]", "", body)
            if body != before_cs:
                logger.info("章节 [%s] 违规引用已强制删除", sec.section_id)

        # 1. 全文引用位置修正
        body = _fix_citation_position(body)

        # 1b. 冗余标点清理
        body = _clean_double_punctuation(body)

        # 1c. 补全缺失句末标点
        body = _check_missing_punct(body, sec.section_id)

        # 1d. 删除自身目标/设计上的误加引用
        sc_before = body
        body = _re.sub(r"(本(?:文|系统|设计|章|节).{0,15})\[\d+(?:[,，]\d+)*\]", r"\1", body)
        if body != sc_before:
            logger.info("章节 [%s] 删除自身目标误加引用标记", sec.section_id)

        # 2. 章节越界清理
        body = _clean_section_overflow(body, sec.section_id)

        # 3. 术语统一替换（L3-A + 全局 term_map）
        if merged_map:
            for wrong, correct in merged_map.items():
                body = body.replace(wrong, correct)

        body = _mcu_normalize_esp_to_spec_mcu(body, sec.section_id, spec)

        # 3b. 以 STC/8051 为主且文中仍有 STM32F103 等：正则统一子型号
        if stc_dominant:
            _COMPARE_SCOPE = _re.compile(
                r"对比|相比|相较于|不同于|替代方案|而.*(?:方案|系统|平台|芯片)|Arduino|竞品",
            )
            if not _COMPARE_SCOPE.search(body):
                body = _re.sub(
                    r"STM32[A-Za-z0-9]*",
                    stc_dominant,
                    body,
                    flags=_re.I,
                )
            else:
                logger.info(
                    "章节 [%s] 含对比语境，跳过 STM32*→%s 正则替换",
                    sec.section_id, stc_dominant,
                )

        # 4. 删除个人感悟相关句子
        body = _remove_personal_remarks(body)

        # 5. 摘要字数超标时规则截断
        body = _truncate_abstract(body, sec.section_id)

        new_sections.append(ManuscriptSection(
            section_id=sec.section_id,
            title=sec.title,
            markdown_body=body,
        ))

    return Manuscript(
        sections=new_sections,
        cover_text=manuscript.cover_text,
        toc_text=manuscript.toc_text,
        version=manuscript.version,
        thesis_title=manuscript.thesis_title,
        keywords_zh_text=manuscript.keywords_zh_text,
        keywords_en_text=manuscript.keywords_en_text,
        tech_spec=manuscript.tech_spec,
    )


# ── 引用按出现顺序重编号 ────────────────────────────────────

def reorder_citations_by_first_appearance(draft, store) -> dict:
    """
    按正文中引用的首次出现顺序重建编号映射，然后重写所有章节的引用标记。
    返回: 空 dict 表示无需重排，非空 dict 为 {old_num: new_num}
    """
    # Step 1: 扫描全文所有引用
    full_text = ""
    for sec in draft.sections:
        if sec.section_id in ("refs", "keywords"):
            continue
        full_text += sec.markdown_body + "\n"

    cited_in_order: list[int] = []
    seen = set()
    for m in _re.finditer(r"\[(\d+)\]", full_text):
        n = int(m.group(1))
        if n not in seen:
            seen.add(n)
            cited_in_order.append(n)

    if not cited_in_order:
        return {}

    # Step 2: 构建映射表
    all_refs = store.all_refs()
    max_old = len(all_refs)
    old_to_new: dict[int, int] = {}
    for new_num, old_num in enumerate(cited_in_order, 1):
        old_to_new[old_num] = new_num

    next_new = len(cited_in_order) + 1
    for old_num in range(1, max_old + 1):
        if old_num not in old_to_new:
            old_to_new[old_num] = next_new
            next_new += 1

    # Step 3: 替换所有章节正文中的 [n]
    def _replace(m):
        n = int(m.group(1))
        new_n = old_to_new.get(n, n)
        return f"[{new_n}]"

    for sec in draft.sections:
        if sec.section_id in ("refs", "keywords"):
            continue
        sec.markdown_body = _re.sub(r"\[(\d+)\]", _replace, sec.markdown_body)

    logger.info(
        "引用重编号完成：%d 篇被引用 → 重排为 [1]..[%d]，%d 篇未引用排在末尾",
        len(cited_in_order), len(cited_in_order), max_old - len(cited_in_order),
    )
    return old_to_new


# ── 一站式后处理 ─────────────────────────────────────────────

def _finalize_manuscript_postprocess(
    manuscript: Manuscript,
    plan: Optional[WritingPlan] = None,
) -> tuple[Manuscript, dict[str, str]]:
    """
    对已定稿节列表的 Manuscript 依次执行：build_global_term_map → postprocess_manuscript。
    用于初稿后以外的阶段，保证与最终输出使用同一套全文规则。
    """
    result = build_global_term_map(manuscript)
    term_map = result["term_map"]
    finalized = postprocess_manuscript(
        manuscript,
        plan=plan,
        term_map=term_map,
        tech_spec=manuscript.tech_spec,
        stc_dominant=result.get("stc_dominant"),
    )
    return finalized, term_map
