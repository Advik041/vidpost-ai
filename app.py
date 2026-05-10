from flask import Flask, request, jsonify
from flask_cors import CORS
import anthropic
import re
import os
import requests

app = Flask(__name__)
CORS(app)
# ADD THESE TWO LINES:
app.config['SERVER_NAME'] = None
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "VidPost AI backend"})


@app.route("/transcript", methods=["POST"])
def get_transcript():
    data = request.get_json()
    video_id = data.get("videoId", "").strip()

    if not video_id:
        return jsonify({"error": "Missing videoId"}), 400
    if not re.match(r'^[A-Za-z0-9_-]{11}$', video_id):
        return jsonify({"error": "Invalid YouTube video ID"}), 400

    title = "YouTube Video"
    try:
        oembed = requests.get(
            f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json",
            timeout=5
        )
        if oembed.ok:
            title = oembed.json().get("title", "YouTube Video")
    except Exception:
        pass

    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        text = ""
        # Try v1.0+ new instance-based API first
        try:
            ytt = YouTubeTranscriptApi()
            fetched = ytt.fetch(video_id)
            text = " ".join([
                (s.text if hasattr(s, 'text') else s.get('text', ''))
                for s in fetched
            ])
        except Exception:
            pass

        # Fallback to v0.x static method
        if not text:
            try:
                lst = YouTubeTranscriptApi.get_transcript(
                    video_id, languages=["en", "en-US", "en-GB", "en-IN"]
                )
                text = " ".join([t.get("text", "") for t in lst])
            except Exception:
                pass

        # Last resort: any language
        if not text:
            transcripts = YouTubeTranscriptApi.list_transcripts(video_id)
            first = next(iter(transcripts))
            lst = first.fetch()
            text = " ".join([
                (s.text if hasattr(s, 'text') else s.get('text', ''))
                for s in lst
            ])

        text = re.sub(r'\[.*?\]', '', text)
        text = re.sub(r'\s+', ' ', text).strip()

        if len(text) < 50:
            return jsonify({"error": "Transcript too short. Try another video."}), 422

        return jsonify({
            "transcript": text[:10000],
            "wordCount": len(text.split()),
            "title": title,
            "videoId": video_id
        })

    except Exception as e:
        err = str(e).lower()
        if "disabled" in err or "no transcript" in err:
            return jsonify({"error": "No captions on this video. Paste transcript manually."}), 422
        if "unavailable" in err or "private" in err:
            return jsonify({"error": "Video unavailable or private."}), 422
        return jsonify({"error": f"Transcript error: {str(e)}"}), 500


@app.route("/generate", methods=["POST"])
def generate_posts():
    data       = request.get_json()
    transcript = data.get("transcript", "").strip()
    title      = data.get("title", "YouTube Video")
    tone       = data.get("tone", "professional")
    platforms  = data.get("platforms", ["linkedin", "twitter"])
    api_key    = data.get("apiKey", "").strip()

    if not transcript:
        return jsonify({"error": "Missing transcript"}), 400
    if not api_key or not api_key.startswith("sk-ant"):
        return jsonify({"error": "Invalid or missing Claude API key"}), 400

    tone_guide = {
        "professional": "professional, credible, and insightful",
        "casual":       "conversational and approachable, like a smart friend",
        "bold":         "punchy, bold, and direct — short sentences, strong opinions",
        "storytelling": "narrative-driven — open with an anecdote, build to the insight"
    }
    tone_desc = tone_guide.get(tone, tone_guide["professional"])

    prompts = {
        "linkedin": f"""Turn this YouTube transcript into a LinkedIn post.
TITLE: "{title}" | TONE: {tone_desc}
TRANSCRIPT: {transcript}
RULES: Strong hook first line. 3 insights. CTA at end. 150-280 words. Max 3 hashtags at end. Output ONLY the post.""",

        "twitter": f"""Turn this transcript into a 6-tweet Twitter thread.
TITLE: "{title}" | TONE: {tone_desc}
TRANSCRIPT: {transcript}
RULES: Each tweet under 280 chars. Number them 1/ 2/ etc. No hashtags. Output ONLY the 6 tweets, one per line.""",

        "instagram": f"""Turn this transcript into an Instagram caption.
TITLE: "{title}" | TONE: {tone_desc}
TRANSCRIPT: {transcript}
RULES: Hook first (under 125 chars). 3-4 short paragraphs. End with question. 10 hashtags at end. Output ONLY the caption.""",

        "tiktok": f"""Turn this transcript into a TikTok script (~130 words, spoken in 45-60 seconds).
TITLE: "{title}" | TONE: {tone_desc}
TRANSCRIPT: {transcript}
RULES: Ultra-strong hook first 3 seconds. 3 insights. CTA at end. Written to be spoken. Output ONLY the script.""",

        "youtube_short": f"""Turn this transcript into a YouTube Shorts script (~140 words).
TITLE: "{title}" | TONE: {tone_desc}
TRANSCRIPT: {transcript}
RULES: Label [INTRO][POINT 1][POINT 2][POINT 3][CTA]. End with 'Watch the full video'. Output ONLY the script."""
    }

    client  = anthropic.Anthropic(api_key=api_key)
    results = {}
    errors  = {}

    for platform in platforms:
        if platform not in prompts:
            continue
        try:
            message = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompts[platform]}]
            )
            results[platform] = message.content[0].text.strip()
        except anthropic.AuthenticationError:
            return jsonify({"error": "Invalid Claude API key"}), 401
        except anthropic.RateLimitError:
            return jsonify({"error": "Rate limit hit. Wait a moment."}), 429
        except Exception as e:
            errors[platform] = str(e)

    if not results:
        return jsonify({"error": "Generation failed.", "details": errors}), 500

    return jsonify({"results": results, "errors": errors, "title": title})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
