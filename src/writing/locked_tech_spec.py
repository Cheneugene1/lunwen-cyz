"""
用户锁定技术规范（L1）

与 LLM 生成的 TechSpec 形状兼容的 JSON 片段；通过 deep-merge 合并，
同级键由锁定文件覆盖 LLM 结果（ dict 递归合并，列表等非 dict 类型整段替换）。

锁定文件中以下划线开头的顶层键（如 _comment、_meta）仅作人类说明，不参与合并。
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

from ..config import ROOT, deep_merge_dicts, get

logger = logging.getLogger(__name__)


def _strip_meta_keys(fragment: dict[str, Any]) -> dict[str, Any]:
    """去掉以下划线开头的顶层键，避免把说明字段 merge 进 TechSpec。"""
    return {
        k: v
        for k, v in fragment.items()
        if not (isinstance(k, str) and k.startswith("_"))
    }


def load_locked_tech_spec(path: str | None = None) -> dict[str, Any]:
    """
    从 JSON 文件加载用户锁定的 TechSpec 片段。
    path 为 None 时使用配置项 locked_tech_spec_path；为 "" 时不读文件（可用来覆盖配置、仅用 LLM）。
    相对路径相对于项目 ROOT。
    文件不存在或为空时返回 {}。
    """
    raw_path = path if path is not None else get("locked_tech_spec_path", "") or ""
    raw_path = str(raw_path).strip()
    if not raw_path:
        return {}

    p = Path(raw_path)
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        logger.info("用户锁定 TechSpec 文件不存在，跳过：%s", p)
        return {}

    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning("读取锁定 TechSpec 失败（将忽略锁定层）: %s", e)
        return {}

    if not isinstance(data, dict):
        logger.warning("锁定 TechSpec 须为 JSON 对象，已忽略")
        return {}
    return data


def locked_layer_nonempty(locked_file: dict[str, Any] | None) -> bool:
    """锁定文件是否包含有效合并字段（排除顶层 `_` 元数据键）。"""
    if not locked_file:
        return False
    return bool(_strip_meta_keys(locked_file))


def merge_tech_specs(llm_spec: dict[str, Any] | None, locked_file: dict[str, Any] | None) -> dict[str, Any]:
    """
    将锁定片段合并进 LLM 生成的 TechSpec。
    锁定层优先：与 config.deep_merge_dicts 语义一致。
    """
    base = copy.deepcopy(llm_spec) if llm_spec else {}
    if not locked_file:
        return base
    locked = _strip_meta_keys(locked_file)
    if not locked:
        return base
    return deep_merge_dicts(base, locked)
