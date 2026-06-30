"""
③ ランキング急上昇検知ジョブ (ranking_surge.py)

フロー:
  1. RANKING_CATEGORIES の各ブラウズノードから売れ筋上位10件を取得
  2. ranking_cache.json（前回順位）と比較
  3. 順位が SURGE_THRESHOLD 以上上昇した商品を「話題の商品」として選定
  4. Claude で紹介文生成 → X 投稿 → posted_log に記録
  5. ranking_cache.json を更新

実行: python scripts/ranking_surge.py
"""

import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.common.amazon_api import AmazonPAAPI
from scripts.common.claude_api  import ClaudeClient
from scripts.common.twitter_api import TwitterClient
from scripts.common import log_manager

CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "ranking_cache.json")

# ── 監視カテゴリ（Amazon.co.jp ブラウズノード ID） ─────────────────────────────
# 主要カテゴリの人気ノードを横断して監視する
RANKING_CATEGORIES = [
    {"id": "electronics",   "node_id": "3210981",   "name": "家電・カメラ"},
    {"id": "pc",            "node_id": "2127209051","name": "パソコン・周辺機器"},
    {"id": "toys",          "node_id": "13299531",  "name": "おもちゃ"},
    {"id": "video_games",   "node_id": "637394",    "name": "テレビゲーム"},
    {"id": "sports",        "node_id": "14313411",  "name": "スポーツ・アウトドア"},
    {"id": "beauty",        "node_id": "47391051",  "name": "ビューティー"},
    {"id": "kitchen",       "node_id": "4967926051","name": "キッチン・日用品"},
    {"id": "books",         "node_id": "465392",    "name": "本"},
]

# 「急上昇」とみなす順位の上昇幅（前回より何位上昇したか）
SURGE_THRESHOLD = 5

# 1回の実行で投稿する最大件数
MAX_POSTS_PER_RUN = 1  # 予算節約のため1件/実行に変更

# ── 予算ガード ────────────────────────────────────────────────────────────────
# 全ジョブ合計で1日5件を上限（$0.20 × 5 × 30日 = $30 ≈ 4,860円）
GLOBAL_DAILY_CAP = 5


# ── キャッシュ I/O ────────────────────────────────────────────────────────────

def load_cache() -> dict:
    path = os.path.abspath(CACHE_PATH)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_cache(data: dict) -> None:
    path = os.path.abspath(CACHE_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── ランキング比較ロジック ─────────────────────────────────────────────────────

def find_surges(
    category_id: str,
    current_items: list[dict],
    cache: dict,
) -> list[dict]:
    """
    前回キャッシュと比較して SURGE_THRESHOLD 以上上昇した商品を返す。
    戻り値の各要素に rank_prev / rank_now を付与する。
    """
    prev_ranking: dict = cache.get(category_id, {})
    surges = []

    for rank_now, item in enumerate(current_items, start=1):
        asin = item["asin"]
        rank_prev = prev_ranking.get(asin)

        if rank_prev is None:
            # 初回登場 → ランキング圏外からのランクイン（最大とみなす）
            rank_prev = len(current_items) + 1

        rise = rank_prev - rank_now  # 正なら上昇
        if rise >= SURGE_THRESHOLD:
            item["rank_prev"] = rank_prev
            item["rank_now"]  = rank_now
            item["rank_rise"] = rise
            surges.append(item)

    return surges


def build_new_cache_entry(items: list[dict]) -> dict:
    """ASIN → 現在順位 のマッピングを返す。"""
    return {item["asin"]: rank for rank, item in enumerate(items, start=1)}


# ── メイン ────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=== ranking_surge start ===")

    amazon  = AmazonPAAPI()
    claude  = ClaudeClient()
    twitter = TwitterClient()

    cache     = load_cache()
    new_cache = {}
    all_surges: list[dict] = []

    # 1. 各カテゴリのランキング取得 & 急上昇検知
    for cat in RANKING_CATEGORIES:
        cat_id = cat["id"]
        name   = cat["name"]
        node   = cat["node_id"]

        print(f"\n[ranking] {name} (node={node})")
        try:
            items = amazon.get_browse_node_items(node, item_count=10)
            print(f"[ranking] got {len(items)} items")
        except Exception as e:
            print(f"[ranking] error: {e}")
            continue

        if not items:
            continue

        # 急上昇検知
        surges = find_surges(cat_id, items, cache)
        if surges:
            print(f"[ranking] {len(surges)} surge(s) in {name}")
            for s in surges:
                s["category_name"] = name  # ツイート文に使う
            all_surges.extend(surges)

        # キャッシュを更新（ツイート有無に関わらず）
        new_cache[cat_id] = build_new_cache_entry(items)

    # 2. 急上昇商品をスコア順（上昇幅×スコア）で並べ替え
    all_surges.sort(
        key=lambda x: x["rank_rise"] * (x.get("score", 1) + 1),
        reverse=True,
    )

    # 3. 投稿済み・重複を除外してツイート
    # 予算ガード: 実行前チェック
    today_count = log_manager.count_today_posts()
    print(f"[budget] today's posts so far: {today_count}/{GLOBAL_DAILY_CAP}")
    if today_count >= GLOBAL_DAILY_CAP:
        print("[budget] daily cap reached. skip ranking post.")
        save_cache({**cache, **new_cache})
        return

    posted_count = 0
    for item in all_surges:
        if posted_count >= MAX_POSTS_PER_RUN:
            print(f"[ranking] MAX_POSTS_PER_RUN reached ({MAX_POSTS_PER_RUN}). stop.")
            break

        # 投稿直前にも予算ガード再チェック
        if log_manager.count_today_posts() >= GLOBAL_DAILY_CAP:
            print("[budget] daily cap reached mid-loop. stop.")
            break

        asin = item["asin"]
        if log_manager.is_posted_today(asin):
            print(f"[ranking] already posted: {asin}")
            continue

        print(
            f"[ranking] surge: {item['title'][:40]} "
            f"{item['rank_prev']}位 → {item['rank_now']}位 (+{item['rank_rise']})"
        )

        try:
            tweet_text = claude.generate_ranking_tweet(
                item, item["rank_prev"], item["rank_now"]
            )
        except Exception as e:
            print(f"[ranking] Claude error: {e}")
            tweet_text = (
                f"📈【急上昇】{item['category_name']}ランキング\n"
                f"{item['rank_prev']}位 → {item['rank_now']}位 ↑{item['rank_rise']}\n"
                f"「{item['title'][:50]}」\n"
                f"¥{int(item['current_price']):,}"
            )

        full_tweet = f"{tweet_text}\n{item['url']}"
        if len(full_tweet) > 280:
            full_tweet = full_tweet[:277] + "…"

        print(f"[ranking] tweet:\n{full_tweet}")
        try:
            twitter.post_tweet(full_tweet)
            log_manager.mark_as_posted(asin)
            posted_count += 1
        except Exception as e:
            print(f"[ranking] Twitter error: {e}")

    # 4. キャッシュ保存
    # 取得できたカテゴリだけ上書き（エラーカテゴリは前回値を保持）
    merged = {**cache, **new_cache}
    save_cache(merged)

    print(f"=== ranking_surge done. posted={posted_count} ===")


if __name__ == "__main__":
    main()
