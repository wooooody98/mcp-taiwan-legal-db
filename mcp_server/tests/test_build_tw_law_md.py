"""PoC A: 法規 Markdown 轉換器單元測試"""

import pytest

from scripts.build_tw_law_md import (
    parse_law_text,
    render_markdown,
    Section,
    Article,
    _extract_pcode,
    _format_modified_date,
    _safe_filename,
)


class TestParseLawText:
    def test_basic_章條結構(self):
        text = """中華民國憲法

序文第一段。
序文第二段。

第 一 章 總綱
第 1 條 本國為民主共和國。
第 2 條 主權屬於國民。
第 二 章 人民權利
第 7 條 人民在法律上平等。
"""
        parsed = parse_law_text(text, "中華民國憲法")
        assert "序文第一段" in parsed.preamble
        assert "序文第二段" in parsed.preamble
        sections = [b for b in parsed.blocks if isinstance(b, Section)]
        articles = [b for b in parsed.blocks if isinstance(b, Article)]
        assert len(sections) == 2
        assert sections[0].unit == "章"
        assert sections[0].title == "總綱"
        assert len(articles) == 3
        assert articles[0].number == "1"
        assert articles[2].number == "7"

    def test_條文續行(self):
        text = """民法

第 1 條 第一行內容。
第二行內容。
第三行內容。
第 2 條 另一條。
"""
        parsed = parse_law_text(text, "民法")
        articles = [b for b in parsed.blocks if isinstance(b, Article)]
        assert articles[0].content.count("\n") == 2  # 3 行內容 → 2 個 \n
        assert "第三行內容" in articles[0].content
        assert articles[1].content == "另一條。"

    def test_條號含破折號(self):
        """247-1、1113-2 類條文都要能解析"""
        text = """民法

第 247-1 條 附合契約條款。
第 1113-2 條 意定監護。
"""
        parsed = parse_law_text(text, "民法")
        articles = [b for b in parsed.blocks if isinstance(b, Article)]
        assert [a.number for a in articles] == ["247-1", "1113-2"]

    def test_多層章節(self):
        """編/章/節 都要正確辨識"""
        text = """民法

第 一 編 總則
第 一 章 法例
第 1 條 條文A。
第 二 章 人
第 一 節 自然人
第 6 條 條文B。
"""
        parsed = parse_law_text(text, "民法")
        sections = [b for b in parsed.blocks if isinstance(b, Section)]
        units = [s.unit for s in sections]
        assert units == ["編", "章", "章", "節"]
        # 確認 level mapping：編=2 章=3 節=4
        assert sections[0].level == 2
        assert sections[1].level == 3
        assert sections[3].level == 4


class TestRenderMarkdown:
    def _row(self, **overrides):
        base = {
            "text": "憲法\n\n第 一 章 總綱\n第 1 條 本國為民主共和國。\n",
            "name": "憲法",
            "level": "憲法",
            "category": "憲法",
            "modified_date": "20200101",
            "url": "https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=A0000001",
            "has_eng_version": True,
            "eng_name": "Constitution",
            "histories": "",
        }
        base.update(overrides)
        return base

    def test_frontmatter_含_pcode_與_英文名(self):
        md = render_markdown(self._row())
        assert 'pcode: "A0000001"' in md
        assert 'name: "憲法"' in md
        assert 'name_en: "Constitution"' in md
        assert "modified_date: 2020-01-01" in md

    def test_無英文版不輸出_name_en(self):
        md = render_markdown(self._row(has_eng_version=False, eng_name=""))
        assert "name_en" not in md

    def test_heading_hierarchy(self):
        """# 法規 → ## 編/## 章 → ##### 條（條文固定 h5）"""
        md = render_markdown(self._row())
        assert "\n# 憲法\n" in md
        assert "\n### 第 一 章 總綱\n" in md  # 章用 h3
        assert "\n##### 第 1 條\n" in md       # 條用 h5

    def test_修法沿革附在尾端(self):
        md = render_markdown(self._row(histories="1. 民國 36 年公布\n2. 民國 80 年修正"))
        assert "## 修法沿革" in md
        assert "民國 36 年公布" in md


class TestHelpers:
    def test_extract_pcode(self):
        url = "https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=B0000001"
        assert _extract_pcode(url) == "B0000001"

    def test_extract_pcode_無_query(self):
        assert _extract_pcode("") == ""
        assert _extract_pcode("https://law.moj.gov.tw/foo") == ""

    @pytest.mark.parametrize("raw,expected", [
        ("19470101", "1947-01-01"),
        ("20210120", "2021-01-20"),
        ("", ""),
        ("abc", "abc"),
        ("1947010", "1947010"),  # 長度不足，原樣回傳
    ])
    def test_format_modified_date(self, raw, expected):
        assert _format_modified_date(raw) == expected

    def test_safe_filename(self):
        assert _safe_filename("民法") == "民法"
        assert _safe_filename("勞工/職業安全衛生法") == "勞工_職業安全衛生法"
        assert _safe_filename("問?題<檔>名") == "問_題_檔_名"
