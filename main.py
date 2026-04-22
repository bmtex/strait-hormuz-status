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
        system="""You are a geopolitical analyst specializing in the Strait of Hormuz and Iran-US relations.

First, decide if this post is relevant. A post is relevant ONLY if it directly or indirectly concerns:
- Iran, Iranian government, IRGC
- The Strait of Hormuz or Persian Gulf
- Oil tankers, naval blockades, energy sanctions related to Iran
- US-Iran nuclear deal or diplomatic negotiations
- Military posturing toward Iran

If the post is about anything else (domestic politics, other countries, economy, sports, personal comments, other geopolitical issues) mark it as irrelevant.

Respond ONLY with valid JSON, no markdown, no backticks:
{"relevant":true|false,"status":"OPEN"|"CLOSED"|"UNCERTAIN","confidence":0-100,"reasoning":"one sentence"}

If relevant is false, set status to UNCERTAIN and confidence to 0.""",
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
            
                if not result.get("relevant", False):
                    log.info(f"Skipping {pid} — not Hormuz/Iran related")
                    # Still mark as processed so we don't re-check it every run
                    sb.table("classifications").insert({
                        "post_id":    pid,
                        "post_text":  text,
                        "status":     "IRRELEVANT",
                        "confidence": 0,
                        "reasoning":  "Not related to Strait of Hormuz or Iran.",
                        "created_at": datetime.utcnow().isoformat()
                    }).execute()
                    continue
            
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
            .neq("status", "IRRELEVANT") \
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
