"""
Amazon Product Advertising API v5 クライアント
Creators API (OAuth2 client_credentials) で認証する。

環境変数:
  AMAZON_ACCESS_KEY  : OAuth2 Client ID  (amzn1.application-oa2-client.xxx)
  AMAZON_SECRET_KEY  : OAuth2 Client Secret
  AMAZON_PARTNER_TAG : アソシエイトタグ (例: kamenmankun-22)
"""

import os
import json
import time
import requests
from typing import Optional


class AmazonPAAPI:
    HOST        = "webservices.amazon.co.jp"
    MARKETPLACE = "www.amazon.co.jp"
    TOKEN_URL   = "https://api.amazon.com/auth/o2/token"
    BASE_URL    = "https://webservices.amazon.co.jp/paapi5"

    def __init__(self):
        self.client_id     = os.environ["AMAZON_ACCESS_KEY"]
        self.client_secret = os.environ["AMAZON_SECRET_KEY"]
        self.partner_tag   = os.environ["AMAZON_PARTNER_TAG"]
        self._token: Optional[str] = None
        self._token_expires: float = 0

    # ── OAuth2 トークン取得 ───────────────────────────────────────────────────

    def _get_token(self) -> str:
        """アクセストークンを取得・キャッシュする（期限切れなら再取得）。"""
        if self._token and time.time() < self._token_expires - 60:
            return self._token

        resp = requests.post(
            self.TOKEN_URL,
            data={
                "grant_type":    "client_credentials",
                "client_id":     self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=15,
        )
        if not resp.ok:
            print(f"[amazon_api] token error {resp.status_code}: {resp.text[:300]}")
        resp.raise_for_status()
        data = resp.json()
        print(f"[amazon_api] token obtained. expires_in={data.get('expires_in')}")
        self._token         = data["access_token"]
        self._token_expires = time.time() + data.get("expires_in", 3600)
        return self._token

    # ── PA-API リクエスト ─────────────────────────────────────────────────────

    def _make_request(self, operation: str, payload: dict) -> dict:
        token = self._get_token()
        url   = f"{self.BASE_URL}/{operation.lower()}"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "x-amz-target": (
                f"com.amazon.paapi5.v1.ProductAdvertisingAPIv1.{operation}"
            ),
        }

        resp = requests.post(url, headers=headers, json=payload, timeout=20)
        if not resp.ok:
            print(f"[amazon_api] API error {resp.status_code}: {resp.text[:300]}")
        resp.raise_for_status()
        return resp.json()

    # ── 公開 API ──────────────────────────────────────────────────────────────

    def search_deals(
        self,
        search_index: str = "All",
        min_saving_percent: int = 20,
        item_count: int = 10,
    ) -> list[dict]:
        """割引商品を検索し、星3以上の商品リストを返す。"""
        payload = {
            "PartnerTag":        self.partner_tag,
            "PartnerType":       "Associates",
            "Marketplace":       self.MARKETPLACE,
            "Keywords":          "セール 割引",
            "SearchIndex":       search_index,
            "ItemCount":         item_count,
            "MinSavingPercent":  min_saving_percent,
            "Resources": [
                "ItemInfo.Title",
                "Offers.Listings.Price",
                "Offers.Listings.SavingBasis",
                "CustomerReviews.Count",
                "CustomerReviews.StarRating",
                "BrowseNodeInfo.WebsiteSalesRank",
            ],
        }
        result    = self._make_request("SearchItems", payload)
        raw_items = result.get("SearchResult", {}).get("Items", [])
        parsed    = [self._parse_item(i) for i in raw_items]
        return [p for p in parsed if p and p["star_rating"] >= 3.0]

    def get_items(self, asins: list[str]) -> list[dict]:
        """ASIN リストから商品情報を取得する（速報検知・ランキング用）。"""
        payload = {
            "PartnerTag":  self.partner_tag,
            "PartnerType": "Associates",
            "Marketplace": self.MARKETPLACE,
            "ItemIds":     asins,
            "Resources": [
                "ItemInfo.Title",
                "Offers.Listings.Price",
                "Offers.Listings.SavingBasis",
                "Offers.Listings.Availability.Message",
                "CustomerReviews.Count",
                "CustomerReviews.StarRating",
            ],
        }
        result    = self._make_request("GetItems", payload)
        raw_items = result.get("ItemsResult", {}).get("Items", [])
        return [p for p in (self._parse_item(i) for i in raw_items) if p]

    def get_browse_node_items(
        self, browse_node_id: str, item_count: int = 10
    ) -> list[dict]:
        """ブラウズノードの売れ筋商品を取得する（ランキング用）。"""
        payload = {
            "PartnerTag":   self.partner_tag,
            "PartnerType":  "Associates",
            "Marketplace":  self.MARKETPLACE,
            "BrowseNodeId": browse_node_id,
            "SortBy":       "Featured",
            "ItemCount":    item_count,
            "Resources": [
                "ItemInfo.Title",
                "Offers.Listings.Price",
                "Offers.Listings.SavingBasis",
                "CustomerReviews.Count",
                "CustomerReviews.StarRating",
                "BrowseNodeInfo.WebsiteSalesRank",
            ],
        }
        result    = self._make_request("SearchItems", payload)
        raw_items = result.get("SearchResult", {}).get("Items", [])
        return [p for p in (self._parse_item(i) for i in raw_items) if p]

    # ── パーサー ──────────────────────────────────────────────────────────────

    def _parse_item(self, item: dict) -> Optional[dict]:
        try:
            asin  = item.get("ASIN", "")
            title = item.get("ItemInfo", {}).get("Title", {}).get("DisplayValue", "")
            if not asin or not title:
                return None

            listings      = item.get("Offers", {}).get("Listings", [])
            listing       = listings[0] if listings else {}
            price_info    = listing.get("Price", {})
            current_price = float(price_info.get("Amount", 0))
            currency      = price_info.get("Currency", "JPY")

            saving_basis    = listing.get("SavingBasis", {})
            original_price  = float(saving_basis.get("Amount", 0))
            discount_amount = max(original_price - current_price, 0)
            discount_pct    = (
                round(discount_amount / original_price * 100)
                if original_price > 0 else 0
            )

            availability = (
                listing.get("Availability", {}).get("Message", "") or
                listing.get("Availability", {}).get("Type", "")
            )

            reviews      = item.get("CustomerReviews", {})
            review_count = int(reviews.get("Count", 0) or 0)
            star_rating  = float(
                reviews.get("StarRating", {}).get("Value", 0) or 0
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
            print(f"[amazon_api] parse error: {e}")
            return None
