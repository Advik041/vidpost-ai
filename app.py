from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import re, os, uuid, json, subprocess, threading, time, shutil
import requests

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024

JOBS = {}
CLIPS_DIR = "/tmp/vidpost_clips"
UPLOADS_DIR = "/tmp/vidpost_uploads"
os.makedirs(CLIPS_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)

def find_ytdlp():
    for p in ["/usr/bin/yt-dlp","/usr/local/bin/yt-dlp","/opt/venv/bin/yt-dlp","/root/.nix-profile/bin/yt-dlp"]:
        if os.path.exists(p): return p
    try:
        r = subprocess.run(["which","yt-dlp"], capture_output=True, text=True)
        if r.returncode == 0: return r.stdout.strip()
    except: pass
    return "yt-dlp"

YTDLP    = find_ytdlp()
PROXY    = os.environ.get("PROXY_URL", "")
SUPADATA = os.environ.get("SUPADATA_API_KEY", "")
print(f"yt-dlp:{YTDLP} proxy:{'yes' if PROXY else 'no'} supadata:{'yes' if SUPADATA else 'no'}")

def ytdlp_base():
    cmd = [YTDLP, "--no-playlist",
           "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"]
    if PROXY: cmd += ["--proxy", PROXY]
    return cmd

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok", "ytdlp": YTDLP,
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "proxy": "configured" if PROXY else "not set",
        "supadata": "configured" if SUPADATA else "not set"
    })

# ── Debug endpoint — test ffmpeg on uploaded file ─────────────────────────────
@app.route("/debug-clip", methods=["POST","OPTIONS"])
def debug_clip():
    if request.method == "OPTIONS": return jsonify({}), 200
    data        = request.get_json()
    upload_path = data.get("uploadPath","").strip()

    if not upload_path:
        return jsonify({"error": "Missing uploadPath"}), 400

    result = {"upload_path": upload_path}

    # Check file exists
    result["file_exists"] = os.path.exists(upload_path)
    if not result["file_exists"]:
        # List what IS in uploads dir
        try:
            files = os.listdir(UPLOADS_DIR)
            result["uploads_dir_contents"] = files
        except Exception as e:
            result["uploads_dir_error"] = str(e)
        return jsonify(result)

    # Get file size
    result["file_size_bytes"] = os.path.getsize(upload_path)

    # Run ffprobe to check video info
    try:
        probe = subprocess.run([
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", upload_path
        ], capture_output=True, text=True, timeout=30)
        result["ffprobe_returncode"] = probe.returncode
        if probe.returncode == 0:
            info = json.loads(probe.stdout)
            fmt = info.get("format", {})
            result["duration"] = fmt.get("duration")
            result["format_name"] = fmt.get("format_name")
            streams = info.get("streams", [])
            result["streams"] = [{"codec_type": s.get("codec_type"), "codec_name": s.get("codec_name")} for s in streams]
        else:
            result["ffprobe_stderr"] = probe.stderr[:500]
    except Exception as e:
        result["ffprobe_error"] = str(e)

    # Try a test cut
    test_output = os.path.join("/tmp", f"test_cut_{uuid.uuid4().hex[:6]}.mp4")
    try:
        cut = subprocess.run([
            "ffmpeg", "-y", "-ss", "10", "-i", upload_path,
            "-t", "5", "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", test_output
        ], capture_output=True, text=True, timeout=60)
        result["ffmpeg_returncode"] = cut.returncode
        result["ffmpeg_stdout"] = cut.stdout[-500:] if cut.stdout else ""
        result["ffmpeg_stderr"] = cut.stderr[-1000:] if cut.stderr else ""
        if os.path.exists(test_output):
            result["test_cut_size_bytes"] = os.path.getsize(test_output)
            os.remove(test_output)
        else:
            result["test_cut_size_bytes"] = 0
    except Exception as e:
        result["ffmpeg_error"] = str(e)

    return jsonify(result)

