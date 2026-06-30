"""
Claude API クライアント
商品情報を受け取り、X 投稿用の日本語紹介文を生成する。
"""

import os
import anthropic


class ClaudeClient:
    MODEL = "claude-haiku-4-5-20251001"  # 高速・低コスト

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=os.environ["CLAUDE_API_KEY"])

    def generate_deal_tweet(self, item: dict) -> str:
        """
        定時投稿用の紹介文を生成する。
        URL は呼び出し側で末尾に追記するため、ここでは生成しない。
        返り値はツイート本文（URL 除き最大 230 文字程度）。
        """
        prompt = f"""あなたはAmazonのお得な商品を紹介するSNSアカウントの担当者です。
以下の商品情報をもとに、Xに投稿する日本語の紹介文を作成してください。

【ルール】
- 絵文字を効果的に使う（先頭に必ず1つ）
- 商品名・割引率・価格を必ず含める
- 購買意欲を高める一言コメントを添える
- URL は含めない（後で自動追加される）
- 改行は最大2回まで
- 合計 200 文字以内

【商品情報】
商品名: {item['title']}
現在価格: ¥{int(item['current_price']):,}
元の価格: ¥{int(item['original_price']):,}
割引率: {item['discount_pct']}%OFF（¥{int(item['discount_amount']):,}引き）
レビュー: ★{item['star_rating']} ({item['review_count']:,}件)

紹介文のみ出力してください（余分な説明不要）。"""

        message = self.client.messages.create(
            model=self.MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()

    def generate_watchlist_tweet(self, item: dict, change_type: str) -> str:
        """
        速報検知用の緊急ツイートを生成する。
        change_type: "price_drop" | "in_stock" | "out_of_stock"
        """
        change_desc = {
            "price_drop": f"価格が¥{int(item['discount_amount']):,}値下がり（{item['discount_pct']}%OFF）",
            "in_stock":   "在庫が復活",
            "out_of_stock": "在庫が少なくなっています",
        }.get(change_type, "変化を検知")

        prompt = f"""Amazonの商品に変化がありました。速報ツイートを作成してください。

【ルール】
- 「速報」「緊急」などの言葉を先頭に入れる
- 絵文字を使って目立たせる
- 変化内容を端的に伝える
- URL は含めない
- 150 文字以内

【情報】
商品名: {item['title']}
変化内容: {change_desc}
現在価格: ¥{int(item['current_price']):,}

速報ツイートのみ出力してください。"""

        message = self.client.messages.create(
            model=self.MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()

    def generate_ranking_tweet(self, item: dict, rank_prev: int, rank_now: int) -> str:
        """
        ランキング急上昇検知用の紹介文を生成する。
        """
        prompt = f"""Amazonのランキングで急上昇している商品を紹介するツイートを作成してください。

【ルール】
- 「急上昇」「話題」などのワードを入れる
- ランキング変動（{rank_prev}位→{rank_now}位）を必ず含める
- 絵文字を効果的に使う
- URL は含めない
- 180 文字以内

【商品情報】
商品名: {item['title']}
現在価格: ¥{int(item['current_price']):,}
前回順位: {rank_prev}位 → 現在: {rank_now}位

ツイートのみ出力してください。"""

        message = self.client.messages.create(
            model=self.MODEL,
            max_tokens=250,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()
