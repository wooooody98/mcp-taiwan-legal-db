"""把 build_tw_law_md.py 的產出推到 HuggingFace Dataset。

產出包含：
    - 全量 MD 檔（output/tw-law-md/**/*.md）— 給人類/LLM 閱讀、git diff 用
    - Parquet 表（output/tw-law.parquet）— 給 datasets.load_dataset() 用
    - SQLite FTS5 DB（output/tw-law.db）— 給需要本地全文檢索的應用

使用：
    export HF_TOKEN=hf_xxx
    python scripts/push_to_hf.py --repo-id lawchat-oss/tw-law-md
    python scripts/push_to_hf.py --repo-id wooooody98/tw-law-md --dry-run  # 不實際推
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger("push_to_hf")


def build_parquet(db_path: Path, output_path: Path) -> None:
    """從 SQLite 產生一份 parquet，欄位對齊 HF 慣例。"""
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as e:
        raise SystemExit(f"pip install pyarrow 先: {e}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT pcode, name, name_en, level, category,
               modified_date, source_url, md_content
        FROM laws
        ORDER BY pcode
    """).fetchall()
    conn.close()

    table = pa.Table.from_pylist([dict(r) for r in rows])
    pq.write_table(table, output_path, compression="snappy")
    logger.info("寫入 parquet: %s (%d 筆)", output_path, len(rows))


def write_dataset_card(output_dir: Path, total_laws: int) -> Path:
    """產生 HF dataset README。"""
    card = f"""---
license: other
license_name: open-government-data-license-1.0
language:
- zh
- en
size_categories:
- 1K<n<10K
task_categories:
- text-retrieval
- text-generation
tags:
- legal
- taiwan
- law
- markdown
---

# Taiwan Law Markdown Dataset

結構化 Markdown 版台灣現行法規（{total_laws} 部），**每部法規一個 `.md` 檔**，
保留原始「編 / 章 / 節 / 款 / 目 / 條」章節層級。

## 資料來源與定位

```
法務部全國法規資料庫 (law.moj.gov.tw)
    ↓ 每週同步
lianghsun/tw-law (HF dataset)
    ↓ scripts/build_tw_law_md.py
本資料集（純格式轉換）
```

**本資料集不重複爬 MOJ**，直接從 [`lianghsun/tw-law`](https://huggingface.co/datasets/lianghsun/tw-law) 讀取結構化 JSONL，
轉換為 Markdown heading hierarchy + SQLite FTS5，方便：

- 人類閱讀與 AI context 載入
- 透過 Git diff 追蹤修法變化
- 離線全文檢索（SQLite FTS5 trigram，查詢 < 1ms）

## 檔案結構

```
├── tw-law-md/                    # 1343 部法規，每部一個 .md
│   ├── 憲法/
│   │   └── 中華民國憲法.md
│   ├── 行政＞法務部＞法律事務目/
│   │   └── 民法.md
│   └── ...
├── tw-law.parquet                # 同樣內容的 parquet 表
└── tw-law.db                     # SQLite + FTS5，供本地查詢
```

## 使用方式

### 載入 Parquet（HF datasets）

```python
from datasets import load_dataset
ds = load_dataset("lawchat-oss/tw-law-md")
print(ds["train"][0]["md_content"][:500])
```

### 下載 SQLite 做本地查詢

```python
from huggingface_hub import hf_hub_download
db_path = hf_hub_download(
    repo_id="lawchat-oss/tw-law-md",
    filename="tw-law.db",
    repo_type="dataset",
)

import sqlite3
conn = sqlite3.connect(db_path)
for name, in conn.execute(
    "SELECT name FROM laws WHERE rowid IN "
    "(SELECT rowid FROM laws_fts WHERE laws_fts MATCH ?) LIMIT 10",
    ("消滅時效",),
):
    print(name)
```

### 直接讀 MD 檔

```python
from huggingface_hub import snapshot_download
local_dir = snapshot_download(repo_id="lawchat-oss/tw-law-md", repo_type="dataset")
with open(f"{{local_dir}}/tw-law-md/行政＞法務部＞法律事務目/民法.md") as f:
    print(f.read())
```

## Markdown 格式規範

```markdown
---
pcode: "B0000001"
name: "民法"
name_en: "Civil Code"
modified_date: 2021-01-20
source_url: "https://law.moj.gov.tw/..."
---

# 民法

## 第 一 編 總則            (← h2: 編)
### 第 一 章 法例           (← h3: 章)
#### 第 一 節 ...           (← h4: 節)

##### 第 1 條               (← h5: 條，固定層級)
民事，法律所未規定者...
```

條文一律使用 `h5`，確保不論章節單位深淺都低於任何章節 heading。

## 授權

- **原始資料**：法務部全國法規資料庫，[政府資料開放授權條款 1.0](https://data.gov.tw/license)
- **上游工具**：[lianghsun/tw-law](https://huggingface.co/datasets/lianghsun/tw-law)（MIT）
- **本資料集**：同上游授權（Open Government Data License 1.0）

## 產出工具

本資料集由 [mcp-taiwan-legal-db/scripts/build_tw_law_md.py](https://github.com/lawchat-oss/mcp-taiwan-legal-db) 產出。
每週自動同步，commit diff 對應上游異動。
"""
    readme_path = output_dir / "README.md"
    readme_path.write_text(card, encoding="utf-8")
    logger.info("寫入 dataset card: %s", readme_path)
    return readme_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument("--repo-id", required=True,
                        help="目標 HF dataset repo，如 lawchat-oss/tw-law-md")
    parser.add_argument("--dry-run", action="store_true",
                        help="只生成 parquet + README，不實際推送")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    md_dir = args.output_dir / "tw-law-md"
    db_path = args.output_dir / "tw-law.db"
    parquet_path = args.output_dir / "tw-law.parquet"

    if not md_dir.exists() or not db_path.exists():
        raise SystemExit(
            "找不到 output/tw-law-md/ 或 output/tw-law.db，"
            "請先執行 scripts/build_tw_law_md.py"
        )

    # 1. 產生 parquet（從 SQLite 轉，source of truth 一致）
    build_parquet(db_path, parquet_path)

    # 2. 計算法規數
    conn = sqlite3.connect(db_path)
    total = conn.execute("SELECT COUNT(*) FROM laws").fetchone()[0]
    conn.close()

    # 3. 寫 dataset card
    write_dataset_card(args.output_dir, total)

    if args.dry_run:
        logger.info("--dry-run: 已在 %s 備妥，跳過實際推送", args.output_dir)
        return

    # 4. 推到 HF
    try:
        from huggingface_hub import HfApi
    except ImportError:
        raise SystemExit("pip install huggingface_hub 先")

    api = HfApi()
    api.create_repo(repo_id=args.repo_id, repo_type="dataset", exist_ok=True)
    logger.info("準備推送 %s → %s", args.output_dir, args.repo_id)

    api.upload_folder(
        folder_path=str(args.output_dir),
        repo_id=args.repo_id,
        repo_type="dataset",
        commit_message=f"Sync from lianghsun/tw-law ({total} laws)",
        # 忽略內部工作檔，只推需要的
        ignore_patterns=["*.pyc", "__pycache__/", ".DS_Store"],
    )
    logger.info("推送完成：https://huggingface.co/datasets/%s", args.repo_id)


if __name__ == "__main__":
    main()
