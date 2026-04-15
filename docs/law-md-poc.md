# PoC A：法規 Markdown 資料集

## 目標

產出結構化 Markdown 版台灣法規，填補 [`victorhsieh/tw-law-corpus`](https://github.com/victorhsieh/tw-law-corpus)（2013 年廢棄）留下的生態位，並作為本 MCP 離線法規查詢的基礎。

## 定位 — 不重做已有的事

```
lianghsun/tw-law (HuggingFace)        ← 上游：每週自動同步 MOJ 全國法規資料庫
  • 8 個 config（ch/en × law/order × articles/full_text）
  • 結構化 JSONL（含 heading_path、中英對照、修法沿革）
    ↓ datasets.load_dataset(...)
scripts/build_tw_law_md.py            ← 本腳本：純格式轉換
  • 解析 text 欄位的「編 / 章 / 節 / 款 / 目 / 條」結構
  • 渲染成 Markdown heading hierarchy
    ↓
兩種輸出：
  output/tw-law-md/<category>/<name>.md
    • 每部法規一個 .md，給人類 / LLM / git diff 用
  output/tw-law.db
    • SQLite + FTS5 (trigram tokenizer for Chinese)
    • 給 MCP 離線查詢用，< 1ms query latency
```

**我們不重爬 MOJ** — lianghsun 已經每週做這件事，再爬一次只是浪費 MOJ 頻寬 + 自找維護負擔。本 PoC 就專注在「把既有結構化資料轉成更好的消費格式」。

## 快速開始

```bash
pip install datasets
python scripts/build_tw_law_md.py              # 全量 1343 部現行法律（~6 秒）
python scripts/build_tw_law_md.py --limit 10   # 只跑 10 部（用來看輸出）
python scripts/build_tw_law_md.py --law 民法   # 只跑指定法規

# 其他 config（命令/英文版）
python scripts/build_tw_law_md.py --config ch_order_full_text
python scripts/build_tw_law_md.py --config en_law_full_text
```

## 產出規模（2026-04 實測）

| 項目 | 數量 |
|---|---|
| 現行法律 (ch_law_full_text) | 1,343 部 |
| 全量 MD 輸出大小 | ~22 MB |
| SQLite FTS5 DB 大小 | ~58 MB |
| 轉換時間（全量） | ~6 秒 |
| FTS5 查詢延遲 | < 1 ms |

比較：同樣關鍵字查詢透過 `query_regulation` live API → 200–800 ms。

## Markdown 格式

```markdown
---
pcode: "B0000001"
name: "民法"
name_en: "Civil Code"
level: "法律"
category: "行政＞法務部＞法律事務目"
modified_date: 2021-01-20
source_url: "https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=B0000001"
upstream: "lianghsun/tw-law (HuggingFace)"
---

# 民法

## 第 一 編 總則

### 第 一 章 法例

##### 第 1 條

民事，法律所未規定者，依習慣；無習慣者，依法理。

...

---

## 修法沿革

```
1.中華民國十八年...
2.中華民國十九年...
...
```
```

### Heading 階層規則

| MD Level | 對應 |
|---|---|
| `#` (h1) | 法規名稱 |
| `##` (h2) | 編 |
| `###` (h3) | 章 |
| `####` (h4) | 節 |
| `#####` (h5) | **條文（固定）** |

條文**固定用 h5**，確保不管某部法規有沒有「編」或「章」層級，條文永遠比任何章節單位深一階，GitHub outline / TOC 產生器不會錯亂。

## SQLite 結構

```sql
CREATE TABLE laws (
    pcode TEXT PRIMARY KEY,
    name, name_en, level, category,
    modified_date, source_url,
    md_content TEXT  -- 完整 MD 文字
);

CREATE VIRTUAL TABLE laws_fts USING fts5(
    name, md_content,
    content='laws',
    tokenize='trigram'  -- ← 中文全文檢索關鍵
);
```

### Trigram tokenizer 的 1 個限制

SQLite FTS5 `trigram` tokenizer 要求查詢詞 **≥ 3 字**才有 token。實務上法律詞彙大多 ≥ 3 字沒問題：

| 查詢 | 結果 |
|---|---|
| `消滅時效`（4 字） | ✅ 找到民法、商標法等 10+ 部 |
| `法定代理人`（5 字） | ✅ |
| `時效`（2 字） | ❌ 不符 trigram 最小長度 |
| `買賣`（2 字） | ❌ |

2 字查詢請擴充成 3 字以上或用 `LIKE '%時效%'` on `md_content`。

## 整合到 MCP

完成後 `query_regulation` 可以加一條 fast path：

```python
async def query_regulation(law_name, article_no=""):
    # 若有本地 SQLite，先查
    if TW_LAW_DB.exists():
        result = await query_sqlite(law_name, article_no)
        if result:
            return result  # ~1ms
    # 否則走現有 live MOJ API (~300ms)
    return await reg_client.get_article(...)
```

## Pipeline

```
.github/workflows/sync-tw-law-md.yml（每週二 11:00 台北時間）
    ↓
  python scripts/build_tw_law_md.py      # MOJ→MD + SQLite
    ↓
  GitHub Actions artifact（14 天保留期，任何人從 Actions UI 下載）
    ↓ 若 HF_TOKEN secret 已設
  python scripts/push_to_hf.py           # 推到 HF dataset
    ↓
  https://huggingface.co/datasets/<repo-id>
```

## 完成狀態

- [x] 全量 1343 部法規 MD + SQLite FTS5 產出
- [x] `examples/tw-law-md/` 放 3 份代表樣本（民法、憲法、勞基法）
- [x] GitHub Actions 每週同步 + 產生 artifact
- [x] HuggingFace dataset push 腳本（含 dataset card）
- [ ] MCP 整合：`query_regulation` 加本地 SQLite fast path
- [ ] 搬到獨立 repo `lawchat-oss/tw-law-md`（待評估）

## 授權

- 原始資料：法務部全國法規資料庫，[政府資料開放授權條款 1.0](https://data.gov.tw/license)
- 上游處理：[lianghsun/tw-law](https://huggingface.co/datasets/lianghsun/tw-law)
- 本腳本：MIT（跟著 mcp-taiwan-legal-db）
