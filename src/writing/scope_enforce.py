"""
章节 scope 校验、配置覆盖大纲、L3-A 轻量术语表（与 TechSpec 传感器型号对齐）。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..config import get
from ..models import SectionNode, WritingPlan

logger = logging.getLogger(__name__)

# 常见「规范型号 → 易混错写」，仅当 TechSpec 锁定 canonical 型号时启用替换
_DEFAULT_MODEL_ALTS: dict[str, list[str]] = {
    "DHT11": ["DS18B20", "DHT-11", "DHT 11"],
    "DS18B20": ["DHT11", "DHT-11", "DHT 11"],
    "YL-69": ["YL69", "YL_69"],
}


def scope_validation_settings() -> dict[str, Any]:
    raw = get("scope_validation") or {}
    if not isinstance(raw, dict):
        raw = {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "retry_once": bool(raw.get("retry_once", True)),
    }


def subsections_sequential_settings() -> dict[str, Any]:
    raw = get("subsections_sequential_draft") or {}
    if not isinstance(raw, dict):
        raw = {}
    return {"enabled": bool(raw.get("enabled", False))}


def l3a_settings() -> dict[str, Any]:
    raw = get("l3a_tech_spec_enforce") or {}
    if not isinstance(raw, dict):
        raw = {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "sensor_alias_table": raw.get("sensor_alias_table") if isinstance(raw.get("sensor_alias_table"), dict) else {},
    }


def build_l3a_term_map(tech_spec: dict | None) -> dict[str, str]:
    """
    从 TechSpec.hardware.sensors[].model 与可选配置合并「错写 → 规范」替换表。
    term_map 合并时后者覆盖前者冲突键：controller 侧将 global term_map 压在 l3a 之上。
    """
    if not tech_spec or not l3a_settings()["enabled"]:
        return {}

    cfg_table = l3a_settings()["sensor_alias_table"]
    term_map: dict[str, str] = {}

    hw = tech_spec.get("hardware") or {}
    sensors = hw.get("sensors") or []
    for s in sensors:
        if not isinstance(s, dict):
            continue
        model = str(s.get("model") or "").strip()
        if not model:
            continue
        alts = list(_DEFAULT_MODEL_ALTS.get(model, []))
        extra = cfg_table.get(model)
        if isinstance(extra, list):
            alts.extend(str(x) for x in extra if x)
        for wrong in alts:
            w = wrong.strip()
            if w and w != model:
                term_map[w] = model

    manual = get("l3a_manual_replacements") or {}
    if isinstance(manual, dict):
        for k, v in manual.items():
            ks, vs = str(k).strip(), str(v).strip()
            if ks and vs:
                term_map[ks] = vs

    return term_map


def section_has_scope_constraints(section: SectionNode) -> bool:
    return bool(
        (section.outline_detail or "").strip()
        or section.scope_must_include
        or section.scope_forbidden
    )


def validate_abstract_against_tech_spec(
    abstract_body: str,
    tech_spec: dict | None,
) -> tuple[bool, list[str]]:
    """
    中文摘要硬校验：与 evaluator 摘要规则及 TechSpec 型号对齐。
    - 禁引用 [n]、禁图表引用（子串规则同 _check_thesis_rules）
    - 若提供 TechSpec：不得出现 L3-A 表中的「错写」键；若主控为 STM32 族则禁 ESP8266/ESP32
    """
    issues: list[str] = []
    text = abstract_body or ""
    if not text.strip():
        return True, []
    if "中文摘要生成失败" in text[:40]:
        return True, []

    if re.search(r"\[\d+\]", text):
        issues.append("摘要中不得出现参考文献引用标记（如[1]）")
    if re.search(r"[图表][\d一二三四五六七八九十]|图\s*\d|表\s*\d", text):
        issues.append("摘要中不得出现图表引用（如图1、表2）")

    if tech_spec:
        l3a_wrong = build_l3a_term_map(tech_spec)
        for wrong in l3a_wrong:
            w = str(wrong).strip()
            if w and w in text:
                issues.append(
                    f"摘要须与 TechSpec 硬件型号一致，不得使用：「{w}」（请改用规范表述）"
                )
        hw = tech_spec.get("hardware") or {}
        mcu = hw.get("mcu") or {}
        mcu_model = str(mcu.get("model") or "")
        if mcu_model and re.search(r"STM32", mcu_model, re.I):
            if re.search(r"(?<![A-Za-z0-9_])ESP8266(?![A-Za-z0-9_])", text):
                issues.append("摘要中不得出现 ESP8266（与 TechSpec 主控 STM32 冲突）")
            if re.search(r"(?<![A-Za-z0-9_])ESP32(?![A-Za-z0-9_])", text):
                issues.append("摘要中不得出现 ESP32（与 TechSpec 主控 STM32 冲突）")

    return (len(issues) == 0, issues)


def validate_section_body_scope(body: str, section: SectionNode) -> tuple[bool, list[str]]:
    """检查正文是否满足 must_include / 是否触犯 forbidden（子串匹配）。"""
    issues: list[str] = []
    text = body or ""
    if "本章节生成失败" in text or "本部分生成失败" in text:
        return True, []
    for phrase in section.scope_must_include:
        p = (phrase or "").strip()
        if p and p not in text:
            issues.append(f"缺少必须包含的内容：「{p}」")
    for phrase in section.scope_forbidden:
        p = (phrase or "").strip()
        if p and p in text:
            issues.append(f"禁止出现的内容被写出：「{p}」")
    return (len(issues) == 0, issues)


def merge_outline_scope_overrides(plan: WritingPlan) -> WritingPlan:
    """将 config outline_scope_overrides 合并进顶层 SectionNode（按 section_id）。"""
    raw = get("outline_scope_overrides") or {}
    if not isinstance(raw, dict) or not raw:
        return plan

    new_outline: list[SectionNode] = []
    touched = 0
    for node in plan.outline:
        ovr = raw.get(node.section_id)
        if not isinstance(ovr, dict):
            new_outline.append(node)
            continue
        touched += 1
        must = list(node.scope_must_include)
        for x in ovr.get("scope_must_include") or []:
            xs = str(x).strip()
            if xs and xs not in must:
                must.append(xs)
        forb = list(node.scope_forbidden)
        for x in ovr.get("scope_forbidden") or []:
            xs = str(x).strip()
            if xs and xs not in forb:
                forb.append(xs)
        od_new = node.outline_detail or ""
        if ovr.get("outline_detail") is not None:
            od_add = str(ovr.get("outline_detail") or "").strip()
            if od_add:
                od_new = (od_new + "\n" + od_add).strip()
        new_outline.append(
            node.model_copy(
                update={
                    "scope_must_include": must,
                    "scope_forbidden": forb,
                    "outline_detail": od_new,
                }
            )
        )
    if touched:
        logger.info("已从 outline_scope_overrides 合并 %d 个顶层章节约束", touched)
    return plan.model_copy(update={"outline": new_outline})


def flatten_subsections_depth_first(nodes: list[SectionNode]) -> list[SectionNode]:
    out: list[SectionNode] = []
    for n in nodes:
        out.append(n)
        if n.subsections:
            out.extend(flatten_subsections_depth_first(n.subsections))
    return out
