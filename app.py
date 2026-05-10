from flask import Flask, request, jsonify
from flask_cors import CORS
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

    # Method 1: Supadata API
    supadata_key = os.environ.get("SUPADATA_API_KEY", "")
    if supadata_key:
        try:
            resp = requests.get(
                "https://api.supadata.ai/v1/youtube/transcript",
                params={"videoId": video_id, "text": "true"},
                headers={"x-api-key": supadata_key},
                timeout=15
            )
            if resp.ok:
                d = resp.json()
                content = d.get("content", "")
                if isinstance(content, list):
                    text = " ".join([s.get("text","") for s in content])
                elif isinstance(content, str):
                    text = content
                print(f"Supadata success: {len(text) if text else 0} chars")
        except Exception as e:
            print(f"Supadata failed: {e}")

    # Method 2: youtubetranscript.com
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
                    for ent, ch in [("&amp;","&"),("&quot;",'"'),("&#39;","'"),("&lt;","<"),("&gt;",">")]:
                        raw = raw.replace(ent, ch)
                    text = raw.strip()
                    print(f"youtubetranscript.com success: {len(text)} chars")
        except Exception as e:
            print(f"youtubetranscript.com failed: {e}")

    if not text or len(text) < 30:
        return jsonify({"error": "Could not fetch transcript automatically. Please paste it manually."}), 422

    text = re.sub(r'\[.*?\]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()

    return jsonify({
        "transcript": text[:10000],
        "wordCount": len(text.split()),
        "title": title,
        "videoId": video_id
    })


# ── Generate with Groq (free) ─────────────────────────────────────────────────
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

    # Use Groq key from env, fallback to user-provided key
    groq_key = os.environ.get("GROQ_API_KEY", "")
    use_key   = groq_key if groq_key else api_key

    if not use_key:
        return jsonify({"error": "No API key available"}), 400

    tone_map = {
        "professional": "professional and insightful",
        "casual":       "conversational and approachable",
        "bold":         "punchy and direct, short sentences",
        "storytelling": "narrative-driven with a story arc"
    }
    tone_desc = tone_map.get(tone, "professional and insightful")

    prompts = {
        "linkedin":      f'Turn this YouTube transcript into a LinkedIn post.\nTitle: "{title}"\nTone: {tone_desc}\nTranscript: {transcript[:3000]}\n\nRules: Strong hook first line. 3 insights. CTA at end. 150-250 words. Max 3 hashtags at end. Output ONLY the post text, nothing else.',
        "twitter":       f'Turn this transcript into a 6-tweet Twitter thread.\nTitle: "{title}"\nTone: {tone_desc}\nTranscript: {transcript[:3000]}\n\nRules: Each tweet under 280 chars. Number them 1/ 2/ etc. No hashtags. Output ONLY the 6 tweets, one per line, nothing else.',
        "instagram":     f'Turn this transcript into an Instagram caption.\nTitle: "{title}"\nTone: {tone_desc}\nTranscript: {transcript[:3000]}\n\nRules: Hook under 125 chars. 3-4 short paragraphs. End with question. 10 hashtags at end. Output ONLY the caption, nothing else.',
        "tiktok":        f'Write a 45-second TikTok script from this transcript.\nTitle: "{title}"\nTone: {tone_desc}\nTranscript: {transcript[:3000]}\n\nRules: Strong hook in first 3 seconds. 3 points. CTA at end. Written to be spoken. Output ONLY the script, nothing else.',
        "youtube_short": f'Write a YouTube Shorts script from this transcript.\nTitle: "{title}"\nTone: {tone_desc}\nTranscript: {transcript[:3000]}\n\nRules: Label [INTRO][POINT 1][POINT 2][POINT 3][CTA]. Under 60 seconds spoken. Output ONLY the script, nothing else.'
    }

    results = {}
    errors  = {}

    for platform in platforms:
        if platform not in prompts:
            continue
        try:
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {use_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "llama-3.3-70b-versatile",
                    "messages": [
                        {"role": "user", "content": prompts[platform]}
                    ],
                    "max_tokens": 1024,
                    "temperature": 0.7
                },
                timeout=30
            )

            if resp.status_code == 401:
                return jsonify({"error": "Invalid Groq API key. Get a free one at console.groq.com"}), 401
            if resp.status_code == 429:
                return jsonify({"error": "Rate limit hit. Wait a moment and try again."}), 429
            if not resp.ok:
                errors[platform] = f"Groq error {resp.status_code}: {resp.text[:200]}"
                print(f"Groq error for {platform}: {resp.text[:200]}")
                continue

            d = resp.json()
            results[platform] = d["choices"][0]["message"]["content"].strip()
            print(f"Generated {platform} successfully")

        except Exception as e:
            print(f"Generation error for {platform}: {e}")
            errors[platform] = str(e)

    if not results:
        return jsonify({"error": "Generation failed", "details": errors}), 500

    return jsonify({"results": results, "errors": errors, "title": title})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"VidPost AI starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
