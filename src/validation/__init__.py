"""验证子包：论文质量评估与规则检查。"""
from .evaluator import evaluate, _check_thesis_rules

__all__ = ["evaluate", "_check_thesis_rules"]
