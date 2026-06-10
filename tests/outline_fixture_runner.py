"""YAML outline fixture 解析器 — 测试 planner 的 28 条硬规则"""

import os
import yaml
from pathlib import Path
from typing import Any

from src.models import WritingPlan, SectionNode
from src.planner import _check_outline_hard_rules

_HERE = Path(__file__).resolve().parent
_OUTLINE_FIXTURES = _HERE / "fixtures" / "outline_rules"


class OutlineFixtureError(Exception):
    pass


def _yaml_to_writing_plan(data: dict[str, Any]) -> WritingPlan:
    """将 YAML 字典转为 WritingPlan。"""
    outline_raw = data.get("outline", [])
    keywords = data.get("keywords", [])
    search_queries = data.get("search_queries", keywords[:])

    nodes: list[SectionNode] = []
    for item in outline_raw:
        section_id = item["section_id"]
        title = item.get("title", "")
        bullets = item.get("bullets", [])
        subsections_raw = item.get("subsections", [])
        subsections = [
            SectionNode(
                section_id=s["section_id"],
                title=s.get("title", ""),
                bullets=s.get("bullets", []),
            )
            for s in subsections_raw
        ] if subsections_raw else []
        nodes.append(SectionNode(
            section_id=section_id,
            title=title,
            bullets=bullets,
            subsections=subsections,
        ))
    return WritingPlan(outline=nodes, keywords=keywords, search_queries=search_queries)


def run_outline_fixture(filepath: Path) -> tuple[bool, str, set[str], set[str]]:
    """执行单个 outline YAML fixture。"""
    text = filepath.read_text(encoding="utf-8")
    meta_raw, _, body_raw = text.partition("\n---\n")
    if not meta_raw.startswith("#"):
        # no front matter, entire file is YAML
        meta = yaml.safe_load(text)
    else:
        meta = yaml.safe_load(meta_raw.lstrip("#").strip())
        body_meta = yaml.safe_load(body_raw.strip())
        if isinstance(body_meta, dict):
            meta.update(body_meta)

    if not isinstance(meta, dict):
        raise OutlineFixtureError("YAML 格式错误：期望字典")

    case_id = meta.get("case_id", filepath.stem)
    expected_rules = set(meta.get("expected_rules", []))
    forbidden_rules = set(meta.get("forbidden_rules", []))
    thesis_mode = meta.get("thesis_mode", True)

    if not expected_rules and not forbidden_rules:
        raise OutlineFixtureError(f"{case_id}: 缺少 expected_rules / forbidden_rules")

    plan = _yaml_to_writing_plan(meta)
    issues = _check_outline_hard_rules(plan, thesis_mode)
    actual_ids = {i["rule_id"] for i in issues}

    missing = expected_rules - actual_ids
    unexpected = forbidden_rules & actual_ids

    passed = len(missing) == 0 and len(unexpected) == 0
    detail = ""
    if missing:
        detail += f" 缺少: {sorted(missing)}"
    if unexpected:
        if detail:
            detail += "; "
        detail += f" 误报: {sorted(unexpected)}"

    if not passed:
        print(f"  ❌ {case_id}{detail}")
    else:
        print(f"  ✅ {case_id}")

    return passed, case_id, missing, unexpected


def run_all_outline_fixtures(fixture_dir: Path | None = None) -> bool:
    if fixture_dir is None:
        fixture_dir = _OUTLINE_FIXTURES

    if not fixture_dir.exists():
        print("  ⚠ outline fixtures 目录不存在，跳过")
        return True

    all_pass = True
    tested = 0
    for root, _, files in os.walk(fixture_dir):
        for fname in sorted(files):
            if not fname.endswith(".yaml") and not fname.endswith(".yml"):
                continue
            if fname == "README.md":
                continue
            fpath = Path(root) / fname
            try:
                passed, case_id, missing, unexpected = run_outline_fixture(fpath)
                if not passed:
                    all_pass = False
                tested += 1
            except Exception as e:
                print(f"  ❌ ERROR {fname}: {e}")
                all_pass = False

    if tested == 0:
        print("  ⚠ 未找到 outline fixture 文件")
        return True

    print(f"  outline fixtures: {tested} files")
    return all_pass
