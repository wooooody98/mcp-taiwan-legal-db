"""建立 Taiwan 法規 Markdown 資料集。

架構：
    lianghsun/tw-law (HF, ch_law_full_text config)  ← 上游（每週自動同步 MOJ）
        ↓ load_dataset
    本腳本解析 text 欄位的 編/章/節/條 結構
        ↓
    輸出 output/tw-law-md/<category>/<law_name>.md
        ↓ git push
    lawchat-oss/tw-law-md (GitHub repo)  ← 人類閱讀 / LLM context / git diff 修法

定位：
    * 不重新爬 MOJ（lianghsun 已做），只做格式轉換
    * Markdown heading hierarchy + frontmatter metadata
    * 每部法規一個檔案（而非 JSONL row），讓 git diff 可追蹤修法

使用：
    pip install datasets
    python scripts/build_tw_law_md.py --limit 10        # PoC: 10 部
    python scripts/build_tw_law_md.py                   # 全量
    python scripts/build_tw_law_md.py --law "民法"      # 只跑特定法規
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("build_tw_law_md")

# ─────────────────────────────────────────────────────────
# 結構解析
# ─────────────────────────────────────────────────────────

# 匹配「第 一 編 總則」「第一章 總綱」「第 1 節 ...」等章節標題
# 注意：MOJ 格式章節單位是「編/章/節/款/目」，條文才是「條」
_SECTION_RE = re.compile(
    r"^第\s*([一二三四五六七八九十百千零〇０-９0-9]+)\s*(編|章|節|款|目)\s*(.*)$"
)
# 條文起始行：「第 1 條 內容...」或「第 1-1 條」或「第 247-1 條」
_ARTICLE_START_RE = re.compile(r"^第\s*(\d+(?:[-–—]\d+)?)\s*條(?:\s+(.*))?$")

# 章節單位的 heading level（編 > 章 > 節 > 款 > 目 → MD h2..h6）
_UNIT_LEVEL = {"編": 2, "章": 3, "節": 4, "款": 5, "目": 6}


@dataclass
class Article:
    number: str        # 「1」「247-1」
    content: str


@dataclass
class Section:
    unit: str          # 編/章/節/款/目
    number: str
    title: str
    level: int         # MD heading level


@dataclass
class ParsedLaw:
    preamble: str              # 法規序文（若有）
    blocks: list               # 混合 Section | Article，依原順序


def parse_law_text(text: str, law_name: str) -> ParsedLaw:
    """解析 lianghsun/tw-law 的 text 欄位為結構化章節與條文。

    text 格式範例：
        中華民國憲法

        中華民國國民大會受全體國民之付託，...<序文>

        第 一 章 總綱
        第 1 條 中華民國基於三民主義...
        第 2 條 ...
        第 二 章 人民之權利義務
        第 7 條 ...
    """
    lines = text.splitlines()

    # 首行是法規名稱，丟掉
    if lines and law_name in lines[0]:
        lines = lines[1:]

    preamble_lines: list[str] = []
    blocks: list = []
    current_article: Article | None = None
    seen_first_article = False

    def flush_article():
        nonlocal current_article
        if current_article is not None:
            current_article.content = current_article.content.rstrip()
            blocks.append(current_article)
            current_article = None

    for raw in lines:
        line = raw.rstrip()
        if not line:
            if current_article is not None:
                current_article.content += "\n"
            elif not seen_first_article:
                preamble_lines.append("")
            continue

        # 章節
        sec_match = _SECTION_RE.match(line.strip())
        if sec_match:
            flush_article()
            num, unit, title = sec_match.groups()
            blocks.append(
                Section(
                    unit=unit,
                    number=num,
                    title=title.strip(),
                    level=_UNIT_LEVEL.get(unit, 3),
                )
            )
            seen_first_article = True
            continue

        # 條文起始
        art_match = _ARTICLE_START_RE.match(line.strip())
        if art_match:
            flush_article()
            number, first_content = art_match.groups()
            current_article = Article(number=number, content=(first_content or "").strip())
            seen_first_article = True
            continue

        # 條文續行 or 序文
        if current_article is not None:
            if current_article.content:
                current_article.content += "\n" + line
            else:
                current_article.content = line
        elif not seen_first_article:
            preamble_lines.append(line)

    flush_article()

    # 清理 preamble 前後空行
    preamble = "\n".join(preamble_lines).strip()

    return ParsedLaw(preamble=preamble, blocks=blocks)


# ─────────────────────────────────────────────────────────
# Markdown 輸出
# ─────────────────────────────────────────────────────────

def _format_modified_date(raw: str) -> str:
    """MOJ 的 modified_date 是 YYYYMMDD 字串，轉 ISO。"""
    if not raw or len(raw) != 8 or not raw.isdigit():
        return raw
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"


def _extract_pcode(url: str) -> str:
    """從 law.moj.gov.tw URL 擷取 pcode query 參數。"""
    try:
        return parse_qs(urlparse(url).query).get("pcode", [""])[0]
    except Exception:
        return ""


def _safe_filename(name: str) -> str:
    """法規名稱轉安全檔名（移除 / \\ : 等不合法字元）。"""
    return re.sub(r"[\\/:*?\"<>|]", "_", name).strip()


def render_markdown(row: dict) -> str:
    """把一筆 lianghsun/tw-law 的 row 轉成 Markdown 文件。"""
    parsed = parse_law_text(row["text"], row["name"])

    pcode = _extract_pcode(row.get("url", ""))
    modified = _format_modified_date(row.get("modified_date", ""))

    out: list[str] = []

    # Frontmatter
    out.append("---")
    out.append(f'pcode: "{pcode}"')
    out.append(f'name: "{row["name"]}"')
    if row.get("has_eng_version") and row.get("eng_name"):
        out.append(f'name_en: "{row["eng_name"]}"')
    out.append(f'level: "{row.get("level", "")}"')
    out.append(f'category: "{row.get("category", "")}"')
    if modified:
        out.append(f"modified_date: {modified}")
    out.append(f'source_url: "{row.get("url", "")}"')
    out.append(f'upstream: "lianghsun/tw-law (HuggingFace)"')
    out.append("---")
    out.append("")

    # 標題
    out.append(f"# {row['name']}")
    out.append("")

    # 序文（如有）
    if parsed.preamble:
        for pline in parsed.preamble.splitlines():
            if pline.strip():
                out.append(f"> {pline}")
            else:
                out.append(">")
        out.append("")

    # 章節 + 條文
    for block in parsed.blocks:
        if isinstance(block, Section):
            hashes = "#" * block.level
            heading = f"第 {block.number} {block.unit}"
            if block.title:
                heading += f" {block.title}"
            out.append(f"{hashes} {heading}")
            out.append("")
        else:  # Article
            # 條文固定 h5，保證比最深章節單位（節/款/目，最多 h4）更深一階
            out.append(f"##### 第 {block.number} 條")
            out.append("")
            out.append(block.content)
            out.append("")

    # 修法沿革（附在最後）
    histories = row.get("histories") or ""
    if histories.strip():
        out.append("---")
        out.append("")
        out.append("## 修法沿革")
        out.append("")
        out.append("```")
        out.append(histories.rstrip())
        out.append("```")
        out.append("")

    return "\n".join(out)


# ─────────────────────────────────────────────────────────
# SQLite FTS5 輸出
# ─────────────────────────────────────────────────────────

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS laws (
    pcode TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    name_en TEXT,
    level TEXT,
    category TEXT,
    modified_date TEXT,
    source_url TEXT,
    md_content TEXT NOT NULL
);

-- 中文用 trigram tokenizer（SQLite 3.34+）；無空白分詞情況下能正確匹配「權利能力」等詞
CREATE VIRTUAL TABLE IF NOT EXISTS laws_fts USING fts5(
    name, md_content,
    content='laws',
    content_rowid='rowid',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS laws_ai AFTER INSERT ON laws BEGIN
    INSERT INTO laws_fts(rowid, name, md_content)
    VALUES (new.rowid, new.name, new.md_content);
END;

CREATE TRIGGER IF NOT EXISTS laws_ad AFTER DELETE ON laws BEGIN
    INSERT INTO laws_fts(laws_fts, rowid, name, md_content)
    VALUES ('delete', old.rowid, old.name, old.md_content);
END;

CREATE TRIGGER IF NOT EXISTS laws_au AFTER UPDATE ON laws BEGIN
    INSERT INTO laws_fts(laws_fts, rowid, name, md_content)
    VALUES ('delete', old.rowid, old.name, old.md_content);
    INSERT INTO laws_fts(rowid, name, md_content)
    VALUES (new.rowid, new.name, new.md_content);
END;
"""


