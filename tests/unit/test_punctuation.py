"""单元测试 — _clean_double_punctuation + _check_missing_punct"""

import pytest
from src.writing.postprocess import _clean_double_punctuation, _check_missing_punct


class TestCleanDoublePunctuation:
    """双重标点清理"""

    def test_double_period(self):
        assert _clean_double_punctuation("内容。。") == "内容。"

    def test_triple_period(self):
        assert _clean_double_punctuation("内容。。。") == "内容。"

    def test_double_comma(self):
        assert _clean_double_punctuation("内容，，继续") == "内容，继续"

    def test_mixed_punct_keep_last(self):
        """异类连续标点保留最后一个（，。→。）"""
        assert _clean_double_punctuation("内容，。") == "内容。"

    def test_period_comma_keep_period(self):
        assert _clean_double_punctuation("内容。，") == "内容。"

    def test_no_change_for_single(self):
        assert _clean_double_punctuation("正常。") == "正常。"

    def test_citation_after_punct(self):
        """引用标记前的标点不应被误删"""
        result = _clean_double_punctuation("方法[1]。")
        assert "方法[1]。" in result

    def test_empty_text(self):
        assert _clean_double_punctuation("") == ""


class TestCheckMissingPunct:
    """缺失标点补全"""

    def test_long_sentence_no_punct(self):
        """>20 中文汉字、非列表、无标点末尾 → 补句号"""
        long_text = "这是一个超过二十个汉字的中文正文内容用于测试补全标点功能是否正常工作"
        result = _check_missing_punct(long_text)
        assert result.endswith("。")

    def test_short_sentence_skip(self):
        """<20 字不补"""
        short = "短句子"
        assert _check_missing_punct(short) == short

    def test_list_item_skip(self):
        """列表项不补"""
        assert _check_missing_punct("- 列表项内容文字很长很长很长很长很长很长") == "- 列表项内容文字很长很长很长很长很长很长"

    def test_already_has_period(self):
        assert _check_missing_punct("已经有句号。") == "已经有句号。"

    def test_already_has_question(self):
        assert _check_missing_punct("有问号吗？") == "有问号吗？"

    def test_already_has_exclamation(self):
        assert _check_missing_punct("有感叹号！") == "有感叹号！"

    def test_ends_with_citation(self):
        """末尾有引用标记 → 不补"""
        assert _check_missing_punct("此处有引用[1]") == "此处有引用[1]"

    def test_english_abstract_skip(self):
        long_en = "A" + "b" * 200
        assert _check_missing_punct(long_en, section_id="abstract_en") == long_en

    def test_math_line_skip(self):
        """含 $ 的行跳过"""
        assert _check_missing_punct("$x^2$ 是一个平方项需要更多中文文字才能满足长度条件所以继续写") == "$x^2$ 是一个平方项需要更多中文文字才能满足长度条件所以继续写"

    def test_table_line_skip(self):
        """含 | 的行跳过"""
        line = "| 列1 | 列2 | 这是一段很长中文要超过二十个字才能触发检测条件继续写下去吧"
        assert _check_missing_punct(line) == line
