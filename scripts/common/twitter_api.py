"""
X (Twitter) API v2 クライアント
OAuth 1.0a User Context で投稿する。
"""

import os
import tweepy


class TwitterClient:
    def __init__(self):
        self.client = tweepy.Client(
            consumer_key        = os.environ["X_API_KEY"],
            consumer_secret     = os.environ["X_API_SECRET"],
            access_token        = os.environ["X_ACCESS_TOKEN"],
            access_token_secret = os.environ["X_ACCESS_TOKEN_SECRET"],
        )

    def post_tweet(self, text: str) -> str:
        """
        ツイートを投稿し、tweet_id を返す。
        280 文字を超える場合は自動的に切り詰める。
        """
        if len(text) > 280:
            text = text[:277] + "…"

        response = self.client.create_tweet(text=text)
        tweet_id = response.data["id"]
        print(f"[twitter] posted tweet_id={tweet_id}")
        return tweet_id
