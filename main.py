import os
import json
import logging
from datetime import datetime
from flask import Flask, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
import tweepy
import anthropic
from supabase import create_client

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

# Clients
twitter = tweepy.Client(bearer_token=os.environ["TWITTER_BEARER_TOKEN"])
claude  = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
sb      = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

TRUMP_USER_ID = "25073877"

def get_processed_ids():
    res = sb.table("classifications").select("post_id").execute()
    return {r["post_id"] for r in res.data}

def classify(text):
    msg = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system="""You are a geopolitical analyst. Read this social media post and assess whether it signals the Strait of Hormuz is OPEN (safe/stable), CLOSED (threatened/blocked), or UNCERTAIN.
Consider: Iran threats, sanctions, military posturing, deal-making, oil/tanker references, maximum pressure language.
Respond ONLY with valid JSON, no markdown:
{"status":"OPEN"|"CLOSED"|"UNCERTAIN","confidence":0-100,"reasoning":"one sentence"}""",
        messages=[{"role": "user", "content": text}]
    )
    return json.loads(msg.content[0].text)

def scrape_and_classify():
    log.info("Running scrape job...")
    try:
        processed = get_processed_ids()
        tweets = twitter.get_users_tweets(
            id=TRUMP_USER_ID,
            max_results=5,
            tweet_fields=["created_at", "text"]
        )
        if not tweets.data:
            log.info("No tweets found.")
            return
        for tweet in tweets.data:
            pid = str(tweet.id)
            if pid in processed:
                continue
            try:
                result = classify(tweet.text)
                sb.table("classifications").insert({
                    "post_id":    pid,
                    "post_text":  tweet.text,
                    "status":     result["status"],
                    "confidence": result["confidence"],
                    "reasoning":  result["reasoning"],
                    "created_at": datetime.utcnow().isoformat()
                }).execute()
                log.info(f"Classified {pid}: {result['status']} ({result['confidence']}%)")
            except Exception as e:
                log.error(f"Failed to classify {pid}: {e}")
    except Exception as e:
        log.error(f"Scrape job failed: {e}")

@app.route("/latest")
def latest():
    res = sb.table("classifications") \
            .select("*") \
            .order("created_at", desc=True) \
            .limit(50) \
            .execute()
    return jsonify(res.data)

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    scrape_and_classify()
    scheduler = BackgroundScheduler()
    scheduler.add_job(scrape_and_classify, "interval", minutes=20)
    scheduler.start()
    app.run(host="0.0.0.0", port=5000)