@app.route("/analyse", methods=["POST","OPTIONS"])
def analyse():
    if request.method == "OPTIONS": return jsonify({}), 200
    data     = request.get_json()
    url      = data.get("url","").strip()
    api_key  = data.get("apiKey","").strip()
    groq_key = os.environ.get("GROQ_API_KEY", api_key)
    if not url: return jsonify({"error":"Missing URL"}), 400
    m = re.search(r'(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})', url)
    if not m: return jsonify({"error":"Invalid YouTube URL"}), 400
    video_id = m.group(1)
    title = "YouTube Video"
    try:
        r = requests.get(f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json", timeout=5)
        if r.ok: title = r.json().get("title", title)
    except: pass
    duration = 600
    try:
        cmd = ytdlp_base() + ["--dump-json","--no-download", f"https://www.youtube.com/watch?v={video_id}"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        if result.returncode == 0:
            info = json.loads(result.stdout)
            title = info.get("title", title)
            duration = int(info.get("duration", 600))
    except Exception as e:
        print(f"yt-dlp info error: {e}")
    transcript = f"Video: {title}. Duration: {duration} seconds."
    if SUPADATA:
        try:
            resp = requests.get("https://api.supadata.ai/v1/youtube/transcript",
                params={"videoId": video_id, "text": "true"},
                headers={"x-api-key": SUPADATA}, timeout=20)
            if resp.ok:
                d = resp.json(); c = d.get("content","")
                t = c if isinstance(c,str) else " ".join([s.get("text","") for s in c])
                if t and len(t) > 30: transcript = t
        except Exception as e:
            print(f"Supadata error: {e}")
    clips = _detect_clips(title, duration, transcript, groq_key)
    return jsonify({"videoId":video_id,"title":title,"duration":duration,"clips":clips,"transcriptLength":len(transcript.split()),"mode":"url"})

@app.route("/analyse-upload", methods=["POST","OPTIONS"])
def analyse_upload():
    if request.method == "OPTIONS": return jsonify({}), 200
    if "video" not in request.files: return jsonify({"error":"No video file"}), 400
    file     = request.files["video"]
    api_key  = request.form.get("apiKey","")
    groq_key = os.environ.get("GROQ_API_KEY", api_key)
    upload_id   = str(uuid.uuid4())[:8]
    ext         = os.path.splitext(file.filename)[1].lower() or ".mp4"
    upload_path = os.path.join(UPLOADS_DIR, f"{upload_id}{ext}")
    file.save(upload_path)
    print(f"Saved upload: {upload_path} size:{os.path.getsize(upload_path)}")
    duration = 600
    title    = os.path.splitext(file.filename)[0] or "Uploaded Video"
    try:
        probe = subprocess.run(["ffprobe","-v","quiet","-print_format","json",
            "-show_format","-show_streams",upload_path],
            capture_output=True, text=True, timeout=30)
        if probe.returncode == 0:
            info = json.loads(probe.stdout)
            dur = float(info.get("format",{}).get("duration",600))
            duration = int(dur)
            print(f"Duration: {duration}s")
    except Exception as e:
        print(f"ffprobe error: {e}")
    clips = _detect_clips(title, duration, f"Uploaded video: {title}. Duration: {duration}s.", groq_key)
    # Store upload_path in a global dict so it persists
    UPLOAD_STORE[upload_id] = upload_path
    threading.Timer(7200, lambda: _cleanup_upload(upload_id, upload_path)).start()
    return jsonify({"uploadId":upload_id,"uploadPath":upload_path,
        "title":title,"duration":duration,"clips":clips,"transcriptLength":0,"mode":"upload"})

UPLOAD_STORE = {}

def _cleanup_upload(upload_id, path):
    UPLOAD_STORE.pop(upload_id, None)
    if os.path.exists(path):
        os.remove(path)

def _detect_clips(title, duration, transcript, groq_key):
    clips = []
    if groq_key:
        try:
            prompt = f"""Find the 5 best viral moments to clip from this video.
VIDEO: "{title}"
DURATION: {duration} seconds
TRANSCRIPT: {transcript[:3000]}

Return ONLY a valid JSON array:
[{{"start":10,"end":70,"title":"Clip Title","hook":"Opening line","virality_score":8,"reason":"Why viral"}}]

Rules: 30-90 seconds each, within 0-{duration}, spaced throughout. ONLY return JSON array."""
            resp = requests.post("https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization":f"Bearer {groq_key}","Content-Type":"application/json"},
                json={"model":"llama-3.3-70b-versatile","messages":[{"role":"user","content":prompt}],
                      "max_tokens":800,"temperature":0.1},
                timeout=30)
            if resp.ok:
                raw = resp.json()["choices"][0]["message"]["content"].strip()
                match = re.search(r'\[[\s\S]*\]', raw)
                if match:
                    parsed = json.loads(match.group())
                    clips = [c for c in parsed if
                        isinstance(c.get("start"),(int,float)) and isinstance(c.get("end"),(int,float)) and
                        0 <= c["start"] < c["end"] <= duration and (c["end"]-c["start"]) >= 15]
        except Exception as e:
            print(f"AI clip error: {e}")
    if not clips:
        seg = max(duration//6, 40)
        for i in range(5):
            s=i*seg+10; e=min(s+60,duration-5)
            if e>s+15: clips.append({"start":s,"end":e,"title":f"Highlight {i+1}",
                "hook":"Watch this...","virality_score":7,"reason":"Auto-detected"})
    return clips

@app.route("/clip", methods=["POST","OPTIONS"])
def create_clip():
    if request.method == "OPTIONS": return jsonify({}), 200
    data        = request.get_json()
    video_id    = data.get("videoId","").strip()
    upload_path = data.get("uploadPath","").strip()
    start       = float(data.get("start",0))
    end         = float(data.get("end",60))
    formats     = data.get("formats",["vertical","horizontal"])
    if not video_id and not upload_path: return jsonify({"error":"Missing source"}), 400
    if end-start < 5: return jsonify({"error":"Clip too short"}), 400

    # Resolve upload path from store if needed
    if upload_path and not os.path.exists(upload_path):
        # Try to find by upload_id
        for uid, path in UPLOAD_STORE.items():
            if uid in upload_path:
                upload_path = path
                print(f"Resolved upload path: {upload_path}")
                break

    job_id = str(uuid.uuid4())[:8]
    JOBS[job_id] = {"status":"queued","progress":0,"message":"Starting...","files":{}}
    t = threading.Thread(target=process_clip_job, args=(job_id,video_id,upload_path,start,end,formats))
    t.daemon = True; t.start()
    return jsonify({"jobId":job_id})

def process_clip_job(job_id, video_id, upload_path, start, end, formats):
    job_dir   = os.path.join(CLIPS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    raw_video = None

    try:
        if upload_path and os.path.exists(upload_path):
            raw_video = upload_path
            size = os.path.getsize(upload_path)
            print(f"Job {job_id}: Using uploaded file {upload_path} ({size} bytes)")
            update_job(job_id,"processing",20,f"Using uploaded file ({size//1024}KB)...")
        elif video_id:
            update_job(job_id,"downloading",10,"Downloading from YouTube...")
            raw_video = os.path.join(job_dir,"raw.mp4")
            yt_url = f"https://www.youtube.com/watch?v={video_id}"
            cmd = ytdlp_base() + [
                "--format","bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[ext=mp4]/best",
                "--download-sections", f"*{max(0,start-3)}-{end+3}",
                "--force-keyframes-at-cuts","--merge-output-format","mp4",
                "-o", raw_video, yt_url]
            dl = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if dl.returncode != 0 or not os.path.exists(raw_video):
                raise Exception(f"Download failed. Try uploading the video file directly.")
        else:
            raise Exception("No video source provided")

        if not raw_video or not os.path.exists(raw_video):
            raise Exception(f"Source video not found at: {raw_video}")

        file_size = os.path.getsize(raw_video)
        print(f"Job {job_id}: Source video size: {file_size} bytes")
        if file_size == 0:
            raise Exception("Source video file is empty (0 bytes)")

        update_job(job_id,"cutting",40,"Cutting clip to timestamps...")
        cut_video = os.path.join(job_dir,"cut.mp4")

        # Use -c copy first (fastest), fallback to re-encode
        cut = subprocess.run([
            "ffmpeg","-y",
            "-ss", str(start),
            "-i", raw_video,
            "-t", str(end-start),
            "-c", "copy",
            "-avoid_negative_ts","make_zero",
            cut_video
        ], capture_output=True, text=True, timeout=120)

        print(f"Job {job_id}: Cut returncode={cut.returncode}")
        print(f"Job {job_id}: Cut stderr={cut.stderr[-500:]}")

        if not os.path.exists(cut_video) or os.path.getsize(cut_video) == 0:
            # Fallback: re-encode
            print(f"Job {job_id}: Copy failed, trying re-encode...")
            cut2 = subprocess.run([
                "ffmpeg","-y",
                "-i", raw_video,
                "-ss", str(start),
                "-t", str(end-start),
                "-c:v","libx264","-preset","ultrafast","-crf","28",
                "-c:a","aac","-b:a","96k",
                cut_video
            ], capture_output=True, text=True, timeout=120)
            print(f"Job {job_id}: Re-encode returncode={cut2.returncode}")
            print(f"Job {job_id}: Re-encode stderr={cut2.stderr[-500:]}")

        if not os.path.exists(cut_video) or os.path.getsize(cut_video) == 0:
            raise Exception(f"Cut failed. ffmpeg stderr: {cut.stderr[-300:]}")

        cut_size = os.path.getsize(cut_video)
        print(f"Job {job_id}: Cut video size: {cut_size} bytes")
        update_job(job_id,"converting",65,"Converting to output formats...")

        output_files = {}

        if "vertical" in formats:
            vpath = os.path.join(job_dir,"vertical.mp4")
            # Scale down first to save RAM, then pad to 9:16
            vr = subprocess.run([
                "ffmpeg","-y","-i",cut_video,
                "-vf","scale=540:960:force_original_aspect_ratio=decrease,pad=540:960:(ow-iw)/2:(oh-ih)/2:black",
                "-c:v","libx264","-preset","ultrafast","-crf","28",
                "-c:a","aac","-b:a","96k","-r","30",
                "-threads","1",
                vpath
            ], capture_output=True, text=True, timeout=180)
            print(f"Job {job_id}: Vertical rc={vr.returncode} size={os.path.getsize(vpath) if os.path.exists(vpath) else 0}")
            if os.path.exists(vpath) and os.path.getsize(vpath) > 0:
                output_files["vertical"] = vpath

        if "horizontal" in formats:
            hpath = os.path.join(job_dir,"horizontal.mp4")
            hr = subprocess.run([
                "ffmpeg","-y","-i",cut_video,
                "-vf","scale=960:540:force_original_aspect_ratio=decrease,pad=960:540:(ow-iw)/2:(oh-ih)/2:black",
                "-c:v","libx264","-preset","ultrafast","-crf","28",
                "-c:a","aac","-b:a","96k","-r","30",
                "-threads","1",
                hpath
            ], capture_output=True, text=True, timeout=180)
            print(f"Job {job_id}: Horizontal rc={hr.returncode} size={os.path.getsize(hpath) if os.path.exists(hpath) else 0}")
            if os.path.exists(hpath) and os.path.getsize(hpath) > 0:
                output_files["horizontal"] = hpath

        if not output_files:
            raise Exception("All format conversions produced empty files. Check ffmpeg logs.")

        refs = {fmt:{"downloadUrl":f"/download/{job_id}/{fmt}",
            "sizeMb":round(os.path.getsize(p)/(1024*1024),1),
            "resolution":"1080x1920" if fmt=="vertical" else "1920x1080"}
            for fmt,p in output_files.items()}
        update_job(job_id,"done",100,"Clip ready!",files=refs)
        threading.Timer(3600, lambda: shutil.rmtree(job_dir,ignore_errors=True)).start()

    except Exception as e:
        print(f"Job {job_id} FAILED: {e}")
        update_job(job_id,"error",0,str(e))

def update_job(job_id, status, progress, message, files=None):
    JOBS[job_id].update({"status":status,"progress":progress,"message":message,"updatedAt":time.time()})
    if files is not None: JOBS[job_id]["files"] = files

@app.route("/job/<job_id>", methods=["GET"])
def get_job(job_id):
    job = JOBS.get(job_id)
    if not job: return jsonify({"error":"Not found"}), 404
    return jsonify(job)

@app.route("/download/<job_id>/<fmt>", methods=["GET"])
def download_clip(job_id, fmt):
    job = JOBS.get(job_id)
    if not job or job["status"]!="done": return jsonify({"error":"Not ready"}), 404
    filepath = os.path.join(CLIPS_DIR,job_id,f"{fmt}.mp4")
    if not os.path.exists(filepath): return jsonify({"error":"File not found"}), 404
    return send_file(filepath, as_attachment=True, download_name=f"vidpost_{fmt}_{job_id}.mp4")

@app.route("/transcript", methods=["POST","OPTIONS"])
def get_transcript():
    if request.method == "OPTIONS": return jsonify({}), 200
    data     = request.get_json()
    video_id = data.get("videoId","").strip()
    if not video_id: return jsonify({"error":"Missing videoId"}), 400
    if not SUPADATA: return jsonify({"error":"Supadata key not configured"}), 500
    title = "YouTube Video"
    try:
        r = requests.get(f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json", timeout=5)
        if r.ok: title = r.json().get("title", title)
    except: pass
    try:
        resp = requests.get("https://api.supadata.ai/v1/youtube/transcript",
            params={"videoId":video_id,"text":"true"},
            headers={"x-api-key":SUPADATA}, timeout=15)
        if resp.ok:
            d = resp.json(); c = d.get("content","")
            text = c if isinstance(c,str) else " ".join([s.get("text","") for s in c])
            if text and len(text)>30:
                text = re.sub(r'\[.*?\]','',text)
                text = re.sub(r'\s+',' ',text).strip()
                return jsonify({"transcript":text[:10000],"wordCount":len(text.split()),"title":title,"videoId":video_id})
    except Exception as e:
        print(f"Transcript error: {e}")
    return jsonify({"error":"Could not fetch transcript"}), 422

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
        "bold":"bold and provocative with punchy short sentences",
        "storytelling":"storytelling — open with a surprising moment then build to the insight"
    }
    tone_desc = tone_map.get(tone, tone_map["professional"])
    prompts = {
        "linkedin":f'Write a viral LinkedIn post.\nVIDEO: "{title}"\nTONE: {tone_desc}\nTRANSCRIPT: {transcript[:3000]}\n\n[Hook under 12 words]\n\n[emoji] Insight 1\n[emoji] Insight 2\n[emoji] Insight 3\n\n[Question for comments]\n\n#tag1 #tag2 #tag3\n\nMax 200 words. Output ONLY the post.',
        "twitter":f'Write a 6-tweet viral thread.\nVIDEO: "{title}"\nTONE: {tone_desc}\nTRANSCRIPT: {transcript[:3000]}\n\nTweet 1: Bold hook\nTweets 2-5: One insight each\nTweet 6: Ending + question\nNumber: 1/ 2/ etc.\nOutput ONLY 6 tweets, one per line.',
        "instagram":f'Write a viral Instagram caption.\nVIDEO: "{title}"\nTONE: {tone_desc}\nTRANSCRIPT: {transcript[:3000]}\n\nLine 1: Hook\n\n3 paragraphs with emoji\n\nQuestion\n\n15 hashtags\n\nOutput ONLY caption.',
        "tiktok":f'Write a 45-second TikTok script.\nVIDEO: "{title}"\nTONE: {tone_desc}\nTRANSCRIPT: {transcript[:3000]}\n\nHOOK\nPOINT 1\nPOINT 2\nPOINT 3\nCTA\n\nUnder 130 words. Output ONLY script.',
        "youtube_short":f'Write a YouTube Shorts script.\nVIDEO: "{title}"\nTONE: {tone_desc}\nTRANSCRIPT: {transcript[:3000]}\n\n[HOOK][POINT 1][POINT 2][POINT 3][CTA]\n\nUnder 150 words. Output ONLY script.'
    }
    results,errors = {},{}
    for platform in platforms:
        if platform not in prompts: continue
        try:
            resp = requests.post("https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization":f"Bearer {groq_key}","Content-Type":"application/json"},
                json={"model":"llama-3.3-70b-versatile","messages":[{"role":"user","content":prompts[platform]}],
                      "max_tokens":1024,"temperature":0.75},
                timeout=30)
            if resp.status_code==401: return jsonify({"error":"Invalid Groq API key"}), 401
            if resp.status_code==429: return jsonify({"error":"Rate limit. Try again."}), 429
            if resp.ok: results[platform]=resp.json()["choices"][0]["message"]["content"].strip()
            else: errors[platform]=f"Error {resp.status_code}"
        except Exception as e:
            errors[platform]=str(e)
    if not results: return jsonify({"error":"Generation failed","details":errors}), 500
    return jsonify({"results":results,"errors":errors,"title":title})

if __name__ == "__main__":
    port = int(os.environ.get("PORT",5000))
    print(f"VidPost AI v2 | port:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
