"""Markdown fixture 解析器 — 将带 YAML front matter 的 .md 文件转为静态规则测试用例"""

import os
import re
import yaml
from pathlib import Path
from typing import Any

from src.models import Manuscript, ManuscriptSection, WritingPlan, SectionNode
from src.validation.evaluator import _check_thesis_rules

_HERE = Path(__file__).resolve().parent
_FIXTURES = _HERE / "fixtures"


class FixtureError(Exception):
    pass


def _parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """解析 Markdown 顶部的 YAML front matter。"""
    text = text.lstrip("\ufeff")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        raise FixtureError("缺少 YAML front matter（--- ... ---）")
    raw = m.group(1)
    meta = yaml.safe_load(raw)
    if not isinstance(meta, dict):
        raise FixtureError("front matter 必须是 YAML 字典")
    body = text[m.end():]
    return meta, body


def _build_manuscript_sections(body: str) -> list[ManuscriptSection]:
    """按 ## 标题将 body 切分为 ManuscriptSection 列表。"""
    sections: list[ManuscriptSection] = []
    parts = re.split(r"\n(?=##\s+)", body)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^##\s+(.+?)\n(.*)", part, re.DOTALL)
        if m:
            title = m.group(1).strip()
            content = m.group(2).strip()
            section_id = title_to_section_id(title)
            sections.append(ManuscriptSection(
                section_id=section_id,
                title=title,
                markdown_body=content,
            ))
    return sections


def title_to_section_id(title: str) -> str:
    """将常见章标题映射到 section_id。"""
    if re.match(r"^第\s*[1-6]\s*章", title):
        return f"s{title.strip()[1]}"
    mapping = {
        "摘要": "abstract_zh", "摘要": "abstract_zh",
        "Abstract": "abstract_en", "英文摘要": "abstract_en",
        "关键词": "keywords", "关键词": "keywords",
        "参考文献": "refs",
        "致谢": "acknowledgment",
        "附录": "appendix",
    }
    return mapping.get(title.strip(), title.strip())


def _build_minimal_plan(sections: list[ManuscriptSection]) -> WritingPlan:
    """从 sections 构造最小 WritingPlan。"""
    nodes = []
    for sec in sections:
        if sec.section_id.startswith("s") and sec.section_id[1:].isdigit():
            nodes.append(SectionNode(section_id=sec.section_id, title=sec.title))
    if not nodes:
        nodes.append(SectionNode(section_id="s1", title="引言"))
    return WritingPlan(outline=nodes, keywords=["测试"], search_queries=["测试"])


def run_fixture(filepath: Path) -> tuple[bool, str, set[str], set[str], set[str]]:
    """
    执行单个 fixture 文件。
    返回: (passed, case_id, expected, actual, unexpected)
    """
    text = filepath.read_text(encoding="utf-8")
    meta, body = _parse_front_matter(text)

    case_id = meta.get("case_id", filepath.stem)
    expected_rules = set(meta.get("expected_rules", []))
    forbidden_rules = set(meta.get("forbidden_rules", []))
    exact = meta.get("exact_rules", False)

    if not case_id:
        raise FixtureError("缺少 case_id")

    sections = _build_manuscript_sections(body)
    if not sections:
        raise FixtureError(f"{case_id}: 未能解析出任何章节")

    plan = _build_minimal_plan(sections)
    ms = Manuscript(sections=sections, version=1)
    issues = _check_thesis_rules(ms, plan)
    actual_ids = {i.rule_id for i in issues}

    missing = expected_rules - actual_ids
    unexpected = forbidden_rules & actual_ids

    if exact:
        passed = (actual_ids == expected_rules) and len(unexpected) == 0
    else:
        passed = len(missing) == 0 and len(unexpected) == 0

    detail = ""
    if missing:
        detail += f" 缺少: {sorted(missing)}"
    if unexpected:
        if detail:
            detail += "; "
        detail += f" 误报: {sorted(unexpected)}"

    return passed, case_id, expected_rules, actual_ids, unexpected if not exact else set()


def run_all_fixtures(fixture_dir: Path | None = None) -> bool:
    """
    运行所有 fixture 目录下的 .md 文件。
    返回 True 表示全部通过。
    """
    if fixture_dir is None:
        fixture_dir = _FIXTURES

    all_pass = True
    tested = 0

    for root, _, files in os.walk(fixture_dir):
        for fname in sorted(files):
            if not fname.endswith(".md"):
                continue
            if fname == "README.md":
                continue
            fpath = Path(root) / fname
            try:
                passed, case_id, _, _, _ = run_fixture(fpath)
                status = "✅" if passed else "❌"
                print(f"  {status} {case_id}")
                if not passed:
                    all_pass = False
                tested += 1
            except FixtureError as e:
                print(f"  ⚠ SKIP {fname}: {e}")
            except Exception as e:
                print(f"  ❌ ERROR {fname}: {e}")
                all_pass = False

    if tested == 0:
        print("  ⚠ 未找到 fixture 文件")
        return True

    print(f"  fixtures: {tested} files")
    return all_pass
