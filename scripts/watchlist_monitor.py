"""
② 速報検知ジョブ (watchlist_monitor.py)

フロー:
  1. data/watchlist.json から監視リストを読み込む
  2. watchlist_cache.json（前回の価格・在庫状態）を読み込む
  3. PA-API で現在の商品情報を取得
  4. 変化を検知:
     - キーワード商品: 在庫状態の変化 or 価格変化
     - ASIN 商品(Mac/iPad): price_threshold_percent を超える値下がり のみ発火
  5. 変化あり → Claude で速報文生成 → X 投稿 → posted_log に記録
  6. watchlist_cache.json を更新

実行: python scripts/watchlist_monitor.py
"""

import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.common.amazon_api import AmazonPAAPI
from scripts.common.claude_api  import ClaudeClient
from scripts.common.twitter_api import TwitterClient
from scripts.common import log_manager

WATCHLIST_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "watchlist.json")
CACHE_PATH     = os.path.join(os.path.dirname(__file__), "..", "data", "watchlist_cache.json")

# 価格変化とみなす最小金額（円）— ノイズ除去
MIN_PRICE_CHANGE = 100

# ── 予算ガード ────────────────────────────────────────────────────────────────
# X API pay-per-use: リンク付きツイート $0.20/件
# 月5000円(≈$31)を守るため、全ジョブ合計で1日5件を上限とする。
# ①定時投稿が3件/日固定なので、②速報は残り2枠を上限に使う。
GLOBAL_DAILY_CAP     = 5   # 全ジョブ合計上限
WATCHLIST_DAILY_SLOT = 2   # ② が使える最大枠（GLOBAL_DAILY_CAP - ①の3件）


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


def load_watchlist() -> dict:
    with open(os.path.abspath(WATCHLIST_PATH), encoding="utf-8") as f:
        return json.load(f)


# ── 変化検知ロジック ──────────────────────────────────────────────────────────

def detect_change_keyword(item: dict, prev: dict | None) -> str | None:
    """
    キーワード監視商品の変化タイプを返す。
    変化なし → None
    """
    if prev is None:
        # 初回取得は記録だけしてツイートしない
        return None

    prev_price  = prev.get("current_price", 0)
    prev_avail  = prev.get("availability", "")
    curr_price  = item["current_price"]
    curr_avail  = item["availability"]

    # 在庫復活
    if prev_avail and curr_avail and prev_avail != curr_avail:
        if "在庫なし" in prev_avail or "取り扱い不可" in prev_avail:
            return "in_stock"
        if "在庫なし" in curr_avail or "取り扱い不可" in curr_avail:
            return "out_of_stock"

    # 価格値下がり
    if prev_price > 0 and curr_price > 0:
        diff = prev_price - curr_price
        if diff >= MIN_PRICE_CHANGE:
            return "price_drop"

    return None


def detect_change_asin(item: dict, prev: dict | None, threshold_pct: float) -> str | None:
    """
    ASIN 監視商品（Mac/iPad）: 通常価格より threshold_pct% 以上安い場合のみ発火。
    """
    if prev is None:
        return None

    prev_price = prev.get("current_price", 0)
    curr_price = item["current_price"]

    if prev_price <= 0 or curr_price <= 0:
        return None

    drop_pct = (prev_price - curr_price) / prev_price * 100
    if drop_pct >= threshold_pct:
        return "price_drop"

    return None


# ── メイン ────────────────────────────────────────────────────────────────────

