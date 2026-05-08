from flask import Flask, request, jsonify
from flask_cors import CORS
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound, VideoUnavailable
import anthropic
import re
import os
import requests

app = Flask(__name__)
CORS(app)

# ─── Health check ─────────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "VidPost AI backend"})


# ─── Get transcript ───────────────────────────────────────────────────────────
@app.route("/transcript", methods=["POST"])
def get_transcript():
    data = request.get_json()
    video_id = data.get("videoId", "").strip()

    if not video_id:
        return jsonify({"error": "Missing videoId"}), 400

    # Validate video ID format
    if not re.match(r'^[A-Za-z0-9_-]{11}$', video_id):
        return jsonify({"error": "Invalid YouTube video ID"}), 400

    try:
        # Try English first, then any available language
        try:
            transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=["en", "en-US", "en-GB"])
        except NoTranscriptFound:
            # Fall back to any available transcript
            transcripts = YouTubeTranscriptApi.list_transcripts(video_id)
            transcript_obj = next(iter(transcripts))
            transcript_list = transcript_obj.fetch()

        # Join all transcript pieces into clean text
        text = " ".join([t["text"] for t in transcript_list])
        # Clean up common artifacts
        text = re.sub(r'\[.*?\]', '', text)   # remove [Music], [Applause] etc
        text = re.sub(r'\s+', ' ', text).strip()
        word_count = len(text.split())

        # Get video title via oEmbed (no API key needed)
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

        return jsonify({
            "transcript": text[:10000],  # cap at 10k chars for Claude
            "wordCount": word_count,
            "title": title,
            "videoId": video_id
        })

    except TranscriptsDisabled:
        return jsonify({"error": "This video has disabled captions. Try pasting the transcript manually."}), 422
    except VideoUnavailable:
        return jsonify({"error": "Video not found or unavailable. Check the URL and try again."}), 422
    except NoTranscriptFound:
        return jsonify({"error": "No transcript available for this video."}), 422
    except Exception as e:
        return jsonify({"error": f"Failed to fetch transcript: {str(e)}"}), 500


# ─── Generate posts via Claude ────────────────────────────────────────────────
@app.route("/generate", methods=["POST"])
def generate_posts():
    data = request.get_json()
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
        "professional": "professional, credible, and insightful — suitable for senior business professionals",
        "casual":       "conversational, warm, and approachable — like a smart friend sharing a key takeaway",
        "bold":         "punchy, bold, and direct — short sentences, strong opinions, zero fluff",
        "storytelling": "narrative-driven — open with a vivid moment or anecdote, build to the insight"
    }
    tone_desc = tone_guide.get(tone, tone_guide["professional"])

    prompts = {
        "linkedin": f"""You are an expert LinkedIn content writer. Turn this YouTube video transcript into a scroll-stopping LinkedIn post.

VIDEO TITLE: "{title}"
TONE: {tone_desc}

TRANSCRIPT:
{transcript}

STRICT RULES:
- First line must be a powerful hook (makes someone stop scrolling instantly)
- 3 clear, specific insights or takeaways from the video
- End with a question or CTA that invites comments
- 150–280 words maximum
- Short paragraphs, generous line breaks
- NEVER use filler like "In today's fast-paced world" or "Game-changer"
- Max 3 hashtags at the very end only
- Output ONLY the post text, no preamble or explanation""",

        "twitter": f"""You are an expert Twitter/X content writer. Turn this YouTube video transcript into a high-performing thread.

VIDEO TITLE: "{title}"
TONE: {tone_desc}

TRANSCRIPT:
{transcript}

STRICT RULES:
- Exactly 6 tweets
- Each tweet MUST be under 280 characters
- Tweet 1: bold hook that works standalone and makes people want to read on
- Tweets 2-5: one sharp insight each — specific, surprising, or counterintuitive
- Tweet 6: punchy conclusion with a question or CTA
- Number like "1/" "2/" etc.
- NO hashtags
- Output ONLY the 6 tweets separated by newlines, nothing else""",

        "instagram": f"""You are an expert Instagram content writer. Turn this YouTube video transcript into an Instagram caption.

VIDEO TITLE: "{title}"
TONE: {tone_desc}

TRANSCRIPT:
{transcript}

STRICT RULES:
- Hook in first line (visible before 'more' cutoff — under 125 chars)
- 3-4 short punchy paragraphs with key insights
- Emojis used sparingly and purposefully (max 1 per paragraph)
- End with a question to drive comments
- 10-15 relevant hashtags at the very end, separated by spaces
- 150-300 words total
- Output ONLY the caption text, nothing else""",

        "tiktok": f"""You are an expert TikTok scriptwriter. Turn this YouTube video transcript into a punchy TikTok video script.

VIDEO TITLE: "{title}"
TONE: {tone_desc}

TRANSCRIPT:
{transcript}

STRICT RULES:
- Target length: 45-60 seconds when spoken aloud (≈120-150 words)
- First 3 seconds: ultra-strong hook (a question, shocking stat, or bold claim)
- Middle: 3 rapid-fire insights or story beats
- End: clear CTA (follow, comment, share)
- Written exactly as it should be SPOKEN — short sentences, natural rhythm
- Format as: [HOOK], then numbered beats, then [CTA]
- Output ONLY the script, nothing else""",

        "youtube_short": f"""You are an expert YouTube Shorts scriptwriter. Turn this YouTube video transcript into a Shorts script.

VIDEO TITLE: "{title}"
TONE: {tone_desc}

TRANSCRIPT:
{transcript}

STRICT RULES:
- Target: 50-60 seconds spoken aloud (≈130-160 words)
- Open with a question or bold statement that creates instant curiosity
- Deliver 3 rapid insights from the original video
- End: "Watch the full video [link in bio]" CTA
- Conversational, direct address ("you", "your")
- Output ONLY the script with clear [INTRO], [POINT 1], [POINT 2], [POINT 3], [CTA] labels"""
    }

    client = anthropic.Anthropic(api_key=api_key)
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
            return jsonify({"error": "Invalid Claude API key. Check it at console.anthropic.com"}), 401
        except anthropic.RateLimitError:
            return jsonify({"error": "Claude API rate limit hit. Wait a moment and try again."}), 429
        except Exception as e:
            errors[platform] = str(e)

    if not results:
        return jsonify({"error": "All platforms failed to generate.", "details": errors}), 500

    return jsonify({"results": results, "errors": errors, "title": title})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV", "production") == "development"
    app.run(host="0.0.0.0", port=port, debug=debug)
