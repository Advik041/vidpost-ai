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

print(f"yt-dlp: {YTDLP} | proxy: {'yes' if PROXY else 'no'} | supadata: {'yes' if SUPADATA else 'no'}")

def ytdlp_base():
    """Build base yt-dlp command with proxy injected directly."""
    cmd = [
        YTDLP,
        "--no-playlist",
        "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    ]
    if PROXY:
        cmd += ["--proxy", PROXY]
    return cmd


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "ytdlp": YTDLP,
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "proxy": "configured" if PROXY else "not set",
        "supadata": "configured" if SUPADATA else "not set"
    })


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

    # Title via oEmbed (no block)
    title = "YouTube Video"
    try:
        r = requests.get(f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json", timeout=5)
        if r.ok: title = r.json().get("title", title)
    except: pass

    # Duration via yt-dlp with proxy
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

    # Transcript via Supadata
    transcript = f"Video: {title}. Duration: {duration} seconds."
    if SUPADATA:
        try:
            resp = requests.get(
                "https://api.supadata.ai/v1/youtube/transcript",
                params={"videoId": video_id, "text": "true"},
                headers={"x-api-key": SUPADATA}, timeout=20
            )
            if resp.ok:
                d = resp.json()
                c = d.get("content","")
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

    duration = 600
    title    = os.path.splitext(file.filename)[0] or "Uploaded Video"
    try:
        probe = subprocess.run(["ffprobe","-v","quiet","-print_format","json","-show_format","-show_streams",upload_path], capture_output=True, text=True, timeout=30)
        if probe.returncode == 0:
            duration = int(float(json.loads(probe.stdout).get("format",{}).get("duration",600)))
    except: pass

    clips = _detect_clips(title, duration, f"Uploaded video: {title}. Duration: {duration}s.", groq_key)
    threading.Timer(7200, lambda: os.remove(upload_path) if os.path.exists(upload_path) else None).start()
    return jsonify({"uploadId":upload_id,"uploadPath":upload_path,"title":title,"duration":duration,"clips":clips,"transcriptLength":0,"mode":"upload"})


def _detect_clips(title, duration, transcript, groq_key):
    clips = []
    if groq_key:
        try:
            prompt = f"""Find the 5 best viral moments to clip from this video.
VIDEO: "{title}"
DURATION: {duration} seconds
TRANSCRIPT: {transcript[:3000]}

Return ONLY a valid JSON array, no other text:
[{{"start":10,"end":70,"title":"Hook Moment","hook":"Opening line","virality_score":8,"reason":"Why viral"}}]

Each clip: 30-90 seconds, within 0-{duration}, spaced throughout. ONLY return the JSON array."""

            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization":f"Bearer {groq_key}","Content-Type":"application/json"},
                json={"model":"llama-3.3-70b-versatile","messages":[{"role":"user","content":prompt}],"max_tokens":800,"temperature":0.1},
                timeout=30
            )
            if resp.ok:
                raw = resp.json()["choices"][0]["message"]["content"].strip()
                # Extract JSON array robustly
                match = re.search(r'\[[\s\S]*\]', raw)
                if match:
                    parsed = json.loads(match.group())
                    clips = [c for c in parsed if
                        isinstance(c.get("start"),(int,float)) and
                        isinstance(c.get("end"),(int,float)) and
                        0 <= c["start"] < c["end"] <= duration and
                        (c["end"]-c["start"]) >= 15]
        except Exception as e:
            print(f"AI clip error: {e}")

    if not clips:
        seg = max(duration//6, 40)
        for i in range(5):
            s=i*seg+10; e=min(s+60,duration-5)
            if e>s+15: clips.append({"start":s,"end":e,"title":f"Highlight {i+1}","hook":"Watch this...","virality_score":7,"reason":"Auto-detected"})
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
    if end-start < 10: return jsonify({"error":"Clip too short"}), 400

    job_id = str(uuid.uuid4())[:8]
    JOBS[job_id] = {"status":"queued","progress":0,"message":"Starting...","files":{}}
    t = threading.Thread(target=process_clip_job, args=(job_id,video_id,upload_path,start,end,formats))
    t.daemon = True; t.start()
    return jsonify({"jobId":job_id})


def process_clip_job(job_id, video_id, upload_path, start, end, formats):
    job_dir   = os.path.join(CLIPS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    raw_video = os.path.join(job_dir, "raw.mp4")

    try:
        if upload_path and os.path.exists(upload_path):
            update_job(job_id,"processing",30,"Using uploaded file...")
            raw_video = upload_path

        elif video_id:
            update_job(job_id,"downloading",10,f"Downloading via {'proxy' if PROXY else 'direct'}...")
            yt_url = f"https://www.youtube.com/watch?v={video_id}"

            cmd = ytdlp_base() + [
                "--format","bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[ext=mp4]/best",
                "--download-sections", f"*{max(0,start-3)}-{end+3}",
                "--force-keyframes-at-cuts",
                "--merge-output-format","mp4",
                "-o", raw_video,
                yt_url
            ]
            print(f"Download cmd: {' '.join(cmd[:6])}...")
            dl = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

            if dl.returncode != 0 or not os.path.exists(raw_video):
                print(f"Download stderr: {dl.stderr[:500]}")
                # Try lowest quality as fallback
                cmd2 = ytdlp_base() + [
                    "--format","worst[ext=mp4]/worst",
                    "--merge-output-format","mp4",
                    "-o", raw_video, yt_url
                ]
                dl2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=300)
                if dl2.returncode != 0 or not os.path.exists(raw_video):
                    proxy_status = "proxy active" if PROXY else "no proxy set"
                    raise Exception(f"Download failed ({proxy_status}). Try uploading the video file directly using the Upload tab.")
        else:
            raise Exception("No video source")

        update_job(job_id,"cutting",55,"Cutting clip...")
        cut_video = os.path.join(job_dir,"cut.mp4")
        cut = subprocess.run([
            "ffmpeg","-y","-ss",str(start),"-i",raw_video,
            "-t",str(end-start),"-c:v","libx264","-preset","fast","-crf","23",
            "-c:a","aac","-b:a","128k","-avoid_negative_ts","make_zero",cut_video
        ], capture_output=True, text=True, timeout=120)
        if not os.path.exists(cut_video): raise Exception(f"Cut failed: {cut.stderr[:200]}")

        update_job(job_id,"converting",75,"Converting formats...")
        output_files = {}

        if "vertical" in formats:
            vpath = os.path.join(job_dir,"vertical.mp4")
            subprocess.run(["ffmpeg","-y","-i",cut_video,
                "-vf","scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black",
                "-c:v","libx264","-preset","fast","-crf","23","-c:a","aac","-b:a","128k","-r","30",vpath
            ], capture_output=True, text=True, timeout=120)
            if os.path.exists(vpath): output_files["vertical"] = vpath

        if "horizontal" in formats:
            hpath = os.path.join(job_dir,"horizontal.mp4")
            subprocess.run(["ffmpeg","-y","-i",cut_video,
                "-vf","scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black",
                "-c:v","libx264","-preset","fast","-crf","23","-c:a","aac","-b:a","128k","-r","30",hpath
            ], capture_output=True, text=True, timeout=120)
            if os.path.exists(hpath): output_files["horizontal"] = hpath

        if not output_files: raise Exception("No output files created")

        refs = {fmt:{"downloadUrl":f"/download/{job_id}/{fmt}","sizeMb":round(os.path.getsize(p)/(1024*1024),1),"resolution":"1080x1920" if fmt=="vertical" else "1920x1080"} for fmt,p in output_files.items()}
        update_job(job_id,"done",100,"Clip ready!",files=refs)
        threading.Timer(3600, lambda: shutil.rmtree(job_dir,ignore_errors=True)).start()

    except Exception as e:
        print(f"Job {job_id} error: {e}")
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
    if not SUPADATA: return jsonify({"error":"Supadata key not configured on server"}), 500

    title = "YouTube Video"
    try:
        r = requests.get(f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json", timeout=5)
        if r.ok: title = r.json().get("title", title)
    except: pass

    try:
        resp = requests.get(
            "https://api.supadata.ai/v1/youtube/transcript",
            params={"videoId":video_id,"text":"true"},
            headers={"x-api-key":SUPADATA}, timeout=15
        )
        if resp.ok:
            d = resp.json()
            c = d.get("content","")
            text = c if isinstance(c,str) else " ".join([s.get("text","") for s in c])
            if text and len(text)>30:
                text = re.sub(r'\[.*?\]','',text)
                text = re.sub(r'\s+',' ',text).strip()
                return jsonify({"transcript":text[:10000],"wordCount":len(text.split()),"title":title,"videoId":video_id})
        print(f"Supadata response: {resp.status_code} {resp.text[:200]}")
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
        "linkedin":f'Write a viral LinkedIn post.\nVIDEO: "{title}"\nTONE: {tone_desc}\nTRANSCRIPT: {transcript[:3000]}\n\nFORMAT:\n[Punchy hook under 12 words]\n\n[emoji] Insight 1\n[emoji] Insight 2\n[emoji] Insight 3\n\n[Question for comments]\n\n#tag1 #tag2 #tag3\n\nMax 200 words. Output ONLY the post.',
        "twitter":f'Write a 6-tweet viral Twitter thread.\nVIDEO: "{title}"\nTONE: {tone_desc}\nTRANSCRIPT: {transcript[:3000]}\n\nTweet 1: Bold hook under 200 chars\nTweets 2-5: One insight each under 240 chars\nTweet 6: Ending + question\nNumber: 1/ 2/ etc. No hashtags.\nOutput ONLY 6 tweets, one per line.',
        "instagram":f'Write a viral Instagram caption.\nVIDEO: "{title}"\nTONE: {tone_desc}\nTRANSCRIPT: {transcript[:3000]}\n\nLine 1: Hook under 100 chars\n\n3 paragraphs with emoji\n\nQuestion for comments\n\n15 hashtags\n\nOutput ONLY the caption.',
        "tiktok":f'Write a 45-second TikTok script.\nVIDEO: "{title}"\nTONE: {tone_desc}\nTRANSCRIPT: {transcript[:3000]}\n\nHOOK: Bold claim\nPOINT 1: Short insight\nPOINT 2: Short insight\nPOINT 3: Short insight\nCTA: Follow or question\n\nUnder 130 words. Output ONLY script.',
        "youtube_short":f'Write a YouTube Shorts script.\nVIDEO: "{title}"\nTONE: {tone_desc}\nTRANSCRIPT: {transcript[:3000]}\n\n[HOOK][POINT 1][POINT 2][POINT 3][CTA]\n\nUnder 150 words. Output ONLY script.'
    }

    results,errors = {},{}
    for platform in platforms:
        if platform not in prompts: continue
        try:
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization":f"Bearer {groq_key}","Content-Type":"application/json"},
                json={"model":"llama-3.3-70b-versatile","messages":[{"role":"user","content":prompts[platform]}],"max_tokens":1024,"temperature":0.75},
                timeout=30
            )
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
    print(f"VidPost AI v2 | port:{port} | proxy:{'yes' if PROXY else 'no'} | supadata:{'yes' if SUPADATA else 'no'}")
    app.run(host="0.0.0.0", port=port, debug=False)