def process_keyword_items(
    amazon: AmazonPAAPI,
    claude: ClaudeClient,
    twitter: TwitterClient,
    keyword_items: list,
    cache: dict,
) -> None:
    for entry in keyword_items:
        item_id  = entry["id"]
        name     = entry["name"]
        keywords = entry["keywords"]
        search_index = entry.get("search_index", "All")

        print(f"\n[watchlist] keyword={name}")

        # PA-API でキーワード検索（割引なし・件数少なめ）
        found_items = []
        for kw in keywords:
            try:
                payload = {
                    "PartnerTag":   amazon.partner_tag,
                    "PartnerType":  "Associates",
                    "Marketplace":  amazon.MARKETPLACE,
                    "Keywords":     kw,
                    "SearchIndex":  search_index,
                    "ItemCount":    3,
                    "Resources": [
                        "ItemInfo.Title",
                        "Offers.Listings.Price",
                        "Offers.Listings.SavingBasis",
                        "Offers.Listings.Availability.Message",
                        "CustomerReviews.Count",
                        "CustomerReviews.StarRating",
                    ],
                }
                result = amazon._make_request("SearchItems", payload)
                raw    = result.get("SearchResult", {}).get("Items", [])
                found_items.extend([amazon._parse_item(i) for i in raw if amazon._parse_item(i)])
            except Exception as e:
                print(f"[watchlist] search error for '{kw}': {e}")

        if not found_items:
            print(f"[watchlist] no items found for {name}")
            continue

        # スコア上位を代表商品として扱う
        found_items.sort(key=lambda x: x["score"], reverse=True)
        item = found_items[0]
        asin = item["asin"]

        prev        = cache.get(item_id)
        change_type = detect_change_keyword(item, prev)

        # キャッシュ更新（常に）
        cache[item_id] = {
            "asin":         asin,
            "current_price": item["current_price"],
            "availability":  item["availability"],
        }

        if change_type is None:
            print(f"[watchlist] no change: {name}")
            continue

        if log_manager.is_posted_today(asin):
            print(f"[watchlist] already posted today: {asin}")
            continue

        # 予算ガード（投稿直前に再チェック）
        if log_manager.count_today_posts() >= GLOBAL_DAILY_CAP:
            print("[budget] daily cap reached during keyword scan. stop.")
            break

        print(f"[watchlist] change detected: {name} → {change_type}")

        try:
            tweet_text = claude.generate_watchlist_tweet(item, change_type)
        except Exception as e:
            print(f"[watchlist] Claude error: {e}")
            tweet_text = f"🚨【速報】{name}に変化あり！\n{change_type} | ¥{int(item['current_price']):,}"

        full_tweet = f"{tweet_text}\n{item['url']}"
        if len(full_tweet) > 280:
            full_tweet = full_tweet[:277] + "…"

        print(f"[watchlist] tweet:\n{full_tweet}")
        try:
            twitter.post_tweet(full_tweet)
            log_manager.mark_as_posted(asin)
        except Exception as e:
            print(f"[watchlist] Twitter error: {e}")


def process_asin_items(
    amazon: AmazonPAAPI,
    claude: ClaudeClient,
    twitter: TwitterClient,
    asin_items: list,
    cache: dict,
) -> None:
    asins = [entry["asin"] for entry in asin_items if entry.get("asin", "").startswith("B")]
    if not asins:
        print("[watchlist] no valid ASINs to check")
        return

    try:
        items = amazon.get_items(asins)
    except Exception as e:
        print(f"[watchlist] get_items error: {e}")
        return

    item_map = {i["asin"]: i for i in items}

    for entry in asin_items:
        asin      = entry.get("asin", "")
        item_id   = entry["id"]
        name      = entry["name"]
        threshold = float(entry.get("price_threshold_percent", 5))

        if asin not in item_map:
            print(f"[watchlist] ASIN not found: {asin} ({name})")
            continue

        item = item_map[asin]
        prev = cache.get(item_id)
        change_type = detect_change_asin(item, prev, threshold)

        # キャッシュ更新
        cache[item_id] = {
            "asin":          asin,
            "current_price": item["current_price"],
            "availability":  item["availability"],
        }

        if change_type is None:
            print(f"[watchlist] no significant change: {name} ¥{int(item['current_price']):,}")
            continue

        if log_manager.is_posted_today(asin):
            print(f"[watchlist] already posted today: {asin}")
            continue

        # 予算ガード（投稿直前に再チェック）
        if log_manager.count_today_posts() >= GLOBAL_DAILY_CAP:
            print("[budget] daily cap reached during ASIN scan. stop.")
            break

        print(f"[watchlist] PRICE DROP detected: {name} → {change_type}")

        try:
            tweet_text = claude.generate_watchlist_tweet(item, change_type)
        except Exception as e:
            print(f"[watchlist] Claude error: {e}")
            tweet_text = (
                f"🚨【速報・値下がり】{name}\n"
                f"¥{int(item['current_price']):,}（{item['discount_pct']}%OFF）"
            )

        full_tweet = f"{tweet_text}\n{item['url']}"
        if len(full_tweet) > 280:
            full_tweet = full_tweet[:277] + "…"

        print(f"[watchlist] tweet:\n{full_tweet}")
        try:
            twitter.post_tweet(full_tweet)
            log_manager.mark_as_posted(asin)
        except Exception as e:
            print(f"[watchlist] Twitter error: {e}")


def main() -> None:
    print("=== watchlist_monitor start ===")

    # ── 予算ガード: 今日すでに上限に達していたら即終了 ──────────────────────
    today_count = log_manager.count_today_posts()
    print(f"[budget] today's posts so far: {today_count}/{GLOBAL_DAILY_CAP}")
    if today_count >= GLOBAL_DAILY_CAP:
        print("[budget] daily cap reached. skip watchlist check.")
        return

    amazon  = AmazonPAAPI()
    claude  = ClaudeClient()
    twitter = TwitterClient()

    watchlist = load_watchlist()
    cache     = load_cache()

    process_keyword_items(
        amazon, claude, twitter,
        watchlist.get("keyword_items", []),
        cache,
    )

    process_asin_items(
        amazon, claude, twitter,
        watchlist.get("asin_items", []),
        cache,
    )

    save_cache(cache)
    print("=== watchlist_monitor done ===")


if __name__ == "__main__":
    main()
