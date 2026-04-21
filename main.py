import os
import re
import json
import logging
from datetime import datetime
from flask import Flask, jsonify
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
import feedparser
import anthropic
from supabase import create_client

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
sb     = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

FEED_URL = "https://trumpstruth.org/feed"

def get_processed_ids():
    res = sb.table("classifications").select("post_id").execute()
    return {r["post_id"] for r in res.data}

def classify(text):
    msg = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system="""You are a geopolitical analyst. Read this social media post and assess whether it signals the Strait of Hormuz is OPEN (safe/stable), CLOSED (threatened/blocked), or UNCERTAIN.
Consider: Iran threats, sanctions, military posturing, deal-making, oil/tanker references, maximum pressure language, naval deployments, energy/oil price comments.
Respond ONLY with valid JSON, no markdown, no backticks:
{"status":"OPEN"|"CLOSED"|"UNCERTAIN","confidence":0-100,"reasoning":"one sentence"}""",
        messages=[{"role": "user", "content": text}]
    )
    raw = msg.content[0].text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)

def scrape_and_classify():
    log.info("Running scrape job...")
    try:
        processed = get_processed_ids()
        feed = feedparser.parse(
            FEED_URL,
            agent="Mozilla/5.0 (compatible; HormuzWatch/1.0)"
        )

        if not feed.entries:
            log.info("No entries found in RSS feed.")
            return

        for entry in feed.entries[:5]:
            pid = entry.id
            if pid in processed:
                log.info(f"Skipping {pid} — already processed")
                continue

            text = (entry.get("summary") or entry.get("title") or "").strip()
            text = re.sub(r'<[^>]+>', '', text).strip()

            if not text or len(text) < 10:
                log.info(f"Skipping {pid} — empty or media-only post")
                continue

            try:
                result = classify(text)
                sb.table("classifications").insert({
                    "post_id":    pid,
                    "post_text":  text,
                    "status":     result["status"],
                    "confidence": result["confidence"],
                    "reasoning":  result["reasoning"],
                    "created_at": datetime.utcnow().isoformat()
                }).execute()
                log.info(f"Classified {pid}: {result['status']} ({result['confidence']}%)")
            except Exception as e:
                log.error(f"Failed to classify {pid}: {e}")
                log.error(f"Post text was: {text!r}")

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