class SqliteSink:
    """累積法規到 SQLite FTS5 資料庫（供本地全文檢索、MCP 離線模式）。"""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # 全新產生，避免跟舊資料混在一起（刷新時直接砍掉重建）
        if db_path.exists():
            db_path.unlink()
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(_SQLITE_SCHEMA)

    def add(self, row: dict, md: str) -> None:
        pcode = _extract_pcode(row.get("url", ""))
        if not pcode:
            return
        self.conn.execute(
            """INSERT OR REPLACE INTO laws
               (pcode, name, name_en, level, category, modified_date, source_url, md_content)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pcode,
                row["name"],
                row.get("eng_name") if row.get("has_eng_version") else None,
                row.get("level", ""),
                row.get("category", ""),
                _format_modified_date(row.get("modified_date", "")),
                row.get("url", ""),
                md,
            ),
        )

    def close(self) -> None:
        self.conn.commit()
        self.conn.execute("VACUUM")
        self.conn.close()


# ─────────────────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("output/tw-law-md"))
    parser.add_argument("--sqlite-path", type=Path, default=Path("output/tw-law.db"),
                        help="同時寫 SQLite FTS5（設為空字串可關閉）")
    parser.add_argument("--limit", type=int, default=0, help="只處理前 N 部法規（0 = 全量）")
    parser.add_argument("--law", type=str, default="", help="只處理指定名稱的法規（除錯用）")
    parser.add_argument("--config", type=str, default="ch_law_full_text",
                        choices=["ch_law_full_text", "ch_order_full_text",
                                 "en_law_full_text", "en_order_full_text"])
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        from datasets import load_dataset
    except ImportError:
        raise SystemExit("pip install datasets 先")

    logger.info("載入 lianghsun/tw-law config=%s", args.config)
    ds = load_dataset("lianghsun/tw-law", args.config, split="train")
    logger.info("共 %d 部", len(ds))

    args.output_dir.mkdir(parents=True, exist_ok=True)

    sqlite_sink = SqliteSink(args.sqlite_path) if str(args.sqlite_path) else None

    total = 0
    for row in ds:
        if args.law and row["name"] != args.law:
            continue
        if args.limit and total >= args.limit:
            break

        category = _safe_filename(row.get("category", "其他"))
        name = _safe_filename(row["name"])
        category_dir = args.output_dir / category
        category_dir.mkdir(parents=True, exist_ok=True)

        md = render_markdown(row)
        path = category_dir / f"{name}.md"
        path.write_text(md, encoding="utf-8")
        if sqlite_sink:
            sqlite_sink.add(row, md)
        total += 1
        logger.debug("寫入 %s (%d bytes)", path, len(md.encode()))

        if total % 100 == 0:
            logger.info("已輸出 %d 部法規", total)

    if sqlite_sink:
        sqlite_sink.close()
        logger.info("SQLite FTS5 已寫入 %s", args.sqlite_path)

    logger.info("完成：輸出 %d 部法規到 %s", total, args.output_dir)


if __name__ == "__main__":
    main()
