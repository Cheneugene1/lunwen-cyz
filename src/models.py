"""
核心数据契约 — Pydantic v2 模型
涵盖所有模块间传递的数据结构
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# 1. 文档解析结果
# ─────────────────────────────────────────────

class DocumentBlock(BaseModel):
    """文档解析后的最小信息单元（段落、表格、图注等）"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source_file: str                                  # 来源文件名
    type: Literal["paragraph", "table", "image_caption", "other"] = "paragraph"
    text: str                                         # 文本内容
    page: Optional[int] = None                        # 所在页码（可选）
    metadata: dict = Field(default_factory=dict)      # 扩展元数据


class DocumentBundle(BaseModel):
    """一批文档解析结果 + 处理标志"""
    blocks: List[DocumentBlock] = Field(default_factory=list)
    flags: dict = Field(default_factory=dict)

    # 便捷属性：汇总全部文本
    def full_text(self, max_chars: int = 0) -> str:
        text = "\n\n".join(b.text for b in self.blocks if b.text.strip())
        if max_chars and len(text) > max_chars:
            return text[:max_chars]
        return text

    # 标志判断快捷方法
    @property
    def is_long_document(self) -> bool:
        return bool(self.flags.get("long_document"))

    @property
    def truncation_message(self) -> str:
        return self.flags.get(
            "truncation_message",
            "文档超过长度上限，已按策略仅采用部分内容参与后续规划；全文未全部进入模型。"
        )


# ─────────────────────────────────────────────
# 2. 参考文献
# ─────────────────────────────────────────────

class Reference(BaseModel):
    """单条参考文献"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str
    authors: List[str] = Field(default_factory=list)
    year: Optional[str] = None
    venue: Optional[str] = None                       # 期刊/会议名
    doi: Optional[str] = None
    url: Optional[str] = None
    abstract: Optional[str] = None
    language: Optional[str] = None                    # "zh" / "en" 等
    source_tag: Literal[
        "manual_file", "openalex", "crossref", "arxiv", "semantic_scholar", "unknown"
    ] = "unknown"
    pinned: bool = False                              # 手动导入的文献为 True
    low_confidence: bool = False                      # 缺关键字段时为 True
    raw: Optional[str] = None                         # 原始文本（便于调试）

    @field_validator("year", mode="before")
    @classmethod
    def coerce_year(cls, v: Any) -> Optional[str]:
        """将整数年份统一为字符串"""
        if v is None:
            return None
        return str(v)

    def formatted_citation(self, style: str = "author_year") -> str:
        """生成简单引用字符串（正文内联用）"""
        if style == "numeric":
            return f"[{self.id[:6]}]"
        # author_year 风格
        first_author = self.authors[0].split()[-1] if self.authors else "Unknown"
        year = self.year or "n.d."
        return f"({first_author}, {year})"


# ─────────────────────────────────────────────
# 3. 写作规划
# ─────────────────────────────────────────────

class SectionNode(BaseModel):
    """大纲中的一个章节节点（可为树：子节点仅用于规划与 prompt 约束，撰写仍按顶层逐章生成）。"""
    section_id: str
    title: str
    bullets: List[str] = Field(default_factory=list)  # 该节要点
    # ── 可执行大纲（规划阶段细化，撰写阶段注入 prompt，降低越界与结构漂移）──
    outline_detail: str = ""
    """段落级展开顺序与写作说明（1～数句），与 bullets 互补。"""
    scope_must_include: List[str] = Field(default_factory=list)
    """本章必须覆盖的主题/术语（短句）。"""
    scope_forbidden: List[str] = Field(default_factory=list)
    """本章禁止展开的话题（如「完整源代码」「第4章实验数据」）。"""
    subsections: List["SectionNode"] = Field(default_factory=list)
    """可选子结构：建议在正文用 ### 小标题对齐，不得写到子结构范围之外。"""


