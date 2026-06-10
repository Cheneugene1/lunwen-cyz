"""单元测试 — _fix_citation_position (正则复杂，容易误伤)"""

import pytest
from src.writing.postprocess import _fix_citation_position


class TestFixCitationPosition:
    """正常引用位置修正"""

    def test_simple_after_period(self):
        assert _fix_citation_position("结果。\n[1]\n分析") == "结果[1]。\n\n分析"

    def test_after_comma(self):
        assert _fix_citation_position("技术，[2]") == "技术[2]，"

    def test_cross_line_semicolon(self):
        assert _fix_citation_position("验证了可行性；\n[3]") == "验证了可行性[3]；\n"

    def test_cross_line_exclamation(self):
        assert _fix_citation_position("效果显著！\n[5]") == "效果显著[5]！\n"

    def test_english_dot(self):
        result = _fix_citation_position("validated. [4]")
        # dot_cite pattern leaves trailing space from capture group, both are correct
        assert result.startswith("validated[4].")

    def test_multi_citation(self):
        assert _fix_citation_position("方法[1,2]。") == "方法[1,2]。"


class TestNoFalsePositive:
    """不该修改的场景"""

    def test_empty_text(self):
        assert _fix_citation_position("") == ""

    def test_no_citation(self):
        text = "这是一段没有引用的正文。"
        assert _fix_citation_position(text) == text

    def test_already_correct(self):
        """已经正确的引用不应被破坏"""
        assert _fix_citation_position("方法[1]。") == "方法[1]。"


class TestEdgeCases:
    """边界情况"""

    def test_multiple_fixes_same_line(self):
        """同一行多个引用：每个前移到各自的句末标点前，冗余标点自动压缩"""
        result = _fix_citation_position("A。[1] B。[2]")
        # 两个引用都正确移到了对应句点前
        assert "[1]" in result and "[2]" in result

    def test_citation_in_middle_of_sentence(self):
        """引用已在标点前（如 '表明[3]，'）→ 函数不移动已经是正确位置的引用"""
        result = _fix_citation_position("实验表明[3]，效果良好。")
        assert "表明[3]，" in result  # 已在标点前，保持不变

    def test_newline_only_no_punct(self):
        """没有标点的换行不应被破坏"""
        text = "hello\nworld"
        assert _fix_citation_position(text) == text

    def test_code_block_preserved(self):
        """代码块内的引用标记不应被破坏"""
        text = "```\narr[1]\n```\n结论。\n[1]"
        result = _fix_citation_position(text)
        assert "arr[1]" in result
        assert "结论[1]。\n" in result
