"""
① 定時投稿ジョブ (hourly_post.py)

フロー:
  1. PA-API で複数カテゴリのセール商品を取得
  2. 当日投稿済み ASIN を除外
  3. 星 3 以上でフィルタ → スコア(割引額×レビュー数)で降順ソート
  4. 1 位商品を選定
  5. Claude API で紹介文を生成
  6. X API でアフィリエイトリンク付きツイートを投稿
  7. posted_log.json に記録

実行: python scripts/hourly_post.py
"""

import sys
import os

# scripts/ をパスに追加して common パッケージを import できるようにする
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.common.amazon_api import AmazonPAAPI
from scripts.common.claude_api  import ClaudeClient
from scripts.common.twitter_api import TwitterClient
from scripts.common import log_manager


# ── 検索対象カテゴリ ──────────────────────────────────────────────────────────
# 複数カテゴリを横断することで候補数を増やす
SEARCH_CONFIGS = [
    {"search_index": "Electronics",     "min_saving_percent": 15},
    {"search_index": "HomeAndKitchen",  "min_saving_percent": 20},
    {"search_index": "Toys",            "min_saving_percent": 20},
    {"search_index": "VideoGames",      "min_saving_percent": 15},
    {"search_index": "Books",           "min_saving_percent": 30},
    {"search_index": "Apparel",         "min_saving_percent": 30},
    {"search_index": "Sports",          "min_saving_percent": 20},
]

MIN_DISCOUNT_AMOUNT = 500   # 割引額が少なすぎる商品は除外（円）
MIN_REVIEW_COUNT    = 10    # レビュー数が少なすぎる商品は除外


def fetch_candidates(amazon: AmazonPAAPI) -> list[dict]:
    """全カテゴリから候補商品を取得してリストにまとめる。"""
    candidates = []
    for cfg in SEARCH_CONFIGS:
        try:
            items = amazon.search_deals(
                search_index       = cfg["search_index"],
                min_saving_percent = cfg["min_saving_percent"],
                item_count         = 10,
            )
            print(f"[fetch] {cfg['search_index']}: {len(items)} items found")
            candidates.extend(items)
        except Exception as e:
            # カテゴリ単位のエラーは無視して続行
            print(f"[fetch] {cfg['search_index']} error: {e}")

    return candidates


def filter_and_rank(candidates: list[dict]) -> list[dict]:
    """
    フィルタリングとスコアリングを行い、降順でソートして返す。
    - 当日投稿済みを除外
    - 割引額・レビュー数の最低ラインを適用
    - score = discount_amount × review_count
    """
    already_posted = set(log_manager.get_today_posted())
    filtered = []

    for item in candidates:
        asin = item["asin"]

        if asin in already_posted:
            print(f"[filter] skip (already posted): {asin}")
            continue
        if item["discount_amount"] < MIN_DISCOUNT_AMOUNT:
            continue
        if item["review_count"] < MIN_REVIEW_COUNT:
            continue

        filtered.append(item)

    # ASIN の重複排除（複数カテゴリで同じ商品が出ることがある）
    seen = set()
    unique = []
    for item in filtered:
        if item["asin"] not in seen:
            seen.add(item["asin"])
            unique.append(item)

    # スコア降順ソート
    unique.sort(key=lambda x: x["score"], reverse=True)
    return unique


def build_tweet(text: str, item: dict) -> str:
    """Claude 生成文 + アフィリエイト URL を結合する。"""
    tweet = f"{text}\n{item['url']}"
    if len(tweet) > 280:
        # URL(約 60 文字)を確保してテキストを切り詰め
        max_text = 280 - len(item["url"]) - 2  # "\n" 含む
        tweet = f"{text[:max_text]}\n{item['url']}"
    return tweet


def main() -> None:
    print("=== hourly_post start ===")

    amazon  = AmazonPAAPI()
    claude  = ClaudeClient()
    twitter = TwitterClient()

    # 1. 商品取得
    candidates = fetch_candidates(amazon)
    print(f"[main] total candidates: {len(candidates)}")

    if not candidates:
        print("[main] no candidates found. exit.")
        sys.exit(0)

    # 2. フィルタ & スコアリング
    ranked = filter_and_rank(candidates)
    print(f"[main] after filter: {len(ranked)} items")

    if not ranked:
        print("[main] all candidates already posted today. exit.")
        sys.exit(0)

    # 3. 1 位商品を選定
    best = ranked[0]
    print(f"[main] selected: ASIN={best['asin']} score={best['score']:.0f} title={best['title'][:40]}")

    # 4. Claude で紹介文生成
    try:
        tweet_text = claude.generate_deal_tweet(best)
        print(f"[main] generated text: {tweet_text[:80]}...")
    except Exception as e:
        print(f"[main] Claude error: {e}. using fallback text.")
        tweet_text = (
            f"🛒 【{best['discount_pct']}%OFF】{best['title'][:60]}\n"
            f"¥{int(best['current_price']):,}（元値¥{int(best['original_price']):,}）\n"
            f"★{best['star_rating']} ({best['review_count']:,}件)"
        )

    # 5. ツイート投稿
    full_tweet = build_tweet(tweet_text, best)
    print(f"[main] tweet ({len(full_tweet)}文字):\n{full_tweet}")

    try:
        tweet_id = twitter.post_tweet(full_tweet)
        print(f"[main] posted successfully: tweet_id={tweet_id}")
    except Exception as e:
        print(f"[main] Twitter post error: {e}")
        sys.exit(1)

    # 6. ログ記録
    log_manager.mark_as_posted(best["asin"])

    # 月初などにログを定期クリーンアップ
    from datetime import datetime
    import pytz
    if datetime.now(pytz.timezone("Asia/Tokyo")).day == 1:
        log_manager.cleanup_old_logs()

    print("=== hourly_post done ===")


if __name__ == "__main__":
    main()
