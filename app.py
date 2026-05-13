from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import re, os, uuid, json, subprocess, threading, time, shutil
import requests

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB upload limit

JOBS = {}
CLIPS_DIR = "/tmp/vidpost_clips"
UPLOADS_DIR = "/tmp/vidpost_uploads"
os.makedirs(CLIPS_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)

def find_ytdlp():
    for p in ["/usr/bin/yt-dlp","/usr/local/bin/yt-dlp","/opt/venv/bin/yt-dlp"]:
        if os.path.exists(p): return p
    try:
        r = subprocess.run(["which","yt-dlp"], capture_output=True, text=True)
        if r.returncode == 0: return r.stdout.strip()
    except: pass
    return "yt-dlp"

YTDLP = find_ytdlp()
print(f"yt-dlp: {YTDLP} | ffmpeg: {bool(shutil.which('ffmpeg'))}")

# ── Health ────────────────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status":"ok","ytdlp":YTDLP,"ffmpeg":bool(shutil.which("ffmpeg"))})

# ── Analyse video (URL mode) ──────────────────────────────────────────────────
@app.route("/analyse", methods=["POST","OPTIONS"])
def analyse():
    if request.method == "OPTIONS": return jsonify({}), 200
    data     = request.get_json()
    url      = data.get("url","").strip()
    api_key  = data.get("apiKey","").strip()
    groq_key = os.environ.get("GROQ_API_KEY", api_key)
    if not url: return jsonify({"error":"Missing YouTube URL"}), 400
    m = re.search(r'(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})', url)
    if not m: return jsonify({"error":"Invalid YouTube URL"}), 400
    video_id = m.group(1)

    # Get title via oEmbed — never IP blocked
    title = "YouTube Video"
    try:
        r = requests.get(f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json", timeout=5)
        if r.ok: title = r.json().get("title", "YouTube Video")
    except: pass

    # Get transcript via Supadata
    transcript = ""
    duration = 600
    supadata_key = os.environ.get("SUPADATA_API_KEY","")
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
                if isinstance(content, list) and content:
                    segments = content
                    transcript = " ".join([s.get("text","") for s in segments])
                    # Estimate duration from last segment offset
                    last = segments[-1]
                    offset = last.get("offset", 0)
                    dur = last.get("duration", 0)
                    if offset > 0:
                        duration = int((offset + dur) / 1000)
                elif isinstance(content, str):
                    transcript = content
        except Exception as e:
            print(f"Supadata error: {e}")

    if not transcript:
        transcript = f"Video: {title}"

    clips = generate_ai_clips(transcript, title, duration, groq_key)
    return jsonify({"videoId": video_id, "title": title, "duration": duration, "clips": clips, "transcriptLength": len(transcript.split()), "source": "url"})

# ── Analyse uploaded file ─────────────────────────────────────────────────────
@app.route("/analyse-file", methods=["POST","OPTIONS"])
def analyse_file():
    if request.method == "OPTIONS": return jsonify({}), 200

    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file    = request.files["file"]
    api_key = request.form.get("apiKey","")
    groq_key = os.environ.get("GROQ_API_KEY", api_key)

    if not file.filename:
        return jsonify({"error": "No file selected"}), 400

    # Save uploaded file
    file_id  = str(uuid.uuid4())[:8]
    ext      = os.path.splitext(file.filename)[1].lower() or ".mp4"
    filepath = os.path.join(UPLOADS_DIR, f"{file_id}{ext}")
    file.save(filepath)

    # Get duration via ffprobe
    duration = 600
    title    = os.path.splitext(file.filename)[0]
    try:
        probe = subprocess.run([
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", filepath
        ], capture_output=True, text=True, timeout=15)
        if probe.returncode == 0:
            info = json.loads(probe.stdout)
            duration = int(float(info.get("format",{}).get("duration", 600)))
    except Exception as e:
        print(f"ffprobe error: {e}")

    # Auto-cleanup after 2 hours
    threading.Timer(7200, lambda: os.remove(filepath) if os.path.exists(filepath) else None).start()

    clips = generate_ai_clips(f"Video file: {title}", title, duration, groq_key)
    return jsonify({"fileId": file_id, "filePath": filepath, "title": title, "duration": duration, "clips": clips, "transcriptLength": 0, "source": "file"})

# ── AI clip generation (shared) ───────────────────────────────────────────────
def generate_ai_clips(transcript, title, duration, groq_key):
    clips = []
    if groq_key:
        try:
            prompt = f"""You are a viral video expert like Opus Clip. Find the 5 BEST moments to clip.

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
    "reason": "<why this clips well>"
  }}
]

Rules: Each clip 30-90 seconds. start/end within 0 and {duration}. Space throughout. Return ONLY the JSON."""

            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
                json={"model":"llama-3.3-70b-versatile","messages":[{"role":"user","content":prompt}],"max_tokens":1024,"temperature":0.3},
                timeout=30
            )
            if resp.ok:
                raw = re.sub(r'```json|```','',resp.json()["choices"][0]["message"]["content"].strip()).strip()
                parsed = json.loads(raw)
                clips = [c for c in parsed if
                    isinstance(c.get("start"),(int,float)) and isinstance(c.get("end"),(int,float)) and
                    0 <= c["start"] < c["end"] <= duration and (c["end"]-c["start"]) >= 15]
        except Exception as e:
            print(f"AI clip error: {e}")

    if not clips:
        seg = max(duration//6, 40)
        for i in range(5):
            s = i*seg+10; e = min(s+60, duration-5)
            if e > s+15:
                clips.append({"start":s,"end":e,"title":f"Highlight {i+1}","hook":"Watch this...","virality_score":7,"reason":"Auto-detected"})
    return clips

# ── Process clip ──────────────────────────────────────────────────────────────
@app.route("/clip", methods=["POST","OPTIONS"])
def create_clip():
    if request.method == "OPTIONS": return jsonify({}), 200
    data      = request.get_json()
    video_id  = data.get("videoId","").strip()
    file_path = data.get("filePath","").strip()  # for uploaded files
    start     = float(data.get("start",0))
    end       = float(data.get("end",60))
    formats   = data.get("formats",["vertical","horizontal"])

    if not video_id and not file_path:
        return jsonify({"error":"Missing videoId or filePath"}), 400
    if end - start < 10:
        return jsonify({"error":"Clip too short"}), 400

    job_id = str(uuid.uuid4())[:8]
    JOBS[job_id] = {"status":"queued","progress":0,"message":"Starting...","files":{}}

    t = threading.Thread(target=process_clip_job, args=(job_id, video_id, file_path, start, end, formats))
    t.daemon = True
    t.start()
    return jsonify({"jobId": job_id})


def process_clip_job(job_id, video_id, file_path, start, end, formats):
    job_dir = os.path.join(CLIPS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    source_video = None

    try:
        # ── Source: uploaded file ─────────────────────────────────────────────
        if file_path and os.path.exists(file_path):
            source_video = file_path
            update_job(job_id, "processing", 30, "Using uploaded video file...")

        # ── Source: YouTube URL ───────────────────────────────────────────────
        elif video_id:
            raw_video = os.path.join(job_dir, "raw.mp4")
            yt_url    = f"https://www.youtube.com/watch?v={video_id}"

            # Try proxy first if configured
            proxy = os.environ.get("RESIDENTIAL_PROXY","")
            proxy_args = ["--proxy", proxy] if proxy else []

            update_job(job_id, "downloading", 10, "Downloading from YouTube...")

            # Attempt 1: with proxy + section download
            dl = subprocess.run([
                YTDLP,
                "--format","bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[ext=mp4]/best",
                "--download-sections", f"*{max(0,start-3)}-{end+3}",
                "--force-keyframes-at-cuts",
                "--merge-output-format","mp4",
                "--no-playlist",
                "--user-agent","Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                *proxy_args,
                "-o", raw_video, yt_url
            ], capture_output=True, text=True, timeout=300)

            if dl.returncode != 0 or not os.path.exists(raw_video):
                update_job(job_id, "downloading", 20, "Retrying with mobile client...")
                # Attempt 2: iPhone user agent, worst quality (faster)
                dl2 = subprocess.run([
                    YTDLP,
                    "--format","worst[ext=mp4]/worst",
                    "--no-playlist",
                    "--user-agent","Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
                    *proxy_args,
                    "-o", raw_video, yt_url
                ], capture_output=True, text=True, timeout=300)

                if dl2.returncode != 0 or not os.path.exists(raw_video):
                    raise Exception(
                        "YouTube is blocking this server's IP. "
                        "Fix: Upload the video file directly using the 'Upload video' button, "
                        "or add a RESIDENTIAL_PROXY to Railway Variables."
                    )

            source_video = raw_video
        else:
            raise Exception("No video source provided")

        # ── Cut to timestamps ─────────────────────────────────────────────────
        update_job(job_id, "cutting", 55, "Cutting to exact timestamps...")
        cut_video = os.path.join(job_dir, "cut.mp4")

        subprocess.run([
            "ffmpeg","-y",
            "-ss", str(start), "-i", source_video,
            "-t", str(end-start),
            "-c:v","libx264","-preset","fast","-crf","23",
            "-c:a","aac","-b:a","128k",
            "-avoid_negative_ts","make_zero",
            cut_video
        ], capture_output=True, text=True, timeout=120)

        if not os.path.exists(cut_video):
            raise Exception("FFmpeg cut failed")

        # ── Convert formats ───────────────────────────────────────────────────
        update_job(job_id, "converting", 75, "Converting to output formats...")
        output_files = {}

        if "vertical" in formats:
            vpath = os.path.join(job_dir, "vertical.mp4")
            subprocess.run([
                "ffmpeg","-y","-i",cut_video,
                "-vf","scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black",
                "-c:v","libx264","-preset","fast","-crf","23",
                "-c:a","aac","-b:a","128k","-r","30", vpath
            ], capture_output=True, text=True, timeout=120)
            if os.path.exists(vpath): output_files["vertical"] = vpath

        if "horizontal" in formats:
            hpath = os.path.join(job_dir, "horizontal.mp4")
            subprocess.run([
                "ffmpeg","-y","-i",cut_video,
                "-vf","scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black",
                "-c:v","libx264","-preset","fast","-crf","23",
                "-c:a","aac","-b:a","128k","-r","30", hpath
            ], capture_output=True, text=True, timeout=120)
            if os.path.exists(hpath): output_files["horizontal"] = hpath

        if not output_files:
            raise Exception("No output files created")

        file_refs = {
            fmt: {"downloadUrl":f"/download/{job_id}/{fmt}","sizeMb":round(os.path.getsize(p)/(1024*1024),1),"resolution":"1080x1920" if fmt=="vertical" else "1920x1080"}
            for fmt, p in output_files.items()
        }
        update_job(job_id, "done", 100, "Clip ready to download!", files=file_refs)
        threading.Timer(3600, lambda: shutil.rmtree(job_dir, ignore_errors=True)).start()

    except Exception as e:
        print(f"Job {job_id} error: {e}")
        update_job(job_id, "error", 0, str(e))


def update_job(job_id, status, progress, message, files=None):
    JOBS[job_id].update({"status":status,"progress":progress,"message":message,"updatedAt":time.time()})
    if files is not None: JOBS[job_id]["files"] = files


@app.route("/job/<job_id>", methods=["GET"])
def get_job(job_id):
    job = JOBS.get(job_id)
    if not job: return jsonify({"error":"Job not found"}), 404
    return jsonify(job)


@app.route("/download/<job_id>/<fmt>", methods=["GET"])
def download_clip(job_id, fmt):
    job = JOBS.get(job_id)
    if not job or job["status"] != "done": return jsonify({"error":"Not ready"}), 404
    filepath = os.path.join(CLIPS_DIR, job_id, f"{fmt}.mp4")
    if not os.path.exists(filepath): return jsonify({"error":"File not found"}), 404
    return send_file(filepath, as_attachment=True, download_name=f"vidpost_{fmt}_{job_id}.mp4")


@app.route("/transcript", methods=["POST","OPTIONS"])
def get_transcript():
    if request.method == "OPTIONS": return jsonify({}), 200
    data     = request.get_json()
    video_id = data.get("videoId","").strip()
    if not video_id: return jsonify({"error":"Missing videoId"}), 400

    title = "YouTube Video"
    try:
        r = requests.get(f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json", timeout=5)
        if r.ok: title = r.json().get("title","YouTube Video")
    except: pass

    supadata_key = os.environ.get("SUPADATA_API_KEY","")
    if not supadata_key: return jsonify({"error":"Supadata key not set"}), 500

    try:
        resp = requests.get(
            "https://api.supadata.ai/v1/youtube/transcript",
            params={"videoId":video_id,"text":"true"},
            headers={"x-api-key":supadata_key},
            timeout=15
        )
        if resp.ok:
            d = resp.json()
            content = d.get("content","")
            text = content if isinstance(content,str) else " ".join([s.get("text","") for s in content])
            if text and len(text) > 30:
                text = re.sub(r'\[.*?\]','',text)
                text = re.sub(r'\s+',' ',text).strip()
                return jsonify({"transcript":text[:10000],"wordCount":len(text.split()),"title":title,"videoId":video_id})
    except Exception as e:
        print(f"Transcript error: {e}")

    return jsonify({"error":"Could not fetch transcript. Paste manually."}), 422


@app.route("/generate", methods=["POST","OPTIONS"])
def generate_posts():
    if request.method == "OPTIONS": return jsonify({}), 200
    data = request.get_json()
    if not data: return jsonify({"error":"No body"}), 400

    transcript = data.get("transcript","").strip()
    title      = data.get("title","YouTube Video")
    tone       = data.get("tone","professional")
    platforms  = data.get("platforms",["linkedin"])
    api_key    = data.get("apiKey","").strip()

    if not transcript: return jsonify({"error":"Missing transcript"}), 400
    groq_key = os.environ.get("GROQ_API_KEY", api_key)
    if not groq_key: return jsonify({"error":"No API key"}), 400

    tone_map = {
        "professional":"professional and insightful like a top industry expert",
        "casual":"casual and conversational like texting a smart friend",
        "bold":"bold and provocative with punchy short sentences and strong takes",
        "storytelling":"storytelling — open with a surprising moment then build to the insight"
    }
    tone_desc = tone_map.get(tone, tone_map["professional"])

    prompts = {
        "linkedin":f'Write a viral LinkedIn post.\nVIDEO: "{title}"\nTONE: {tone_desc}\nTRANSCRIPT: {transcript[:3000]}\n\nFORMAT:\n[Punchy hook under 12 words — about the reader]\n\n[emoji] Insight 1 under 15 words\n[emoji] Insight 2 under 15 words\n[emoji] Insight 3 under 15 words\n\n[Question that drives comments]\n\n#tag1 #tag2 #tag3\n\nBANNED: "Firstly" "Unlock" "Game-changer"\nMax 200 words. Output ONLY the post.',
        "twitter":f'Write a 6-tweet viral Twitter thread.\nVIDEO: "{title}"\nTONE: {tone_desc}\nTRANSCRIPT: {transcript[:3000]}\n\nTweet 1: Bold hook under 200 chars\nTweets 2-5: One sharp insight each under 240 chars\nTweet 6: Punchy ending + question\nNumber: 1/ 2/ etc. No hashtags.\nOutput ONLY the 6 tweets each on its own line.',
        "instagram":f'Write a viral Instagram caption.\nVIDEO: "{title}"\nTONE: {tone_desc}\nTRANSCRIPT: {transcript[:3000]}\n\nLine 1: Hook under 100 chars\n\n3 short paragraphs with emoji\n\nQuestion for comments\n\n15 hashtags\n\nOutput ONLY the caption.',
        "tiktok":f'Write a 45-second TikTok script.\nVIDEO: "{title}"\nTONE: {tone_desc}\nTRANSCRIPT: {transcript[:3000]}\n\nHOOK(0-3s): Bold claim or question\nPOINT 1(3-15s): Short punchy insight\nPOINT 2(15-27s): Short punchy insight\nPOINT 3(27-40s): Short punchy insight\nCTA(40-45s): Follow or question\n\nUnder 130 words. Output ONLY the script.',
        "youtube_short":f'Write a YouTube Shorts script (55 seconds).\nVIDEO: "{title}"\nTONE: {tone_desc}\nTRANSCRIPT: {transcript[:3000]}\n\n[HOOK]: One sentence\n[POINT 1]: Two sentences\n[POINT 2]: Two sentences\n[POINT 3]: Two sentences\n[CTA]: Watch full video + subscribe\n\nUnder 150 words. Output ONLY the script.'
    }

    results, errors = {}, {}
    for platform in platforms:
        if platform not in prompts: continue
        try:
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization":f"Bearer {groq_key}","Content-Type":"application/json"},
                json={"model":"llama-3.3-70b-versatile","messages":[{"role":"user","content":prompts[platform]}],"max_tokens":1024,"temperature":0.75},
                timeout=30
            )
            if resp.status_code == 401: return jsonify({"error":"Invalid Groq API key"}), 401
            if resp.status_code == 429: return jsonify({"error":"Rate limit. Try again shortly."}), 429
            if resp.ok: results[platform] = resp.json()["choices"][0]["message"]["content"].strip()
            else: errors[platform] = f"Error {resp.status_code}"
        except Exception as e:
            errors[platform] = str(e)

    if not results: return jsonify({"error":"Generation failed","details":errors}), 500
    return jsonify({"results":results,"errors":errors,"title":title})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"VidPost AI v2 on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
