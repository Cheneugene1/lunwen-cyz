"""
配置加载模块
加载顺序：config.example.yml 默认值 → local.secrets.yml 覆盖 → 环境变量再覆盖
"""

import os
import copy
from pathlib import Path
from typing import Any

import yaml

# 项目根目录（本文件在 src/ 下，所以向上一层）
ROOT = Path(__file__).parent.parent
CONFIG_EXAMPLE = ROOT / "config" / "config.example.yml"
CONFIG_LOCAL   = ROOT / "config" / "local.secrets.yml"

# 环境变量前缀映射：ENV_VAR → 配置键
_ENV_MAP = {
    "DEEPSEEK_API_KEY":   "deepseek_api_key",
    "DEEPSEEK_BASE_URL":  "deepseek_base_url",
    "DEEPSEEK_MODEL":     "deepseek_model",
    "DEEPSEEK_TIMEOUT_CONNECT": "deepseek_timeout_connect",
    "DEEPSEEK_TIMEOUT_READ_BLOCKING": "deepseek_timeout_read_blocking",
    "DEEPSEEK_TIMEOUT_READ_STREAM": "deepseek_timeout_read_stream",
    "CROSSREF_MAILTO":    "crossref_mailto",
    "LUNWENCYZ_LOCKED_TECH_SPEC": "locked_tech_spec_path",
}


def _load_yaml(path: Path) -> dict:
    """安全加载 YAML，文件不存在时返回空字典"""
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _deep_merge(base: dict, override: dict) -> dict:
    """将 override 递归合并到 base（override 优先）"""
    result = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def deep_merge_dicts(base: dict, override: dict) -> dict:
    """
    对外可用的深合并：override 覆盖 base（同键为 dict 则递归，否则整值替换）。
    TechSpec 等场景下用于「用户锁定层」覆盖 LLM 生成层。
    """
    return _deep_merge(base, override)


def load_config() -> dict:
    """
    返回合并后的配置字典。
    优先级（由低到高）：example.yml → local.secrets.yml → 环境变量
    """
    cfg = _load_yaml(CONFIG_EXAMPLE)
    cfg = _deep_merge(cfg, _load_yaml(CONFIG_LOCAL))

    # 环境变量覆盖
    for env_key, cfg_key in _ENV_MAP.items():
        val = os.environ.get(env_key)
        if val:
            cfg[cfg_key] = val

    return cfg


# 单例，模块级别缓存
_config: dict | None = None


def get_config() -> dict:
    """返回全局单例配置（懒加载）"""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reset_config() -> None:
    """重置配置单例（用于测试隔离）。下次调用 get() 或 get_config() 时重新加载。"""
    global _config
    _config = None


def get(key: str, default: Any = None) -> Any:
    """快捷取配置值"""
    return get_config().get(key, default)
