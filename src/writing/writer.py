"""
撰写模块 — 按 WritingPlan 中的大纲逐章生成与修订学术 Markdown 论文正文。

实现已拆分到子模块：
  draft_engine.py   — 初稿生成（draft_manuscript）
  revision_engine.py — 修订（revise_manuscript / stubborn_targeted_fix / check_revision_compliance）
  postprocess.py     — 全文后处理（postprocess_manuscript / reorder_citations / _finalize_...）
  term_map.py        — 术语映射（build_global_term_map）
  abstract.py        — 摘要生成（_generate_abstract_from_body）
  helpers.py         — 纯工具函数与常量

本文件仅保留：
  parse_manuscript_from_md — 从 paper_*.md 反解析 Manuscript（用于 --phase eval/revise 调试）
"""
import re
from pathlib import Path

from ..models import Manuscript, ManuscriptSection, WritingPlan


def parse_manuscript_from_md(
    md_path: str,
    plan: WritingPlan,
    version: int = 1,
) -> Manuscript:
    """
    从已生成的 paper_*.md 反解析 Manuscript。
    依赖 WritingPlan 的 outline 做 section_id 映射。
    用于 --phase eval/revise 等单步调试场景。
    """
    text = Path(md_path).read_text(encoding="utf-8")

    # 提取 H1 标题（# xxx）
    h1_match = re.match(r"^#\s+(.+?)$", text, re.MULTILINE)
    thesis_title = h1_match.group(1).strip() if h1_match else (plan.title or "")

    # 构建 title → section_id 映射
    title_to_id: dict[str, str] = {}
    for node in plan.outline:
        title_to_id[node.title] = node.section_id

    # 按 --- 分隔符切分
    parts = text.split("\n\n---\n\n")

    cover_text = ""
    toc_text = ""
    sections: list[ManuscriptSection] = []
    seen_first_heading = False

    for part in parts:
        part = part.strip()
        if not part:
            continue

        m = re.match(r"^##\s+(.+?)\n\n(.*)", part, re.DOTALL)
        if m:
            seen_first_heading = True
            title = m.group(1).strip()
            body = m.group(2).strip()
            section_id = title_to_id.get(title)
            if not section_id:
                # 备选：固定章节映射
                if title == "参考文献":
                    section_id = "refs"
                elif title == "致谢":
                    section_id = "acknowledgment"
                elif title == "关键词":
                    section_id = "keywords"
            if section_id:
                sections.append(ManuscriptSection(
                    section_id=section_id,
                    title=title,
                    markdown_body=body,
                ))
        elif not seen_first_heading:
            if not cover_text:
                cover_text = part
            elif not toc_text:
                toc_text = part

    return Manuscript(
        sections=sections,
        cover_text=cover_text,
        toc_text=toc_text,
        version=version,
        thesis_title=thesis_title,
    )