class WritingPlan(BaseModel):
    """完整写作规划：大纲 + 检索词 + 关联文献"""
    title: str = ""              # 论文中文题目（供封面/目录使用）
    title_en: str = ""           # 论文英文题目（供封面使用）
    outline: List[SectionNode] = Field(default_factory=list)
    keywords: List[str] = Field(default_factory=list)       # 中文关键词（3-5个）
    keywords_en: List[str] = Field(default_factory=list)    # 英文关键词（与中文一一对应）
    search_queries: List[str] = Field(default_factory=list)
    manual_ref_ids: List[str] = Field(default_factory=list)  # 优先覆盖的手动文献


# ─────────────────────────────────────────────
# 4. 论文草稿
# ─────────────────────────────────────────────

class ManuscriptSection(BaseModel):
    """单个章节内容"""
    section_id: str
    title: str
    markdown_body: str


_CHAPTER_SECTION_IDS = {"s1", "s2", "s3", "s4", "s5", "s6"}

def _normalize_chapter_title(section_id: str, title: str) -> str:
    """
    确保 s1-s6 的章标题带「第X章」前缀。
    已有则不重复追加；缺失则补齐。
    """
    if section_id not in _CHAPTER_SECTION_IDS:
        return title
    ch_num = int(section_id[1:])
    prefix = f"第{ch_num}章"
    stripped = re.sub(r"^(?:第\d+章\s*)+", "", title).strip()
    normalized = f"{prefix} {stripped}" if stripped else prefix
    if normalized != title:
        logger.debug("章标题规范化: %r → %r", title, normalized)
    return normalized


class Manuscript(BaseModel):
    """完整论文草稿"""
    sections: List[ManuscriptSection] = Field(default_factory=list)
    version: int = 1
    thesis_title: str = ""  # 论文标题（用于 to_markdown 顶部 # H1 锚点）
    cover_text: str = ""   # 封面 Markdown（由 cover.render_cover() 生成）
    toc_text: str = ""     # 目录 Markdown（由 cover.render_toc() 生成）
    keywords_zh_text: str = ""  # 中文关键词文本（拼接在中文摘要末尾，不含"关键词："前缀则自动补）
    keywords_en_text: str = ""  # 英文关键词文本（拼接在英文摘要末尾）
    # 毕业论文模式：合并后的 TechSpec（LLM + 用户锁定），供修订阶段注入 prompt
    tech_spec: Optional[dict] = None

    def to_markdown(self) -> str:
        """
        拼接为完整 Markdown 文本。
        顺序：H1 文档标题 → 封面 → 目录 → 各章节内容
        关键词会拼接到对应摘要末尾。
        """
        parts = []

        if self.thesis_title:
            parts.append(f"# {self.thesis_title}")

        if self.cover_text:
            parts.append(self.cover_text)

        if self.toc_text:
            parts.append(self.toc_text)

        for sec in self.sections:
            if sec.section_id == "abstract_zh":
                body = sec.markdown_body
                if self.keywords_zh_text:
                    body = body.rstrip() + "\n\n" + self.keywords_zh_text
                parts.append(f"## {sec.title}\n\n{body}")
            elif sec.section_id == "abstract_en":
                body = sec.markdown_body
                if self.keywords_en_text:
                    body = body.rstrip() + "\n\n" + self.keywords_en_text
                parts.append(f"## {sec.title}\n\n{body}")
            elif sec.section_id == "refs":
                parts.append(f"## 参考文献\n\n{sec.markdown_body}")
            elif sec.section_id == "acknowledgment":
                parts.append(f"## 致谢\n\n{sec.markdown_body}")
            else:
                display_title = _normalize_chapter_title(sec.section_id, sec.title)
                parts.append(f"## {display_title}\n\n{sec.markdown_body}")

        return "\n\n".join(parts)


# ─────────────────────────────────────────────
# 5. 质量评估
# ─────────────────────────────────────────────

class EvaluationDimensions(BaseModel):
    """各维度得分（0–10）"""
    structure: float = 0.0    # 结构完整性
    logic: float = 0.0        # 逻辑严密性
    language: float = 0.0     # 语言规范性
    alignment: float = 0.0    # 与用户需求匹配度


