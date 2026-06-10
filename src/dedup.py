"""
文献去重逻辑
规则（按优先级）：
  1. 若两条记录均有 DOI → 规范化 DOI 相同则合并
  2. 无 DOI 复合键：(title_norm, year_key, first_author_last_name)
合并时：字段更全的一方优先；若有 pinned 侧，优先保留 pinned 的元数据偏好
"""

import re
import unicodedata
from typing import List, Optional

from .models import Reference


# ── 规范化工具 ──────────────────────────────────────────────

def normalize_doi(doi: Optional[str]) -> Optional[str]:
    """统一小写、去除 URL 前缀、去首尾空白"""
    if not doi:
        return None
    doi = doi.strip().lower()
    # 去掉 https://doi.org/ 前缀
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    return doi or None


def normalize_title(title: str) -> str:
    """
    标题规范化：unicode NFKC → 小写 → 压缩空白 → 去常见标点
    这是去重关键函数，需稳定输出。
    单元测试样例：
      "  Deep  Learning:  A Survey " → "deep learning a survey"
      "Graph-Based Methods" → "graphbased methods"
    """
    # unicode 规范化
    t = unicodedata.normalize("NFKC", title)
    # 小写
    t = t.lower()
    # 去除常见标点（保留字母、数字、中文、空格），直接删除而非替换为空格
    # 这样 "Graph-Based" → "GraphBased" → "graphbased"（与规范一致）
    t = re.sub(r"[^\w\s\u4e00-\u9fff]", "", t)
    # 压缩空白
    t = re.sub(r"\s+", " ", t).strip()
    return t


def extract_first_author_last(authors: List[str]) -> str:
    """
    提取第一作者姓氏（粗略：取最后一个空格分割词）
    无法解析时返回 "_no_author"
    """
    if not authors:
        return "_no_author"
    first = authors[0].strip()
    if not first:
        return "_no_author"
    # 处理 "姓, 名" 或 "名 姓" 两种格式
    if "," in first:
        return first.split(",")[0].strip().lower()
    parts = first.split()
    return parts[-1].lower() if parts else "_no_author"


def composite_key(ref: Reference) -> tuple:
    """生成无-DOI 去重复合键"""
    title_norm = normalize_title(ref.title)
    year_key   = str(ref.year).strip() if ref.year else "_no_year"
    fa         = extract_first_author_last(ref.authors)
    return (title_norm, year_key, fa)


# ── 字段充实度评分（用于选保留哪一条） ────────────────────

def _richness(ref: Reference) -> int:
    """字段非空数量，作为信息量评分"""
    score = 0
    for field in ("title", "year", "venue", "doi", "url", "abstract"):
        if getattr(ref, field):
            score += 1
    score += len(ref.authors)
    return score


# ── 合并两条记录 ────────────────────────────────────────────

def _merge_two(a: Reference, b: Reference) -> Reference:
    """
    合并 a 和 b：
    - 若有 pinned 侧，以 pinned 侧为基，用另一侧填补空字段
    - 否则以信息量更多的为基
    """
    base, extra = (a, b) if (a.pinned or _richness(a) >= _richness(b)) else (b, a)
    # 若 extra 是 pinned 而 base 不是，调换
    if extra.pinned and not base.pinned:
        base, extra = extra, base

    merged = base.model_copy()

    # 逐字段：若 base 无值则用 extra 的
    for field in ("year", "venue", "doi", "url", "abstract", "language"):
        if not getattr(merged, field) and getattr(extra, field):
            setattr(merged, field, getattr(extra, field))

    if not merged.authors and extra.authors:
        merged.authors = extra.authors

    # low_confidence：两者之一为 True 则结果为 True
    merged.low_confidence = base.low_confidence or extra.low_confidence

    return merged


# ── 对列表去重 ──────────────────────────────────────────────

def deduplicate(refs: List[Reference]) -> List[Reference]:
    """
    对文献列表进行去重，返回去重后的新列表。
    顺序：pinned 文献在前，其余按加入顺序。
    """
    # Step 1：DOI 去重
    doi_map: dict[str, Reference] = {}
    no_doi: List[Reference] = []

    for ref in refs:
        norm_doi = normalize_doi(ref.doi)
        if norm_doi:
            if norm_doi in doi_map:
                doi_map[norm_doi] = _merge_two(doi_map[norm_doi], ref)
            else:
                doi_map[norm_doi] = ref
        else:
            no_doi.append(ref)

    # Step 2：无 DOI 复合键去重
    key_map: dict[tuple, Reference] = {}
    for ref in no_doi:
        ck = composite_key(ref)
        if ck in key_map:
            key_map[ck] = _merge_two(key_map[ck], ref)
        else:
            key_map[ck] = ref

    # Step 3：合并，pinned 优先排序
    all_refs = list(doi_map.values()) + list(key_map.values())
    all_refs.sort(key=lambda r: (not r.pinned, r.title))
    return all_refs
