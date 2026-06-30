"""
Amazon Product Advertising API v5 クライアント
SigV4 署名を自前実装。SDK 不要で動作する。
"""

import os
import json
import hmac
import hashlib
import datetime
import requests
from typing import Optional


class AmazonPAAPI:
    HOST = "webservices.amazon.co.jp"
    REGION = "us-west-2"
    SERVICE = "ProductAdvertisingAPI"
    MARKETPLACE = "www.amazon.co.jp"

    def __init__(self):
        self.access_key = os.environ["AMAZON_ACCESS_KEY"]
        self.secret_key = os.environ["AMAZON_SECRET_KEY"]
        self.partner_tag = os.environ["AMAZON_PARTNER_TAG"]

    # ── SigV4 署名 ──────────────────────────────────────────────────────────

    def _sign(self, key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    def _get_signing_key(self, date_stamp: str) -> bytes:
        k_date    = self._sign(("AWS4" + self.secret_key).encode("utf-8"), date_stamp)
        k_region  = self._sign(k_date, self.REGION)
        k_service = self._sign(k_region, self.SERVICE)
        return self._sign(k_service, "aws4_request")

    def _make_request(self, operation: str, payload: dict) -> dict:
        endpoint = f"https://{self.HOST}/paapi5/{operation.lower()}"
        t         = datetime.datetime.utcnow()
        amz_date  = t.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = t.strftime("%Y%m%d")

        body         = json.dumps(payload)
        body_hash    = hashlib.sha256(body.encode("utf-8")).hexdigest()
        target       = f"com.amazon.paapi5.v1.ProductAdvertisingAPIv1.{operation}"

        # 正規ヘッダー（キー昇順）
        canonical_headers = (
            "content-encoding:amz-1.0\n"
            "content-type:application/json; charset=utf-8\n"
            f"host:{self.HOST}\n"
            f"x-amz-date:{amz_date}\n"
            f"x-amz-target:{target}\n"
        )
        signed_headers = "content-encoding;content-type;host;x-amz-date;x-amz-target"

        canonical_request = "\n".join([
            "POST",
            f"/paapi5/{operation.lower()}",
            "",
            canonical_headers,
            signed_headers,
            body_hash,
        ])

        credential_scope = f"{date_stamp}/{self.REGION}/{self.SERVICE}/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ])

        signing_key = self._get_signing_key(date_stamp)
        signature   = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        auth = (
            f"AWS4-HMAC-SHA256 "
            f"Credential={self.access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        )

        headers = {
            "content-encoding": "amz-1.0",
            "content-type": "application/json; charset=utf-8",
            "host": self.HOST,
            "x-amz-date": amz_date,
            "x-amz-target": target,
            "Authorization": auth,
        }

        resp = requests.post(endpoint, headers=headers, data=body, timeout=20)
        resp.raise_for_status()
        return resp.json()

    # ── 公開 API ─────────────────────────────────────────────────────────────

    def search_deals(
        self,
        search_index: str = "All",
        min_saving_percent: int = 20,
        item_count: int = 10,
    ) -> list[dict]:
        """
        割引商品を検索し、パース済みリストを返す。
        返り値は star_rating >= 3.0 の商品のみ。
        """
        payload = {
            "PartnerTag": self.partner_tag,
            "PartnerType": "Associates",
            "Marketplace": self.MARKETPLACE,
            "Keywords": "セール 割引",
            "SearchIndex": search_index,
            "ItemCount": item_count,
            "MinSavingPercent": min_saving_percent,
            "Resources": [
                "ItemInfo.Title",
                "Offers.Listings.Price",
                "Offers.Listings.SavingBasis",
                "CustomerReviews.Count",
                "CustomerReviews.StarRating",
                "BrowseNodeInfo.WebsiteSalesRank",
            ],
        }
        result = self._make_request("SearchItems", payload)
        raw_items = result.get("SearchResult", {}).get("Items", [])
        parsed = [self._parse_item(i) for i in raw_items]
        return [p for p in parsed if p and p["star_rating"] >= 3.0]

    def get_items(self, asins: list[str]) -> list[dict]:
        """ASIN リストから商品情報を取得する（速報検知・ランキング用）。"""
        payload = {
            "PartnerTag": self.partner_tag,
            "PartnerType": "Associates",
            "Marketplace": self.MARKETPLACE,
            "ItemIds": asins,
            "Resources": [
                "ItemInfo.Title",
                "Offers.Listings.Price",
                "Offers.Listings.SavingBasis",
                "Offers.Listings.Availability.Message",
                "CustomerReviews.Count",
                "CustomerReviews.StarRating",
            ],
        }
        result = self._make_request("GetItems", payload)
        raw_items = result.get("ItemsResult", {}).get("Items", [])
        return [p for p in (self._parse_item(i) for i in raw_items) if p]

    def get_browse_node_items(self, browse_node_id: str, item_count: int = 10) -> list[dict]:
        """ブラウズノード（カテゴリ）の売れ筋商品を取得する（ランキング用）。"""
        payload = {
            "PartnerTag": self.partner_tag,
            "PartnerType": "Associates",
            "Marketplace": self.MARKETPLACE,
            "BrowseNodeId": browse_node_id,
            "SortBy": "Featured",
            "ItemCount": item_count,
            "Resources": [
                "ItemInfo.Title",
                "Offers.Listings.Price",
                "Offers.Listings.SavingBasis",
                "CustomerReviews.Count",
                "CustomerReviews.StarRating",
                "BrowseNodeInfo.WebsiteSalesRank",
            ],
        }
        result = self._make_request("SearchItems", payload)
        raw_items = result.get("SearchResult", {}).get("Items", [])
        return [p for p in (self._parse_item(i) for i in raw_items) if p]

    # ── パーサー ─────────────────────────────────────────────────────────────

    def _parse_item(self, item: dict) -> Optional[dict]:
        try:
            asin  = item.get("ASIN", "")
            title = item.get("ItemInfo", {}).get("Title", {}).get("DisplayValue", "")
            if not asin or not title:
                return None

            listings = item.get("Offers", {}).get("Listings", [])
            listing  = listings[0] if listings else {}

            price_info     = listing.get("Price", {})
            current_price  = float(price_info.get("Amount", 0))
            currency       = price_info.get("Currency", "JPY")

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

            reviews     = item.get("CustomerReviews", {})
            review_count = int(reviews.get("Count", 0) or 0)
            star_rating  = float(
                reviews.get("StarRating", {}).get("Value", 0) or 0
            )

            url = f"https://www.amazon.co.jp/dp/{asin}?tag={self.partner_tag}"

            # スコア: 割引額 × レビュー数（定時投稿のランキング用）
            score = discount_amount * review_count

            return {
                "asin": asin,
                "title": title,
                "current_price": current_price,
                "original_price": original_price,
                "discount_amount": discount_amount,
                "discount_pct": discount_pct,
                "currency": currency,
                "review_count": review_count,
                "star_rating": star_rating,
                "availability": availability,
                "url": url,
                "score": score,
            }
        except Exception as e:
            print(f"[amazon_api] parse error for item: {e}")
            return None
