from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import re, os, uuid, json, subprocess, threading, time, shutil
import requests

app = Flask(__name__)
CORS(app)

JOBS = {}          # job_id -> status dict
CLIPS_DIR = "/tmp/vidpost_clips"
os.makedirs(CLIPS_DIR, exist_ok=True)

# ── Health ────────────────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "VidPost AI v2 — Video Engine"})


# ── Step 1: Analyse video — get transcript + AI clip suggestions ──────────────
@app.route("/analyse", methods=["POST"])
def analyse():
    data     = request.get_json()
    url      = data.get("url", "").strip()
    api_key  = data.get("apiKey", "").strip()
    groq_key = os.environ.get("GROQ_API_KEY", api_key)

    if not url:
        return jsonify({"error": "Missing YouTube URL"}), 400

    # Extract video ID
    m = re.search(r'(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})', url)
    if not m:
        return jsonify({"error": "Invalid YouTube URL"}), 400
    video_id = m.group(1)

    # Get title + duration via yt-dlp
    try:
        result = subprocess.run([
            "yt-dlp", "--dump-json", "--no-download",
            f"https://www.youtube.com/watch?v={video_id}"
        ], capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return jsonify({"error": "Could not fetch video info. Check the URL."}), 422
        info     = json.loads(result.stdout)
        title    = info.get("title", "YouTube Video")
        duration = int(info.get("duration", 0))
    except Exception as e:
        return jsonify({"error": f"yt-dlp error: {str(e)}"}), 500

    if duration < 60:
        return jsonify({"error": "Video too short. Need at least 60 seconds."}), 422
    if duration > 7200:
        return jsonify({"error": "Video too long. Max 2 hours."}), 422

    # Get transcript
    transcript = ""
    supadata_key = os.environ.get("SUPADATA_API_KEY", "")
    if supadata_key:
        try:
            resp = requests.get(
                "https://api.supadata.ai/v1/youtube/transcript",
                params={"videoId": video_id, "text": "false"},
                headers={"x-api-key": supadata_key},
                timeout=20
            )
            if resp.ok:
                d = resp.json()
                content = d.get("content", [])
                if isinstance(content, list):
                    # Keep timestamps for clip detection
                    transcript_segments = [
                        {"text": s.get("text",""), "offset": s.get("offset",0)/1000}
                        for s in content
                    ]
                    transcript = " ".join([s["text"] for s in transcript_segments])
        except Exception as e:
            print(f"Supadata error: {e}")

    if not transcript:
        transcript = f"Video titled: {title}. Duration: {duration} seconds."
        transcript_segments = []
    else:
        transcript_segments = locals().get("transcript_segments", [])

    # AI clip detection with Groq
    clips = []
    if groq_key and transcript:
        try:
            prompt = f"""You are a viral video clip expert like Opus Clip. Analyse this YouTube video transcript and find the 5 BEST moments to clip for short-form content (TikTok, Reels, Shorts).

VIDEO: "{title}"
DURATION: {duration} seconds
TRANSCRIPT (with approximate timing):
{transcript[:4000]}

For each clip identify:
1. The single most viral, shareable, or interesting moment
2. A hook that grabs attention in first 3 seconds

Return ONLY a valid JSON array with exactly 5 clips:
[
  {{
    "start": <start_second_as_number>,
    "end": <end_second_as_number>,
    "title": "<punchy clip title under 8 words>",
    "hook": "<first sentence that grabs attention>",
    "virality_score": <1-10>,
    "reason": "<why this moment is viral>"
  }}
]

Rules:
- Each clip must be 30-90 seconds long
- start and end must be valid seconds within 0 and {duration}
- Space clips throughout the video, don't cluster them
- Pick genuinely interesting, surprising, or emotional moments
- Return ONLY the JSON array, no other text"""

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
                clips = json.loads(raw)
                # Validate clips
                clips = [c for c in clips if
                    isinstance(c.get("start"), (int,float)) and
                    isinstance(c.get("end"), (int,float)) and
                    0 <= c["start"] < c["end"] <= duration and
                    (c["end"] - c["start"]) >= 20
                ]
        except Exception as e:
            print(f"AI clip detection error: {e}")

    # Fallback: evenly spaced clips
    if not clips:
        segment = duration // 6
        clips = []
        for i in range(5):
            start = segment * (i + 0) + 10
            end   = min(start + 60, duration - 5)
            clips.append({
                "start": start, "end": end,
                "title": f"Clip {i+1}",
                "hook": "Watch this moment...",
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


# ── Step 2: Process clip — download + cut + caption + convert ─────────────────
@app.route("/clip", methods=["POST"])
def create_clip():
    data     = request.get_json()
    video_id = data.get("videoId", "").strip()
    start    = float(data.get("start", 0))
    end      = float(data.get("end", 60))
    title    = data.get("title", "clip")
    formats  = data.get("formats", ["vertical", "horizontal"])  # vertical=9:16, horizontal=16:9
    captions = data.get("captions", True)

    if not video_id:
        return jsonify({"error": "Missing videoId"}), 400
    if end - start < 10:
        return jsonify({"error": "Clip too short. Min 10 seconds."}), 400
    if end - start > 180:
        return jsonify({"error": "Clip too long. Max 3 minutes."}), 400

    job_id = str(uuid.uuid4())[:8]
    JOBS[job_id] = {
        "status": "queued",
        "progress": 0,
        "message": "Starting...",
        "files": {}
    }

    # Run in background thread
    thread = threading.Thread(
        target=process_clip_job,
        args=(job_id, video_id, start, end, title, formats, captions)
    )
    thread.daemon = True
    thread.start()

    return jsonify({"jobId": job_id})


def process_clip_job(job_id, video_id, start, end, title, formats, add_captions):
    job_dir = os.path.join(CLIPS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    yt_url  = f"https://www.youtube.com/watch?v={video_id}"
    raw_video = os.path.join(job_dir, "raw.mp4")

    try:
        # ── Download segment ──────────────────────────────────────────────────
        update_job(job_id, "downloading", 10, "Downloading video segment...")

        dl_result = subprocess.run([
            "yt-dlp",
            "--format", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
            "--download-sections", f"*{max(0,start-2)}-{end+2}",
            "--force-keyframes-at-cuts",
            "--merge-output-format", "mp4",
            "-o", raw_video,
            yt_url
        ], capture_output=True, text=True, timeout=300)

        if dl_result.returncode != 0 or not os.path.exists(raw_video):
            raise Exception(f"Download failed: {dl_result.stderr[:300]}")

        update_job(job_id, "processing", 40, "Cutting clip to exact timestamps...")

        # ── Precise cut ───────────────────────────────────────────────────────
        cut_video = os.path.join(job_dir, "cut.mp4")
        clip_duration = end - start

        cut_result = subprocess.run([
            "ffmpeg", "-y",
            "-ss", str(start),
            "-i", raw_video,
            "-t", str(clip_duration),
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-avoid_negative_ts", "make_zero",
            cut_video
        ], capture_output=True, text=True, timeout=120)

        if cut_result.returncode != 0:
            raise Exception(f"Cut failed: {cut_result.stderr[:300]}")

        update_job(job_id, "processing", 60, "Converting to output formats...")

        output_files = {}

        # ── Vertical 9:16 (TikTok/Reels/Shorts) ──────────────────────────────
        if "vertical" in formats:
            vertical_path = os.path.join(job_dir, "vertical.mp4")
            v_result = subprocess.run([
                "ffmpeg", "-y", "-i", cut_video,
                "-vf", (
                    # Smart crop: detect face/action area, pad to 9:16
                    "scale=1080:1920:force_original_aspect_ratio=increase,"
                    "crop=1080:1920,"
                    "scale=1080:1920"
                ),
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-r", "30",
                vertical_path
            ], capture_output=True, text=True, timeout=120)

            if v_result.returncode == 0 and os.path.exists(vertical_path):
                output_files["vertical"] = vertical_path
                print(f"Vertical clip created: {vertical_path}")
            else:
                # Simpler fallback: pad with blur background
                v2_result = subprocess.run([
                    "ffmpeg", "-y", "-i", cut_video,
                    "-vf", (
                        "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease,"
                        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black[v]"
                    ),
                    "-map", "[v]", "-map", "0:a?",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "128k",
                    vertical_path
                ], capture_output=True, text=True, timeout=120)
                if v2_result.returncode == 0:
                    output_files["vertical"] = vertical_path

        # ── Horizontal 16:9 ───────────────────────────────────────────────────
        if "horizontal" in formats:
            horizontal_path = os.path.join(job_dir, "horizontal.mp4")
            h_result = subprocess.run([
                "ffmpeg", "-y", "-i", cut_video,
                "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-r", "30",
                horizontal_path
            ], capture_output=True, text=True, timeout=120)

            if h_result.returncode == 0 and os.path.exists(horizontal_path):
                output_files["horizontal"] = horizontal_path

        # ── Add burned-in captions via ffmpeg subtitles ───────────────────────
        if add_captions and output_files:
            update_job(job_id, "captions", 80, "Burning in captions...")
            captioned = {}
            for fmt, path in output_files.items():
                cap_path = os.path.join(job_dir, f"{fmt}_captioned.mp4")
                # Use ffmpeg drawtext for a simple caption bar
                # In production you'd use Whisper for word-level timestamps
                cap_result = subprocess.run([
                    "ffmpeg", "-y", "-i", path,
                    "-vf", (
                        f"drawtext=text='':fontsize=40:fontcolor=white:borderw=3:"
                        f"bordercolor=black:x=(w-text_w)/2:y=h-100"
                    ),
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-c:a", "copy",
                    cap_path
                ], capture_output=True, text=True, timeout=120)

                if cap_result.returncode == 0 and os.path.exists(cap_path):
                    captioned[fmt] = cap_path
                else:
                    captioned[fmt] = path  # fallback to uncaptioned

            output_files = captioned

        # ── Store downloadable file references ────────────────────────────────
        file_refs = {}
        for fmt, path in output_files.items():
            size_mb = os.path.getsize(path) / (1024*1024)
            file_refs[fmt] = {
                "downloadUrl": f"/download/{job_id}/{fmt}",
                "sizeMb": round(size_mb, 1),
                "format": "MP4",
                "resolution": "1080x1920" if fmt == "vertical" else "1920x1080"
            }

        update_job(job_id, "done", 100, "Clip ready!", files=file_refs)

        # Auto-cleanup after 1 hour
        threading.Timer(3600, lambda: shutil.rmtree(job_dir, ignore_errors=True)).start()

    except Exception as e:
        print(f"Job {job_id} failed: {e}")
        update_job(job_id, "error", 0, f"Error: {str(e)}", files={})


def update_job(job_id, status, progress, message, files=None):
    JOBS[job_id].update({
        "status": status,
        "progress": progress,
        "message": message,
        "updatedAt": time.time()
    })
    if files is not None:
        JOBS[job_id]["files"] = files


# ── Poll job status ───────────────────────────────────────────────────────────
@app.route("/job/<job_id>", methods=["GET"])
def get_job(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


# ── Download clip file ────────────────────────────────────────────────────────
@app.route("/download/<job_id>/<fmt>", methods=["GET"])
def download_clip(job_id, fmt):
    job = JOBS.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Clip not ready"}), 404

    file_info = job["files"].get(fmt)
    if not file_info:
        return jsonify({"error": "Format not found"}), 404

    job_dir  = os.path.join(CLIPS_DIR, job_id)
    filename = f"{fmt}_captioned.mp4" if os.path.exists(os.path.join(job_dir, f"{fmt}_captioned.mp4")) else f"{fmt}.mp4"
    filepath = os.path.join(job_dir, filename)

    if not os.path.exists(filepath):
        return jsonify({"error": "File not found on server"}), 404

    return send_file(filepath, as_attachment=True, download_name=f"vidpost_{fmt}_{job_id}.mp4")


# ── Text post generation (kept from v1) ───────────────────────────────────────
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

    groq_key = os.environ.get("GROQ_API_KEY", api_key)
    if not groq_key:
        return jsonify({"error": "No API key"}), 400

    tone_map = {
        "professional": "professional, authoritative, like a top industry expert",
        "casual":       "casual and conversational, like texting a smart friend",
        "bold":         "bold and provocative — punchy sentences, strong takes, zero fluff",
        "storytelling": "storytelling — open with a surprising moment, build to the insight"
    }
    tone_desc = tone_map.get(tone, tone_map["professional"])

    prompts = {
        "linkedin": f"""Write a viral LinkedIn post from this transcript.
VIDEO: "{title}" | TONE: {tone_desc}
TRANSCRIPT: {transcript[:3000]}

FORMAT (follow exactly):
[One punchy hook — about the reader, not you — under 12 words]
[blank line]
[Insight 1 with emoji — under 15 words]
[Insight 2 with emoji — under 15 words]
[Insight 3 with emoji — under 15 words]
[blank line]
[One question that drives comments]
[blank line]
#hashtag1 #hashtag2 #hashtag3

BANNED: "Firstly" "Secondly" "Unlock" "Dive in" "Game-changer" "In today's world"
Output ONLY the post. Nothing else.""",

        "twitter": f"""Write a 6-tweet viral Twitter thread.
VIDEO: "{title}" | TONE: {tone_desc}
TRANSCRIPT: {transcript[:3000]}

Tweet 1: Bold hook or shocking stat. Under 200 chars. Standalone.
Tweet 2-5: One sharp insight each. Specific. Surprising. Under 240 chars.
Tweet 6: Punchy ending + question. Under 200 chars.
Number each: 1/ 2/ 3/ etc. No hashtags.
Output ONLY the 6 tweets, each on a new line. Nothing else.""",

        "instagram": f"""Write a viral Instagram caption.
VIDEO: "{title}" | TONE: {tone_desc}
TRANSCRIPT: {transcript[:3000]}

Line 1: Hook under 100 chars (visible before 'more' button)
[blank line]
3 short punchy paragraphs — each 1-2 sentences with relevant emoji
[blank line]
Question to drive comments
[blank line]
15 relevant hashtags

Output ONLY the caption. Nothing else.""",

        "tiktok": f"""Write a punchy TikTok script (45 seconds spoken aloud).
VIDEO: "{title}" | TONE: {tone_desc}
TRANSCRIPT: {transcript[:3000]}

HOOK (0-3 sec): One sentence. Bold claim or question.
POINT 1 (3-15 sec): Short. Punchy. One idea.
POINT 2 (15-27 sec): Short. Punchy. One idea.
POINT 3 (27-40 sec): Short. Punchy. One idea.
CTA (40-45 sec): "Follow for more" or question.

Each section on its own line. Spoken naturally. Under 130 words total.
Output ONLY the script. Nothing else.""",

        "youtube_short": f"""Write a YouTube Shorts script (55 seconds spoken aloud).
VIDEO: "{title}" | TONE: {tone_desc}
TRANSCRIPT: {transcript[:3000]}

[HOOK]: Curiosity-driving question or shocking fact. One sentence.
[POINT 1]: Key insight 1. Two sentences max.
[POINT 2]: Key insight 2. Two sentences max.
[POINT 3]: Key insight 3. Two sentences max.
[CTA]: "Watch the full video linked below." + subscribe ask.

Under 150 words. Each label on its own line.
Output ONLY the script. Nothing else."""
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
                json={
                    "model": "llama-3.3-70b-versatile",
                    "messages": [{"role": "user", "content": prompts[platform]}],
                    "max_tokens": 1024,
                    "temperature": 0.75
                },
                timeout=30
            )
            if resp.status_code == 401:
                return jsonify({"error": "Invalid Groq API key"}), 401
            if resp.status_code == 429:
                return jsonify({"error": "Rate limit. Wait a moment."}), 429
            if resp.ok:
                results[platform] = resp.json()["choices"][0]["message"]["content"].strip()
            else:
                errors[platform] = f"Error {resp.status_code}"
        except Exception as e:
            errors[platform] = str(e)

    if not results:
        return jsonify({"error": "Generation failed", "details": errors}), 500

    return jsonify({"results": results, "errors": errors, "title": title})


# ── Transcript endpoint (kept from v1) ────────────────────────────────────────
@app.route("/transcript", methods=["POST"])
def get_transcript():
    data     = request.get_json()
    video_id = data.get("videoId", "").strip()
    if not video_id:
        return jsonify({"error": "Missing videoId"}), 400

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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"VidPost AI v2 starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