class StaticRuleIssue(BaseModel):
    """毕业论文静态规则：rule_id + rule_version 唯一标识一条「规则实例」；severity 控制 stop_on_rule_pass。"""

    rule_id: str
    message: str
    rule_version: str = "1"
    rule_category: str = "general"
    severity: Literal["error", "warning"] = "error"


class Evaluation(BaseModel):
    """质量评估结果（必须经 schema 校验）"""
    score_total: float = Field(ge=0, le=10)
    dimensions: EvaluationDimensions = Field(default_factory=EvaluationDimensions)
    feedback: str = ""
    actionable_items: List[str] = Field(default_factory=list)
    # 毕业论文静态规则（_check_thesis_rules）；非 thesis 模式恒为空列表
    static_rule_issues: List[StaticRuleIssue] = Field(default_factory=list)

    @field_validator("static_rule_issues", mode="before")
    @classmethod
    def _coerce_static_rule_issues(cls, v: Any) -> Any:
        """兼容旧数据：纯字符串列表视为 rule_id=legacy_unknown。"""
        if not v:
            return []
        if isinstance(v, list) and v and isinstance(v[0], str):
            return [
                {
                    "rule_id": f"legacy_{i}",
                    "message": s,
                    "rule_version": "1",
                    "rule_category": "general",
                    "severity": "error",
                }
                for i, s in enumerate(v)
                if isinstance(s, str) and s.strip()
            ]
        return v


# ─────────────────────────────────────────────
# 6. 毕业论文规范配置（从 config 加载后填充）
# ─────────────────────────────────────────────

class ThesisConfig(BaseModel):
    """
    毕业论文格式规范，驱动写作模块生成符合要求的内容。
    由 config.py 加载后实例化，传入 writer / evaluator。
    """
    thesis_mode: bool = False
    thesis_type: str = "computer_software"   # 影响字数下限
    thesis_category: str = "论文"            # 或 "设计"，影响页眉
    thesis_school: str = "沈阳工业大学"

    # 字数要求
    min_words_total: int = 8000
    target_words_min: int = 15000
    target_words_max: int = 30000

    # 各章节目标字数（section_id → 目标字数）
    section_words: dict = Field(default_factory=lambda: {
        "abstract_zh":    600,
        "abstract_en":    400,
        "introduction":   2000,
        "related_work":   3000,
        "design":         4000,
        "implementation": 4000,
        "results":        3000,
        "conclusion":     1000,
        "acknowledgment":  300,
    })

    # 文献要求
    min_references: int = 20
    min_foreign_references: int = 5

    # 关键词数量
    keywords_min: int = 3
    keywords_max: int = 5

    # 引用格式（毕业论文强制 numeric）
    citation_style: str = "numeric"

    @classmethod
    def from_config(cls) -> "ThesisConfig":
        """从全局 config 加载毕业论文配置"""
        from .config import get
        thesis_type = get("thesis_type", "computer_software")
        min_words_map = get("thesis_min_words", {})
        min_words_total = min_words_map.get(thesis_type, 8000)
        section_words_raw = get("thesis_section_words", {})

        return cls(
            thesis_mode=bool(get("thesis_mode", False)),
            thesis_type=thesis_type,
            thesis_category=get("thesis_category", "论文"),
            thesis_school=get("thesis_school", "沈阳工业大学"),
            min_words_total=min_words_total,
            target_words_min=int(get("thesis_target_words_min", 15000)),
            target_words_max=int(get("thesis_target_words_max", 30000)),
            section_words=section_words_raw or {
                "abstract_zh": 600, "abstract_en": 400,
                "introduction": 2000, "related_work": 3000,
                "design": 4000, "implementation": 4000,
                "results": 3000, "conclusion": 1000, "acknowledgment": 300,
            },
            min_references=int(get("min_references", 20)),
            min_foreign_references=int(get("min_foreign_references", 5)),
            keywords_min=int(get("thesis_keywords_min", 3)),
            keywords_max=int(get("thesis_keywords_max", 5)),
            citation_style=get("citation_style", "numeric"),
        )
