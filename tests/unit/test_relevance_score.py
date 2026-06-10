"""单元测试 — _relevance_score (打分逻辑，含同义词 ×0.6 权重)"""

import pytest
from src.retriever import _relevance_score
from src.models import Reference


def _make_ref(title="", abstract="", venue="", authors=None):
    return Reference(
        title=title,
        abstract=abstract,
        venue=venue,
        authors=authors or ["Test Author"],
        source_tag="openalex",
    )


class TestRelevanceScore:
    """基本打分"""

    def test_empty_keywords(self):
        """无关键词时返回 0.5"""
        ref = _make_ref(title="Some Title")
        score = _relevance_score(ref, [])
        assert score == 0.5

    def test_perfect_title_match(self):
        """关键词完整在标题中"""
        ref = _make_ref(title="基于STM32的温湿度监控系统设计")
        score = _relevance_score(ref, ["STM32", "温湿度"])
        assert score > 0.5  # 两个关键词都命中 title

    def test_partial_match(self):
        ref = _make_ref(title="STM32 based design", abstract="temperature and humidity")
        score = _relevance_score(ref, ["STM32", "温湿度"])
        assert score > 0.02  # 至少有一个关键词命中

    def test_no_match_but_has_content(self):
        """完全不匹配但有内容 → 保底 0.02"""
        ref = _make_ref(title="Unrelated Topic")
        score = _relevance_score(ref, ["单片机", "传感器"])
        assert score == 0.02

    def test_empty_ref_no_content(self):
        """完全空的文献 → 0.0"""
        ref = _make_ref(title="", abstract="", venue="")
        score = _relevance_score(ref, ["单片机"])
        assert score == 0.0

    def test_venue_match(self):
        ref = _make_ref(title="X", venue="IEEE Transactions on Sensors")
        score = _relevance_score(ref, ["sensors"])
        # venue 权重 0.5，应 > 0
        assert score > 0.0

    def test_abstract_match(self):
        ref = _make_ref(
            title="X",
            abstract="This paper presents a novel approach to IoT sensor networks.",
        )
        score = _relevance_score(ref, ["IoT"])
        assert score > 0.0


class TestSynonymMap:
    """同义词映射"""

    def test_synonym_saves_zero_score(self):
        """直接匹配 0 分时，同义词匹配可以救回"""
        ref = _make_ref(title="micro-doppler feature extraction")
        # "微多普勒特征" 是中文，英文标题无法直接匹配
        # 但 synonym_map 可以把它映射到 "micro-doppler feature"
        score = _relevance_score(
            ref, ["微多普勒特征"],
            synonym_map={"微多普勒特征": ["micro-doppler feature", "micro doppler"]},
        )
        # 同义词命中 ×0.6 权重
        assert score > 0.02
        assert score < 1.0

    def test_synonym_no_match(self):
        """同义词也不匹配"""
        ref = _make_ref(title="unrelated paper")
        score = _relevance_score(
            ref, ["微多普勒特征"],
            synonym_map={"微多普勒特征": ["micro-doppler feature"]},
        )
        assert score == 0.02

    def test_keyword_substring_match(self):
        """关键词是标题的子串（如 keyword='MCU' 在 'MCU-based system' 中）"""
        ref = _make_ref(title="MCU-based monitoring system")
        score = _relevance_score(ref, ["MCU"])
        assert score > 0.1
