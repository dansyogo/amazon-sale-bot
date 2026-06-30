"""
posted_log.json の読み書きを管理するモジュール。

ファイル形式:
{
  "2025-01-15": ["ASIN1", "ASIN2"],
  "2025-01-16": ["ASIN3"]
}

GitHub Actions ワークフロー側で git commit/push を行うため、
このモジュールはファイル I/O のみ担当する。
"""

import json
import os
from datetime import datetime
import pytz

LOG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "posted_log.json"
)
JST = pytz.timezone("Asia/Tokyo")


def _today_jst() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d")


def _load() -> dict:
    path = os.path.abspath(LOG_PATH)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    path = os.path.abspath(LOG_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_posted_today(asin: str) -> bool:
    """当日すでに投稿済みの ASIN かどうかを確認する。"""
    data  = _load()
    today = _today_jst()
    return asin in data.get(today, [])


def mark_as_posted(asin: str) -> None:
    """ASIN を本日投稿済みとして記録する。"""
    data  = _load()
    today = _today_jst()
    posted = data.setdefault(today, [])
    if asin not in posted:
        posted.append(asin)
    _save(data)
    print(f"[log_manager] recorded ASIN={asin} for {today}")


def get_today_posted() -> list[str]:
    """本日投稿済みの ASIN リストを返す。"""
    data = _load()
    return data.get(_today_jst(), [])


def count_today_posts() -> int:
    """本日の全ジョブ合計投稿件数を返す（予算ガード用）。"""
    return len(get_today_posted())


def cleanup_old_logs(keep_days: int = 30) -> None:
    """30 日以上前のログを削除してファイルサイズを抑制する。"""
    from datetime import timedelta
    data    = _load()
    cutoff  = (datetime.now(JST) - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    cleaned = {k: v for k, v in data.items() if k >= cutoff}
    _save(cleaned)
    print(f"[log_manager] cleanup done. remaining days={len(cleaned)}")
