from flask import Flask, request, jsonify
from flask_cors import CORS
import anthropic
import re
import os
import requests

app = Flask(__name__)
CORS(app)

# ── Health ────────────────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "VidPost AI backend"})

# ── Transcript ────────────────────────────────────────────────────────────────
@app.route("/transcript", methods=["POST"])
def get_transcript():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    video_id = data.get("videoId", "").strip()
    if not video_id:
        return jsonify({"error": "Missing videoId"}), 400
    if not re.match(r'^[A-Za-z0-9_-]{11}$', video_id):
        return jsonify({"error": "Invalid video ID"}), 400

    # Get video title
    title = "YouTube Video"
    try:
        r = requests.get(
            f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json",
            timeout=5
        )
        if r.ok:
            title = r.json().get("title", "YouTube Video")
    except:
        pass

    text = None

    # ── Method 1: Supadata API (handles cloud IP blocks) ──────────────────────
    supadata_key = os.environ.get("SUPADATA_API_KEY", "")
    if supadata_key:
        try:
            resp = requests.get(
                f"https://api.supadata.ai/v1/youtube/transcript",
                params={"videoId": video_id, "text": "true"},
                headers={"x-api-key": supadata_key},
                timeout=15
            )
            if resp.ok:
                d = resp.json()
                text = d.get("content") or d.get("transcript") or ""
                print(f"Supadata success: {len(text)} chars")
        except Exception as e:
            print(f"Supadata failed: {e}")

    # ── Method 2: RapidAPI YouTube Transcript ─────────────────────────────────
    if not text:
        try:
            resp = requests.get(
                "https://youtube-transcript3.p.rapidapi.com/api/transcript",
                params={"videoId": video_id, "lang": "en"},
                headers={
                    "X-RapidAPI-Key": os.environ.get("RAPIDAPI_KEY", ""),
                    "X-RapidAPI-Host": "youtube-transcript3.p.rapidapi.com"
                },
                timeout=15
            )
            if resp.ok:
                d = resp.json()
                if isinstance(d, list):
                    text = " ".join([s.get("text", "") for s in d])
                elif isinstance(d, dict):
                    text = d.get("transcript", "") or d.get("text", "")
                print(f"RapidAPI success: {len(text) if text else 0} chars")
        except Exception as e:
            print(f"RapidAPI failed: {e}")

    # ── Method 3: kome.ai free transcript API ────────────────────────────────
    if not text:
        try:
            resp = requests.post(
                "https://kome.ai/api/tools/youtube-transcript",
                json={"video_id": video_id, "format": False},
                headers={"Content-Type": "application/json"},
                timeout=20
            )
            if resp.ok:
                d = resp.json()
                text = d.get("transcript", "") or d.get("text", "")
                print(f"Kome success: {len(text) if text else 0} chars")
        except Exception as e:
            print(f"Kome failed: {e}")

    # ── Method 4: youtubetranscript.com scraper ───────────────────────────────
    if not text:
        try:
            resp = requests.get(
                f"https://www.youtubetranscript.com/?server_vid2={video_id}",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=20
            )
            if resp.ok:
                matches = re.findall(r'<text[^>]*>(.*?)<\/text>', resp.text, re.DOTALL)
                if matches:
                    raw = " ".join(matches)
                    raw = re.sub(r'&amp;', '&', raw)
                    raw = re.sub(r'&quot;', '"', raw)
                    raw = re.sub(r'&#39;', "'", raw)
                    raw = re.sub(r'&lt;', '<', raw)
                    raw = re.sub(r'&gt;', '>', raw)
                    text = raw.strip()
                    print(f"youtubetranscript.com success: {len(text)} chars")
        except Exception as e:
            print(f"youtubetranscript.com failed: {e}")

    if not text or len(text) < 30:
        return jsonify({
            "error": "Could not fetch transcript automatically. Please paste it manually using the box below."
        }), 422

    text = re.sub(r'\[.*?\]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()

    return jsonify({
        "transcript": text[:10000],
        "wordCount": len(text.split()),
        "title": title,
        "videoId": video_id
    })


# ── Generate ──────────────────────────────────────────────────────────────────
@app.route("/generate", methods=["POST"])
def generate_posts():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    transcript = data.get("transcript", "").strip()
    title      = data.get("title", "YouTube Video")
    tone       = data.get("tone", "professional")
    platforms  = data.get("platforms", ["linkedin"])
    api_key    = data.get("apiKey", "").strip()

    if not transcript:
        return jsonify({"error": "Missing transcript"}), 400
    if not api_key or not api_key.startswith("sk-ant"):
        return jsonify({"error": "Invalid Claude API key"}), 400

    tone_map = {
        "professional": "professional and insightful",
        "casual":       "conversational and approachable",
        "bold":         "punchy and direct, short sentences",
        "storytelling": "narrative-driven with a story arc"
    }
    tone_desc = tone_map.get(tone, "professional and insightful")

    prompts = {
        "linkedin":      f'Turn this YouTube transcript into a LinkedIn post.\nTitle: "{title}"\nTone: {tone_desc}\nTranscript: {transcript[:3000]}\n\nRules: Strong hook. 3 insights. CTA. 150-250 words. Max 3 hashtags at end. Output ONLY the post.',
        "twitter":       f'Turn this transcript into a 6-tweet Twitter thread.\nTitle: "{title}"\nTone: {tone_desc}\nTranscript: {transcript[:3000]}\n\nRules: Each under 280 chars. Number 1/ 2/ etc. No hashtags. Output ONLY the tweets, one per line.',
        "instagram":     f'Turn this transcript into an Instagram caption.\nTitle: "{title}"\nTone: {tone_desc}\nTranscript: {transcript[:3000]}\n\nRules: Hook under 125 chars. 3 paragraphs. End with question. 10 hashtags. Output ONLY the caption.',
        "tiktok":        f'Write a 45-second TikTok script.\nTitle: "{title}"\nTone: {tone_desc}\nTranscript: {transcript[:3000]}\n\nRules: Strong hook first 3 sec. 3 points. CTA. Spoken naturally. Output ONLY the script.',
        "youtube_short": f'Write a YouTube Shorts script.\nTitle: "{title}"\nTone: {tone_desc}\nTranscript: {transcript[:3000]}\n\nRules: Label [INTRO][POINT 1][POINT 2][POINT 3][CTA]. Under 60 sec. Output ONLY the script.'
    }

    try:
        client = anthropic.Anthropic(api_key=api_key)
    except Exception as e:
        return jsonify({"error": f"Claude error: {str(e)}"}), 500

    results = {}
    errors  = {}

    for platform in platforms:
        if platform not in prompts:
            continue
        try:
            msg = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompts[platform]}]
            )
            results[platform] = msg.content[0].text.strip()
        except anthropic.AuthenticationError:
            return jsonify({"error": "Invalid Claude API key"}), 401
        except anthropic.RateLimitError:
            return jsonify({"error": "Rate limit hit. Try again in a moment."}), 429
        except Exception as e:
            errors[platform] = str(e)

    if not results:
        return jsonify({"error": "Generation failed", "details": errors}), 500

    return jsonify({"results": results, "errors": errors, "title": title})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"VidPost AI starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
