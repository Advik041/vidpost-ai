from flask import Flask, request, jsonify
from flask_cors import CORS
import anthropic
import re
import os
import requests

app = Flask(__name__)
CORS(app)

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "VidPost AI backend"})

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

    # Get title
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

    # Get transcript
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        text = None

        # Method 1: new v1.0 API
        try:
            api = YouTubeTranscriptApi()
            result = api.fetch(video_id)
            parts = []
            for s in result:
                if hasattr(s, 'text'):
                    parts.append(s.text)
                elif isinstance(s, dict):
                    parts.append(s.get('text', ''))
            text = " ".join(parts).strip()
        except Exception as e1:
            print(f"Method 1 failed: {e1}")

        # Method 2: old static API
        if not text:
            try:
                snippets = YouTubeTranscriptApi.get_transcript(video_id)
                text = " ".join([s.get('text', '') for s in snippets]).strip()
            except Exception as e2:
                print(f"Method 2 failed: {e2}")

        # Method 3: list all and grab first
        if not text:
            try:
                tlist = YouTubeTranscriptApi.list_transcripts(video_id)
                for t in tlist:
                    try:
                        snippets = t.fetch()
                        parts = []
                        for s in snippets:
                            if hasattr(s, 'text'):
                                parts.append(s.text)
                            elif isinstance(s, dict):
                                parts.append(s.get('text', ''))
                        text = " ".join(parts).strip()
                        if text:
                            break
                    except:
                        continue
            except Exception as e3:
                print(f"Method 3 failed: {e3}")

        if not text or len(text) < 30:
            return jsonify({"error": "Could not fetch transcript. Video may have no captions."}), 422

        text = re.sub(r'\[.*?\]', '', text)
        text = re.sub(r'\s+', ' ', text).strip()

        return jsonify({
            "transcript": text[:10000],
            "wordCount": len(text.split()),
            "title": title,
            "videoId": video_id
        })

    except Exception as e:
        print(f"TRANSCRIPT ERROR: {e}")
        return jsonify({"error": str(e)}), 500


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
    if not api_key:
        return jsonify({"error": "Missing API key"}), 400
    if not api_key.startswith("sk-ant"):
        return jsonify({"error": "Invalid Claude API key format"}), 400

    tone_map = {
        "professional": "professional and insightful",
        "casual": "conversational and approachable",
        "bold": "punchy and direct",
        "storytelling": "narrative-driven with a story arc"
    }
    tone_desc = tone_map.get(tone, "professional and insightful")

    prompts = {
        "linkedin": f'Turn this YouTube transcript into a LinkedIn post. Title: "{title}". Tone: {tone_desc}. Transcript: {transcript[:3000]}\n\nRules: Strong hook. 3 insights. CTA. 150-250 words. Max 3 hashtags at end. Output ONLY the post text.',
        "twitter": f'Turn this transcript into a 6-tweet Twitter thread. Title: "{title}". Tone: {tone_desc}. Transcript: {transcript[:3000]}\n\nRules: Each tweet under 280 chars. Number 1/ 2/ etc. No hashtags. Output ONLY tweets, one per line.',
        "instagram": f'Turn this transcript into an Instagram caption. Title: "{title}". Tone: {tone_desc}. Transcript: {transcript[:3000]}\n\nRules: Hook under 125 chars. 3 paragraphs. End with question. 10 hashtags at end. Output ONLY the caption.',
        "tiktok": f'Write a 45-second TikTok script from this transcript. Title: "{title}". Tone: {tone_desc}. Transcript: {transcript[:3000]}\n\nRules: Strong hook in first 3 seconds. 3 points. CTA at end. Spoken naturally. Output ONLY the script.',
        "youtube_short": f'Write a YouTube Shorts script from this transcript. Title: "{title}". Tone: {tone_desc}. Transcript: {transcript[:3000]}\n\nRules: Label [INTRO][POINT 1][POINT 2][POINT 3][CTA]. Under 60 seconds. Output ONLY the script.'
    }

    try:
        client = anthropic.Anthropic(api_key=api_key)
    except Exception as e:
        return jsonify({"error": f"Claude client error: {str(e)}"}), 500

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
            return jsonify({"error": "Invalid Claude API key. Get one at console.anthropic.com"}), 401
        except anthropic.RateLimitError:
            return jsonify({"error": "Rate limit. Wait a moment and try again."}), 429
        except Exception as e:
            print(f"Generation error for {platform}: {e}")
            errors[platform] = str(e)

    if not results:
        return jsonify({"error": "Generation failed", "details": errors}), 500

    return jsonify({"results": results, "errors": errors, "title": title})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
