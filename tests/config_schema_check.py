"""配置 Schema 漂移检查 — 比较 config.example.yml 和 local.secrets.yml 的键结构"""

import sys
import yaml
from pathlib import Path

sys.path.insert(0, ".")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_PATH = PROJECT_ROOT / "config" / "config.example.yml"
LOCAL_PATH = PROJECT_ROOT / "config" / "local.secrets.yml"

REDACT_KEYS = {"deepseek_api_key", "api_key", "secret", "password", "token"}


def _flatten_keys(d: dict, prefix: str = "") -> dict[str, type]:
    """递归展平所有键路径 → 值类型。"""
    out: dict[str, type] = {}
    for k, v in d.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten_keys(v, path))
        else:
            out[path] = type(v)
    return out


def run() -> bool:
    issues: list[str] = []

    if not EXAMPLE_PATH.exists():
        print(f"  \u2757 example 文件不存在: {EXAMPLE_PATH}")
        return False
    if not LOCAL_PATH.exists():
        print(f"  \u26a0 local.secrets.yml 不存在，跳过 (可接受)")
        return True

    ex = yaml.safe_load(EXAMPLE_PATH.read_text(encoding="utf-8"))
    lo = yaml.safe_load(LOCAL_PATH.read_text(encoding="utf-8"))

    if not isinstance(ex, dict) or not isinstance(lo, dict):
        print("  \u274c YAML 格式错误：期望字典")
        return False

    ex_flat = _flatten_keys(ex)
    lo_flat = _flatten_keys(lo)

    ex_keys = set(ex_flat.keys())
    lo_keys = set(lo_flat.keys())

    extra_local = lo_keys - ex_keys
    missing_local = ex_keys - lo_keys

    for k in sorted(extra_local):
        if any(secret in k.lower() for secret in REDACT_KEYS):
            continue
        issues.append(f"EXTRA local key: {k}")
    for k in sorted(missing_local):
        issues.append(f"MISSING local key: {k}")

    common = ex_keys & lo_keys
    for k in sorted(common):
        if ex_flat[k] != lo_flat[k]:
            issues.append(f"TYPE DRIFT: {k}: example={ex_flat[k].__name__}, local={lo_flat[k].__name__}")

    if issues:
        for i in issues:
            print(f"  \u26a0 {i}")
        print(f"  \u274c {len(issues)} schema drift issue(s) found")
        return False
    else:
        print(f"  \u2705 Config schema OK")
        return True


if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 1)
