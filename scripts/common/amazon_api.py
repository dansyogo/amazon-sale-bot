"""
Amazon Creators API クライアント (PA-API v5 の後継)
https://creatorsapi.amazon/catalog/v1/ エンドポイントを使用。
認証: OAuth2 client_credentials (scope=creatorsapi::default)

環境変数:
  AMAZON_ACCESS_KEY  : 認証情報ID  (amzn1.application-oa2-client.xxx)
  AMAZON_SECRET_KEY  : シークレット (amzn1.oa2-cs.v1.xxx)
  AMAZON_PARTNER_TAG : アソシエイトタグ (例: kamenmankun-22)
"""

import os
import json
import time
import requests
from typing import Optional


class AmazonPAAPI:
    MARKETPLACE = "www.amazon.co.jp"
    TOKEN_URL   = "https://api.amazon.com/auth/o2/token"
    BASE_URL    = "https://creatorsapi.amazon/catalog/v1"

    def __init__(self):
        self.client_id     = os.environ["AMAZON_ACCESS_KEY"]
        self.client_secret = os.environ["AMAZON_SECRET_KEY"]
        self.partner_tag   = os.environ["AMAZON_PARTNER_TAG"]
        self._token: Optional[str] = None
        self._token_expires: float = 0

    # ── OAuth2 トークン取得 ───────────────────────────────────────────────────

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token

        resp = requests.post(
            self.TOKEN_URL,
            data={
                "grant_type":    "client_credentials",
                "client_id":     self.client_id,
                "client_secret": self.client_secret,
                "scope":         "creatorsapi::default",
            },
            timeout=15,
        )
        if not resp.ok:
            print(f"[amazon_api] token error {resp.status_code}: {resp.text}")
            resp.raise_for_status()

        data = resp.json()
        self._token         = data["access_token"]
        self._token_expires = time.time() + data.get("expires_in", 3600)
        print(f"[amazon_api] token OK. expires_in={data.get('expires_in')}")
        return self._token

    # ── Creators API リクエスト ───────────────────────────────────────────────

    def _make_request(self, operation: str, payload: dict) -> dict:
        """
        operation: "searchItems" | "getItems"
        """
        url = f"{self.BASE_URL}/{operation}"
        token = self._get_token()

        headers = {
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {token}",
            "x-marketplace": self.MARKETPLACE,
        }

        print(f"[amazon_api] → POST {url}")
        resp = requests.post(url, headers=headers, json=payload, timeout=20)
        if not resp.ok:
            print(f"[amazon_api] error {resp.status_code}: {resp.text}")  # 全文出力
            resp.raise_for_status()

        result = resp.json()
        # レスポンス構造をログに残す（デバッグ用・初回確認）
        top_keys = list(result.keys()) if isinstance(result, dict) else type(result).__name__
        print(f"[amazon_api] response top-keys: {top_keys}")
        return result

    # ── 公開 API ──────────────────────────────────────────────────────────────

    def search_deals(
        self,
        search_index: str = "All",
        min_saving_percent: int = 20,
        item_count: int = 10,
    ) -> list[dict]:
        """割引商品を検索し、星3以上の商品リストを返す。"""
        payload = {
            "partnerTag":        self.partner_tag,
            "partnerType":       "Associates",
            "marketplace":       self.MARKETPLACE,
            "keywords":          "セール 割引",
            "searchIndex":       search_index,
            "itemCount":         item_count,
            "minSavingPercent":  min_saving_percent,
            "resources": [
                "offersV2.listings.price",
                "customerReviews.starRating",
            ],
        }
        result = self._make_request("searchItems", payload)

        # レスポンスのキーを確認しながら取得
        search_result = (
            result.get("searchResult") or
            result.get("SearchResult") or {}
        )
        raw_items = (
            search_result.get("items") or
            search_result.get("Items") or []
        )
        print(f"[amazon_api] searchItems: {len(raw_items)} raw items")
        if raw_items:
            import json as _json
            print(f"[amazon_api] first item sample: {_json.dumps(raw_items[0], ensure_ascii=False)[:600]}")

        parsed = [self._parse_item(i) for i in raw_items]
        return [p for p in parsed if p and p["star_rating"] >= 3.0]

    def get_items(self, asins: list[str]) -> list[dict]:
        """ASIN リストから商品情報を取得する。"""
        payload = {
            "itemIds":     asins,
            "itemIdType":  "ASIN",
            "partnerTag":  self.partner_tag,
            "partnerType": "Associates",
            "marketplace": self.MARKETPLACE,
            "resources": [
                "itemInfo.title",
                "offersV2.listings.price",
                "offersV2.listings.savingBasis",
                "offersV2.listings.availability",
                "customerReviews.count",
                "customerReviews.starRating",
            ],
        }
        result = self._make_request("getItems", payload)

        items_result = (
            result.get("itemsResult") or
            result.get("ItemsResult") or {}
        )
        raw_items = (
            items_result.get("items") or
            items_result.get("Items") or []
        )
        return [p for p in (self._parse_item(i) for i in raw_items) if p]

    def get_browse_node_items(
        self, browse_node_id: str, item_count: int = 10
    ) -> list[dict]:
        """ブラウズノードの売れ筋商品を取得する。"""
        payload = {
            "partnerTag":   self.partner_tag,
            "partnerType":  "Associates",
            "marketplace":  self.MARKETPLACE,
            "browseNodeId": browse_node_id,
            "sortBy":       "Featured",
            "itemCount":    item_count,
            "resources": [
                "offersV2.listings.price",
                "customerReviews.starRating",
            ],
        }
        result = self._make_request("searchItems", payload)

        search_result = (
            result.get("searchResult") or
            result.get("SearchResult") or {}
        )
        raw_items = (
            search_result.get("items") or
            search_result.get("Items") or []
        )
        return [p for p in (self._parse_item(i) for i in raw_items) if p]

    # ── パーサー ──────────────────────────────────────────────────────────────

    def _parse_item(self, item: dict) -> Optional[dict]:
        """Creators API レスポンス（lowerCamelCase）をパースする。"""
        try:
            # ASIN・タイトル
            asin  = item.get("asin") or item.get("ASIN") or ""
            title = (
                item.get("itemInfo", {}).get("title", {}).get("displayValue") or
                item.get("ItemInfo", {}).get("Title", {}).get("DisplayValue") or
                ""
            )
            if not asin or not title:
                print(f"[amazon_api] skip: asin={asin!r} title={title!r}")
                return None

            # 価格情報 (offersV2 形式)
            offers2   = item.get("offersV2", {})
            listings  = offers2.get("listings", []) if offers2 else []
            # 旧形式 fallback
            if not listings:
                offers = item.get("Offers", {}) or item.get("offers", {})
                listings = offers.get("Listings", []) or offers.get("listings", [])

            listing      = listings[0] if listings else {}
            price_info   = listing.get("price", {}) or listing.get("Price", {})
            current_price = float(
                price_info.get("amount") or
                price_info.get("Amount") or 0
            )
            currency = (
                price_info.get("currency") or
                price_info.get("Currency") or "JPY"
            )

            saving_basis   = (
                listing.get("savingBasis") or
                listing.get("SavingBasis") or {}
            )
            original_price = float(
                saving_basis.get("amount") or
                saving_basis.get("Amount") or 0
            )
            discount_amount = max(original_price - current_price, 0)
            discount_pct    = (
                round(discount_amount / original_price * 100)
                if original_price > 0 else 0
            )

            # 在庫
            avail_obj    = (
                listing.get("availability") or
                listing.get("Availability") or {}
            )
            availability = (
                avail_obj.get("message") or
                avail_obj.get("Message") or
                avail_obj.get("type") or
                avail_obj.get("Type") or ""
            )

            # レビュー
            reviews      = item.get("customerReviews") or item.get("CustomerReviews") or {}
            review_count = int(reviews.get("count") or reviews.get("Count") or 0)
            star_raw     = reviews.get("starRating") or reviews.get("StarRating") or {}
            star_rating  = float(
                star_raw.get("value") or
                star_raw.get("Value") or
                (star_raw if isinstance(star_raw, (int, float)) else 0)
            )

            url   = f"https://www.amazon.co.jp/dp/{asin}?tag={self.partner_tag}"
            score = discount_amount * review_count

            return {
                "asin":            asin,
                "title":           title,
                "current_price":   current_price,
                "original_price":  original_price,
                "discount_amount": discount_amount,
                "discount_pct":    discount_pct,
                "currency":        currency,
                "review_count":    review_count,
                "star_rating":     star_rating,
                "availability":    availability,
                "url":             url,
                "score":           score,
            }
        except Exception as e:
            print(f"[amazon_api] parse error: {e} | item keys={list(item.keys())[:10]}")
            return None
