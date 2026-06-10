"""单元测试 — ref_store cull_poor_quality / _is_poor (过滤规则，组合爆炸)"""

import pytest
from src.models import Reference
from src.ref_store import ReferenceStore


def _ref(title="Test Title", authors=None, venue="", doi="", year="2024",
         source_tag="openalex", pinned=False):
    return Reference(
        title=title,
        authors=authors if authors is not None else ["Author One"],
        venue=venue,
        doi=doi,
        year=year,
        source_tag=source_tag,
        pinned=pinned,
    )


def _store(*refs):
    s = ReferenceStore()
    for r in refs:
        s.add(r)
    return s


class TestIsPoor:
    """低质量文献过滤"""

    def test_no_authors_filtered(self):
        """无作者 → 过滤"""
        s = _store(
            _ref(title="Good Paper", authors=["Valid Author"]),
            _ref(title="No Author Paper", authors=[]),
        )
        assert len(s) == 2
        removed = s.cull_poor_quality(max_total=40)
        assert removed == 1
        remaining = s.all_refs()
        assert len(remaining) == 1
        assert remaining[0].title == "Good Paper"

    def test_anonymous_author_filtered(self):
        """佚名/空作者名 → 过滤"""
        s = _store(_ref(title="Bad", authors=[""]))
        removed = s.cull_poor_quality(max_total=40)
        assert removed == 1

    def test_numeric_author_filtered(self):
        """纯数字作者 → 过滤"""
        s = _store(_ref(title="Bad", authors=["12345"]))
        removed = s.cull_poor_quality(max_total=40)
        assert removed == 1

    def test_short_author_filtered(self):
        """单字符作者 → 过滤"""
        s = _store(_ref(title="Bad", authors=["X"]))
        removed = s.cull_poor_quality(max_total=40)
        assert removed == 1

    def test_blacklist_venue_filtered(self):
        """黑名单出版商（如 'hans'）→ 过滤"""
        s = _store(_ref(title="Bad", venue="Hans Publishers"))
        removed = s.cull_poor_quality(max_total=40)
        assert removed == 1

    def test_pinned_ref_preserved(self):
        """pinned 文献不被过滤"""
        s = _store(_ref(title="Important", authors=[], pinned=True))
        removed = s.cull_poor_quality(max_total=40)
        assert removed == 0
        assert len(s) == 1


class TestKeywordFiltering:
    """关键词命中数筛选"""

    def test_keyword_hits_sufficient(self):
        """命中 ≥2 个关键词的保留"""
        s = _store(_ref(title="基于STM32和DHT11的温湿度监控"))
        removed = s.cull_poor_quality(
            keywords=["STM32", "DHT11", "温湿度"],
            max_total=40,
        )
        # 三个关键词都命中了标题
        assert removed == 0

    def test_keyword_hits_insufficient_filtered(self):
        """命中不足的过滤（非 pinned）"""
        s = _store(_ref(title="Unrelated Paper"))
        removed = s.cull_poor_quality(
            keywords=["STM32", "ESP32", "传感器"],
            max_total=40,
        )
        # 没有关键词命中 → 移除（非 pinned）
        assert removed == 1

    def test_min_refs_to_keep_rescued(self):
        """len(keywords)<2 时跳过关键词筛选，文献全部保留"""
        s = _store(
            _ref(title="Paper Alpha"),
            _ref(title="Paper Beta"),
        )
        removed = s.cull_poor_quality(
            keywords=["STM32"],  # 只有 1 个关键词 → 跳过关键词检查
            max_total=40,
            min_refs_to_keep=2,
        )
        assert removed == 0
        assert len(s) == 2

    def test_synonym_map_aids_hit_count(self):
        """同义词映射帮助命中计数"""
        s = _store(
            _ref(title="A microcontroller-based monitoring system"),
        )
        removed = s.cull_poor_quality(
            keywords=["单片机", "监控"],
            max_total=40,
            synonym_map={"单片机": ["microcontroller"]},
        )
        # "microcontroller" 匹配 "单片机" 的同义词、"monitoring" 匹配 "监控"
        # 两个关键词都命中 → 保留
        assert removed == 0


class TestMaxTotalCulling:
    """最大数量裁剪"""

    def test_trim_to_limit(self):
        """超过 max_total 时裁剪到上限（需传 keywords 触发裁剪逻辑）"""
        refs = [_ref(title=f"Paper {i}") for i in range(50)]
        s = _store(*refs)
        removed = s.cull_poor_quality(keywords=["Paper", "Test"], max_total=10)
        assert len(s) <= 10

    def test_below_limit_untouched(self):
        """标题 ≥5 字 + 有效作者 → 文献保留（无关键词则跳过筛选）"""
        s = _store(
            _ref(title="Paper Alpha"),
            _ref(title="Paper Beta"),
        )
        removed = s.cull_poor_quality(max_total=40)
        assert removed == 0
        assert len(s) == 2

    def test_pinned_protected_from_culling(self):
        """pinned 文献在裁剪时优先保留"""
        refs = [_ref(title=f"P{i}") for i in range(45)]
        pinned = _ref(title="Important Paper", pinned=True)
        s = _store(pinned, *refs)
        removed = s.cull_poor_quality(max_total=10)
        remaining = s.all_refs()
        assert any(r.pinned for r in remaining)
        assert len(remaining) <= 10
