from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import re, os, uuid, json, subprocess, threading, time, shutil
import requests

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=False)

JOBS = {}
CLIPS_DIR = "/tmp/vidpost_clips"
os.makedirs(CLIPS_DIR, exist_ok=True)

# Find yt-dlp wherever it is
def find_ytdlp():
    for path in ["/usr/bin/yt-dlp", "/usr/local/bin/yt-dlp", "/opt/venv/bin/yt-dlp", "yt-dlp"]:
        if os.path.exists(path):
            return path
    # Try which
    try:
        r = subprocess.run(["which", "yt-dlp"], capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout.strip()
    except:
        pass
    return "yt-dlp"

YTDLP = find_ytdlp()
print(f"yt-dlp path: {YTDLP}")

# ── Health ────────────────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    ytdlp_ok = os.path.exists(YTDLP) or YTDLP == "yt-dlp"
    return jsonify({
        "status": "ok",
        "service": "VidPost AI v2",
        "ytdlp": YTDLP,
        "ffmpeg": bool(shutil.which("ffmpeg"))
    })

# ── Analyse video ─────────────────────────────────────────────────────────────
@app.route("/analyse", methods=["POST", "OPTIONS"])
def analyse():
    if request.method == "OPTIONS":
        return jsonify({}), 200

    data    = request.get_json()
    url     = data.get("url", "").strip()
    api_key = data.get("apiKey", "").strip()
    groq_key = os.environ.get("GROQ_API_KEY", api_key)

    if not url:
        return jsonify({"error": "Missing YouTube URL"}), 400

    m = re.search(r'(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})', url)
    if not m:
        return jsonify({"error": "Invalid YouTube URL"}), 400
    video_id = m.group(1)

    # Get video info via yt-dlp
    try:
        result = subprocess.run([
            YTDLP, "--dump-json", "--no-download",
            "--no-playlist",
            f"https://www.youtube.com/watch?v={video_id}"
        ], capture_output=True, text=True, timeout=40)

        if result.returncode != 0:
            return jsonify({"error": f"Could not fetch video info: {result.stderr[:200]}"}), 422

        info     = json.loads(result.stdout)
        title    = info.get("title", "YouTube Video")
        duration = int(info.get("duration", 0))
    except FileNotFoundError:
        return jsonify({"error": "yt-dlp not installed on server. Check Railway build logs."}), 500
    except Exception as e:
        return jsonify({"error": f"yt-dlp error: {str(e)}"}), 500

    if duration < 30:
        return jsonify({"error": "Video too short. Need at least 30 seconds."}), 422

    # Get transcript via Supadata
    transcript = ""
    supadata_key = os.environ.get("SUPADATA_API_KEY", "")
    if supadata_key:
        try:
            resp = requests.get(
                "https://api.supadata.ai/v1/youtube/transcript",
                params={"videoId": video_id, "text": "true"},
                headers={"x-api-key": supadata_key},
                timeout=20
            )
            if resp.ok:
                d = resp.json()
                content = d.get("content", "")
                transcript = content if isinstance(content, str) else " ".join([s.get("text","") for s in content])
        except Exception as e:
            print(f"Supadata error: {e}")

    if not transcript:
        transcript = f"Video: {title}. Duration: {duration} seconds."

    # AI clip detection with Groq
    clips = []
    if groq_key:
        try:
            prompt = f"""You are a viral video expert. Find the 5 BEST moments to clip from this YouTube video for TikTok/Reels/Shorts.

VIDEO: "{title}"
DURATION: {duration} seconds
TRANSCRIPT: {transcript[:4000]}

Return ONLY a valid JSON array with exactly 5 clips:
[
  {{
    "start": <number>,
    "end": <number>,
    "title": "<clip title under 8 words>",
    "hook": "<opening line that grabs attention>",
    "virality_score": <1-10>,
    "reason": "<why this moment is viral>"
  }}
]

Rules:
- Each clip 30-90 seconds long
- start/end within 0 and {duration}
- Space clips throughout the video
- Return ONLY the JSON array"""

            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
                json={
                    "model": "llama-3.3-70b-versatile",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 1024,
                    "temperature": 0.3
                },
                timeout=30
            )
            if resp.ok:
                raw = resp.json()["choices"][0]["message"]["content"].strip()
                raw = re.sub(r'```json|```', '', raw).strip()
                parsed = json.loads(raw)
                clips = [c for c in parsed if
                    isinstance(c.get("start"), (int,float)) and
                    isinstance(c.get("end"), (int,float)) and
                    0 <= c["start"] < c["end"] <= duration and
                    (c["end"] - c["start"]) >= 15
                ]
        except Exception as e:
            print(f"AI clip detection error: {e}")

    # Fallback evenly spaced clips
    if not clips:
        segment = max(duration // 6, 40)
        for i in range(min(5, duration // 40)):
            start = i * segment + 5
            end   = min(start + 60, duration - 5)
            if end > start + 15:
                clips.append({
                    "start": start, "end": end,
                    "title": f"Highlight {i+1}",
                    "hook": "Watch this...",
                    "virality_score": 7,
                    "reason": "Auto-detected segment"
                })

    return jsonify({
        "videoId": video_id,
        "title": title,
        "duration": duration,
        "clips": clips,
        "transcriptLength": len(transcript.split())
    })


# ── Process clip ──────────────────────────────────────────────────────────────
@app.route("/clip", methods=["POST", "OPTIONS"])
def create_clip():
    if request.method == "OPTIONS":
        return jsonify({}), 200

    data     = request.get_json()
    video_id = data.get("videoId", "").strip()
    start    = float(data.get("start", 0))
    end      = float(data.get("end", 60))
    title    = data.get("title", "clip")
    formats  = data.get("formats", ["vertical", "horizontal"])

    if not video_id:
        return jsonify({"error": "Missing videoId"}), 400
    if end - start < 10:
        return jsonify({"error": "Clip must be at least 10 seconds"}), 400
    if end - start > 180:
        return jsonify({"error": "Clip max 3 minutes"}), 400

    job_id = str(uuid.uuid4())[:8]
    JOBS[job_id] = {"status": "queued", "progress": 0, "message": "Starting...", "files": {}}

    thread = threading.Thread(target=process_clip_job, args=(job_id, video_id, start, end, title, formats))
    thread.daemon = True
    thread.start()

    return jsonify({"jobId": job_id})


def process_clip_job(job_id, video_id, start, end, title, formats):
    job_dir   = os.path.join(CLIPS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    yt_url    = f"https://www.youtube.com/watch?v={video_id}"
    raw_video = os.path.join(job_dir, "raw.mp4")

    try:
        update_job(job_id, "downloading", 10, "Downloading video from YouTube...")

        dl = subprocess.run([
            YTDLP,
            "--format", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[ext=mp4]/best",
            "--download-sections", f"*{max(0,start-3)}-{end+3}",
            "--force-keyframes-at-cuts",
            "--merge-output-format", "mp4",
            "--no-playlist",
            "-o", raw_video,
            yt_url
        ], capture_output=True, text=True, timeout=300)

        if dl.returncode != 0 or not os.path.exists(raw_video):
            raise Exception(f"Download failed: {dl.stderr[:400]}")

        update_job(job_id, "cutting", 50, "Cutting to exact timestamps...")

        cut_video = os.path.join(job_dir, "cut.mp4")
        cut = subprocess.run([
            "ffmpeg", "-y",
            "-ss", str(start),
            "-i", raw_video,
            "-t", str(end - start),
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-avoid_negative_ts", "make_zero",
            cut_video
        ], capture_output=True, text=True, timeout=120)

        if cut.returncode != 0:
            raise Exception(f"Cut failed: {cut.stderr[:300]}")

        update_job(job_id, "converting", 70, "Converting to output formats...")

        output_files = {}

        if "vertical" in formats:
            vpath = os.path.join(job_dir, "vertical.mp4")
            vr = subprocess.run([
                "ffmpeg", "-y", "-i", cut_video,
                "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k", "-r", "30",
                vpath
            ], capture_output=True, text=True, timeout=120)
            if vr.returncode == 0 and os.path.exists(vpath):
                output_files["vertical"] = vpath

        if "horizontal" in formats:
            hpath = os.path.join(job_dir, "horizontal.mp4")
            hr = subprocess.run([
                "ffmpeg", "-y", "-i", cut_video,
                "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k", "-r", "30",
                hpath
            ], capture_output=True, text=True, timeout=120)
            if hr.returncode == 0 and os.path.exists(hpath):
                output_files["horizontal"] = hpath

        if not output_files:
            raise Exception("No output files were created. ffmpeg may have failed.")

        file_refs = {}
        for fmt, path in output_files.items():
            size_mb = os.path.getsize(path) / (1024*1024)
            file_refs[fmt] = {
                "downloadUrl": f"/download/{job_id}/{fmt}",
                "sizeMb": round(size_mb, 1),
                "resolution": "1080x1920" if fmt == "vertical" else "1920x1080"
            }

        update_job(job_id, "done", 100, "Clip ready to download!", files=file_refs)
        threading.Timer(3600, lambda: shutil.rmtree(job_dir, ignore_errors=True)).start()

    except Exception as e:
        print(f"Job {job_id} error: {e}")
        update_job(job_id, "error", 0, str(e))


def update_job(job_id, status, progress, message, files=None):
    JOBS[job_id].update({"status": status, "progress": progress, "message": message, "updatedAt": time.time()})
    if files is not None:
        JOBS[job_id]["files"] = files


@app.route("/job/<job_id>", methods=["GET"])
def get_job(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/download/<job_id>/<fmt>", methods=["GET"])
def download_clip(job_id, fmt):
    job = JOBS.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Clip not ready"}), 404
    job_dir  = os.path.join(CLIPS_DIR, job_id)
    filepath = os.path.join(job_dir, f"{fmt}.mp4")
    if not os.path.exists(filepath):
        return jsonify({"error": "File not found"}), 404
    return send_file(filepath, as_attachment=True, download_name=f"vidpost_{fmt}_{job_id}.mp4")


# ── Transcript ────────────────────────────────────────────────────────────────
@app.route("/transcript", methods=["POST", "OPTIONS"])
def get_transcript():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data     = request.get_json()
    video_id = data.get("videoId", "").strip()
    if not video_id:
        return jsonify({"error": "Missing videoId"}), 400

    title = "YouTube Video"
    try:
        r = requests.get(f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json", timeout=5)
        if r.ok:
            title = r.json().get("title", "YouTube Video")
    except: pass

    text = None
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
                text = content if isinstance(content, str) else " ".join([s.get("text","") for s in content])
        except Exception as e:
            print(f"Supadata error: {e}")

    if not text or len(text) < 30:
        return jsonify({"error": "Could not fetch transcript. Paste manually."}), 422

    text = re.sub(r'\[.*?\]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return jsonify({"transcript": text[:10000], "wordCount": len(text.split()), "title": title, "videoId": video_id})


# ── Generate posts ────────────────────────────────────────────────────────────
@app.route("/generate", methods=["POST", "OPTIONS"])
def generate_posts():
    if request.method == "OPTIONS":
        return jsonify({}), 200
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

    groq_key = os.environ.get("GROQ_API_KEY", api_key)
    if not groq_key:
        return jsonify({"error": "No API key"}), 400

    tone_map = {
        "professional": "professional, authoritative, like a top industry expert",
        "casual":       "casual and conversational like texting a smart friend",
        "bold":         "bold and provocative — punchy sentences, strong takes, zero fluff",
        "storytelling": "storytelling — open with a surprising moment, build to the insight"
    }
    tone_desc = tone_map.get(tone, tone_map["professional"])

    prompts = {
        "linkedin": f"""Write a viral LinkedIn post from this transcript.
VIDEO: "{title}" | TONE: {tone_desc}
TRANSCRIPT: {transcript[:3000]}

FORMAT:
[One punchy hook — about the reader, not you — under 12 words]

[emoji] Insight 1 — under 15 words
[emoji] Insight 2 — under 15 words
[emoji] Insight 3 — under 15 words

[One question that drives comments]

#hashtag1 #hashtag2 #hashtag3

BANNED: "Firstly" "Secondly" "Unlock" "Dive in" "Game-changer" "In today's world"
MAX 200 words. Output ONLY the post.""",

        "twitter": f"""Write a 6-tweet viral Twitter thread.
VIDEO: "{title}" | TONE: {tone_desc}
TRANSCRIPT: {transcript[:3000]}

Tweet 1: Bold hook or shocking fact. Under 200 chars.
Tweet 2-5: One sharp specific insight each. Under 240 chars.
Tweet 6: Punchy ending + question. Under 200 chars.
Number each: 1/ 2/ 3/ etc. No hashtags.
Output ONLY the 6 tweets, each on its own line.""",

        "instagram": f"""Write a viral Instagram caption.
VIDEO: "{title}" | TONE: {tone_desc}
TRANSCRIPT: {transcript[:3000]}

Line 1: Hook under 100 chars
[blank line]
3 short paragraphs with emoji
[blank line]
Question to drive comments
[blank line]
15 hashtags

Output ONLY the caption.""",

        "tiktok": f"""Write a punchy TikTok script (45 seconds spoken).
VIDEO: "{title}" | TONE: {tone_desc}
TRANSCRIPT: {transcript[:3000]}

HOOK (0-3s): Bold claim or question.
POINT 1 (3-15s): Short. Punchy.
POINT 2 (15-27s): Short. Punchy.
POINT 3 (27-40s): Short. Punchy.
CTA (40-45s): Follow or question.

Under 130 words. Output ONLY the script.""",

        "youtube_short": f"""Write a YouTube Shorts script (55 seconds).
VIDEO: "{title}" | TONE: {tone_desc}
TRANSCRIPT: {transcript[:3000]}

[HOOK]: One sentence question or fact.
[POINT 1]: Two sentences.
[POINT 2]: Two sentences.
[POINT 3]: Two sentences.
[CTA]: Watch full video + subscribe.

Under 150 words. Output ONLY the script."""
    }

    results = {}
    errors  = {}
    for platform in platforms:
        if platform not in prompts:
            continue
        try:
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
                json={"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompts[platform]}], "max_tokens": 1024, "temperature": 0.75},
                timeout=30
            )
            if resp.status_code == 401:
                return jsonify({"error": "Invalid Groq API key"}), 401
            if resp.status_code == 429:
                return jsonify({"error": "Rate limit. Try again in a moment."}), 429
            if resp.ok:
                results[platform] = resp.json()["choices"][0]["message"]["content"].strip()
            else:
                errors[platform] = f"Error {resp.status_code}"
        except Exception as e:
            errors[platform] = str(e)

    if not results:
        return jsonify({"error": "Generation failed", "details": errors}), 500
    return jsonify({"results": results, "errors": errors, "title": title})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"VidPost AI v2 on port {port} | yt-dlp: {YTDLP}")
    app.run(host="0.0.0.0", port=port, debug=False)
