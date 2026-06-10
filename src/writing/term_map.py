"""
全文术语映射 — 扫描 Manuscript 全文，识别常见术语矛盾并构建替换字典。

返回格式：{"term_map": {...}, "stc_dominant": str|None}
不再使用模块级全局变量，消除并行 DRAFT/REVISE 下的覆盖隐患。
"""
import logging
import re as _re
from typing import Optional

from ..models import Manuscript

logger = logging.getLogger(__name__)


def _extract_dominant_term(manuscript: Manuscript, candidates: list[str]) -> str | None:
    """
    在全文中统计 candidates 列表中各词出现频次，返回出现最多的词。
    若最高频次并列则不做自动统一（返回 None），避免误判。
    """
    all_text = " ".join(s.markdown_body for s in manuscript.sections)
    counts = {term: all_text.count(term) for term in candidates}
    if not counts or max(counts.values()) == 0:
        return None
    max_c = max(counts.values())
    tops = [t for t, c in counts.items() if c == max_c]
    if len(tops) > 1:
        logger.warning(
            "术语频次并列，跳过自动统一（避免错误替换）: %s 均为 %d 次",
            tops, max_c,
        )
        return None
    return tops[0]


def build_global_term_map(manuscript: Manuscript) -> dict:
    """
    扫描全文，自动识别常见的术语矛盾并构建替换字典。

    返回：{"term_map": {旧词: 新词, ...}, "stc_dominant": str | None}
    stc_dominant 为 None 或 STC89C52/AT89C52 等 8051 系列型号；
    postprocess_manuscript 据此决定是否对正文做 STM32*→8051 正则替换。

    当前处理的矛盾类型：
    - 温度传感器：DS18B20 vs DHT11
    - 传感器类型：电容式 vs 电阻式（YL-69）
    - 主控 / MCU：STM32 vs 8051 / ESP32 vs ESP8266 / 显示模块 OLED vs LCD1602
    - TechSpec 驱动动态术语（candidate_models 字段）
    """
    term_map: dict[str, str] = {}
    stc_dominant: Optional[str] = None
    all_text = " ".join(s.markdown_body for s in manuscript.sections)

    # 矛盾组1：温度传感器型号
    dominant = _extract_dominant_term(manuscript, ["DHT11", "DS18B20"])
    if dominant and all_text.count("DHT11") > 0 and all_text.count("DS18B20") > 0:
        minority = "DS18B20" if dominant == "DHT11" else "DHT11"
        logger.info(
            "全文术语统一：%s → %s（出现次数 %d vs %d）",
            minority, dominant, all_text.count(minority), all_text.count(dominant),
        )
        term_map[minority] = dominant

    # 矛盾组2：YL-69 传感器类型
    cap_count = all_text.count("电容式土壤湿度传感器")
    res_count = all_text.count("电阻式土壤湿度传感器")
    if cap_count > 0 and res_count > 0:
        if cap_count == res_count:
            logger.warning(
                "「电容式/电阻式土壤湿度传感器」出现次数相同（各 %d 次），跳过自动统一",
                cap_count,
            )
        elif res_count > cap_count:
            logger.info("全文术语统一：'电容式土壤湿度传感器' → '电阻式土壤湿度传感器'")
            term_map["电容式土壤湿度传感器"] = "电阻式土壤湿度传感器"
        else:
            logger.info("全文术语统一：'电阻式土壤湿度传感器' → '电容式土壤湿度传感器'")
            term_map["电阻式土壤湿度传感器"] = "电容式土壤湿度传感器"

    # ── 矛盾组3a：STM32 家族 vs 8051 家族 ──
    _8051 = {"STC89C52", "STC89C51", "AT89C52", "AT89C51"}
    grp_a: dict[str, int] = {}
    grp_a["STM32"] = len(_re.findall(r"STM32[A-Za-z0-9]*", all_text, _re.I))
    for lab, sub in (
        ("STC89C52", "STC89C52"),
        ("STC89C51", "STC89C51"),
        ("AT89C52", "AT89C52"),
        ("AT89C51", "AT89C51"),
    ):
        grp_a[lab] = all_text.count(sub)
    present_a = {k: v for k, v in grp_a.items() if v > 0}
    if len(present_a) >= 2:
        spec_mcu_model = ""
        if manuscript.tech_spec:
            spec_mcu = (manuscript.tech_spec.get("hardware") or {}).get("mcu") or {}
            spec_mcu_model = (spec_mcu.get("model") or "").strip()
        if spec_mcu_model and spec_mcu_model in present_a:
            dom_a = spec_mcu_model
        else:
            max_a = max(present_a.values())
            tops_a = [k for k, v in present_a.items() if v == max_a]
            if len(tops_a) > 1:
                logger.warning(
                    "STM32/8051 家族术语频次并列，跳过自动统一: %s 均为 %d 次",
                    tops_a, max_a,
                )
                dom_a = None
            else:
                dom_a = tops_a[0]
        if dom_a:
            for k in present_a:
                if k == dom_a:
                    continue
                if dom_a in _8051 and k == "STM32":
                    continue  # STM32* 由 stc_dominant 正则统一
                term_map[k] = dom_a
                logger.info("全文术语统一（主控/STM32-8051族）: %s → %s", k, dom_a)
            if dom_a in _8051 and present_a.get("STM32", 0) > 0:
                stc_dominant = dom_a

    # ── 矛盾组3b：ESP32 vs ESP8266 ──
    grp_b = {
        "ESP32": len(
            _re.findall(r"(?<![A-Za-z0-9_])ESP32(?![A-Za-z0-9_])", all_text)
        ),
        "ESP8266": len(
            _re.findall(r"(?<![A-Za-z0-9_])ESP8266(?![A-Za-z0-9_])", all_text)
        ),
    }
    present_b = {k: v for k, v in grp_b.items() if v > 0}
    if len(present_b) >= 2:
        max_b = max(present_b.values())
        tops_b = [k for k, v in present_b.items() if v == max_b]
        if len(tops_b) > 1:
            logger.warning("ESP 系列频次并列，跳过自动统一: %s 均为 %d 次", tops_b, max_b)
        else:
            dom_b = tops_b[0]
            for k in present_b:
                if k != dom_b:
                    term_map[k] = dom_b
                    logger.info("全文术语统一（ESP 系列）: %s → %s", k, dom_b)

    # ── 矛盾组4：显示模块型号 ──
    _DISPLAY_VARIANTS = [
        "LCD1602", "LCD 1602", "LCD2004", "LCD 2004",
        "OLED 128x64", "OLED 128×64", "OLED128x64",
        "LCD12864", "12864 LCD", "LCD 12864", "12864",
    ]
    display_counts = {v: all_text.count(v) for v in _DISPLAY_VARIANTS}
    present_disp = {k: v for k, v in display_counts.items() if v > 0}
    if len(present_disp) >= 2:
        max_d = max(present_disp.values())
        tops_d = [k for k, v in present_disp.items() if v == max_d]
        if len(tops_d) == 1:
            dom_d = tops_d[0]
            for k in present_disp:
                if k != dom_d:
                    term_map[k] = dom_d
                    logger.info("全文术语统一（显示模块）: %s → %s", k, dom_d)
        else:
            logger.warning("显示模块频次并列，跳过自动统一: %s", tops_d)

    # ── 矛盾组5：TechSpec 动态提取 ──
    if manuscript.tech_spec:
        hw = (manuscript.tech_spec.get("hardware") or {})
        sensors = hw.get("sensors", []) or []
        for s in sensors:
            canonical = (s.get("model") or "").strip()
            if not canonical:
                continue
            candidate = s.get("candidate_models", [])
            for alt in (candidate if isinstance(candidate, list) else []):
                if alt in all_text and alt != canonical:
                    term_map[alt] = canonical
                    logger.info("全文术语统一（TechSpec 动态-传感器）: %s → %s", alt, canonical)
        actuators = hw.get("actuators", [])
        for a in actuators:
            spec_val = (a.get("spec") or a.get("model") or "").strip()
            if not spec_val:
                continue
            candidate = a.get("candidate_models", [])
            for alt in (candidate if isinstance(candidate, list) else []):
                if alt in all_text and alt != spec_val:
                    term_map[alt] = spec_val
                    logger.info("全文术语统一（TechSpec 动态-执行器）: %s → %s", alt, spec_val)
        comm = hw.get("communication_module", {}) or {}
        if comm:
            comm_model = (comm.get("model") or "").strip()
            candidate = comm.get("candidate_models", [])
            if comm_model:
                for alt in (candidate if isinstance(candidate, list) else []):
                    if alt in all_text and alt != comm_model:
                        term_map[alt] = comm_model
                        logger.info("全文术语统一（TechSpec 动态-通信）: %s → %s", alt, comm_model)

    return {"term_map": term_map, "stc_dominant": stc_dominant}
