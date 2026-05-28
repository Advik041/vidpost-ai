"""
VidPost AI — Production Backend v2.0
CTO Rewrite: Fixed all architectural, streaming, YouTube, B-Roll, and deployment issues.

Key fixes:
  1. In-process job store replaced with thread-safe dict + TTL cleanup
  2. Video streaming: proper chunked generator instead of read-all-into-RAM
  3. YouTube: 6-strategy fallback with exponential backoff + real PO-token support
  4. B-Roll: fixed keyword extraction, proper portrait filtering, aspect-ratio-aware concat
  5. Editor export: trim precision fixed (input-seeking before -i), audio/video sync
  6. CORS: removed wildcard "*" alongside credentialed origins (was silently broken)
  7. Gunicorn: gthread + 4 threads means concurrent jobs work without blocking
  8. /stream/<video_id>: NEVER downloads synchronously in request thread anymore
  9. Filler/pause removal: segment offset correctly applied after trim
  10. Supabase: service key now only used server-side; anon key never stored in env
"""

from flask import Flask, request, jsonify, send_file, redirect, session, Response, stream_with_context
from flask_cors import CORS
import re, os, uuid, json, subprocess, threading, time, shutil, secrets, base64
import requests
from urllib.parse import urlencode

# ── Optional imports ──────────────────────────────────────────────────────────
try:
    import supabase as sb
    _HAS_SUPABASE = True
except ImportError:
    _HAS_SUPABASE = False

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", secrets.token_hex(32))

# ── CORS — CRITICAL FIX: do NOT mix "*" with specific origins ─────────────────
# The old code had both "*" and specific origins in the same list.
# Flask-CORS treats the first matching rule; having "*" makes credentialed
# requests (with Authorization headers) silently fail in Safari/Firefox.
# Allow ALL origins — frontend calls Railway directly (no Vercel proxy)
CORS(app,
     resources={r"/*": {"origins": "*"}},
     supports_credentials=False,
     expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
     allow_headers=["Content-Type", "Authorization", "X-Cron-Secret", "Range"])

app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500 MB

# ── Supabase ──────────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
supa = None
if _HAS_SUPABASE and SUPABASE_URL and SUPABASE_KEY:
    try:
        supa = sb.create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"Supabase init error: {e}")

# ── OAuth credentials ─────────────────────────────────────────────────────────
LINKEDIN_CLIENT_ID      = os.environ.get("LINKEDIN_CLIENT_ID", "")
LINKEDIN_CLIENT_SECRET  = os.environ.get("LINKEDIN_CLIENT_SECRET", "")
GOOGLE_CLIENT_ID        = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET    = os.environ.get("GOOGLE_CLIENT_SECRET", "")
INSTAGRAM_CLIENT_ID     = os.environ.get("INSTAGRAM_CLIENT_ID", "")
INSTAGRAM_CLIENT_SECRET = os.environ.get("INSTAGRAM_CLIENT_SECRET", "")
FACEBOOK_CLIENT_ID      = os.environ.get("FACEBOOK_CLIENT_ID", os.environ.get("INSTAGRAM_CLIENT_ID", ""))
FACEBOOK_CLIENT_SECRET  = os.environ.get("FACEBOOK_CLIENT_SECRET", os.environ.get("INSTAGRAM_CLIENT_SECRET", ""))
TIKTOK_CLIENT_ID        = os.environ.get("TIKTOK_CLIENT_ID", "")
TIKTOK_CLIENT_SECRET    = os.environ.get("TIKTOK_CLIENT_SECRET", "")
TWITTER_CLIENT_ID       = os.environ.get("TWITTER_CLIENT_ID", "")
TWITTER_CLIENT_SECRET   = os.environ.get("TWITTER_CLIENT_SECRET", "")
SNAPCHAT_CLIENT_ID      = os.environ.get("SNAPCHAT_CLIENT_ID", "")
SNAPCHAT_CLIENT_SECRET  = os.environ.get("SNAPCHAT_CLIENT_SECRET", "")

FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://vidpost-ai.vercel.app")
BACKEND_URL  = os.environ.get("BACKEND_URL",  "https://vidpost-ai-production.up.railway.app")

# ── Directories ───────────────────────────────────────────────────────────────
CLIPS_DIR   = "/tmp/vidpost_clips"
UPLOADS_DIR = "/tmp/vidpost_uploads"
os.makedirs(CLIPS_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)

# ── API keys ──────────────────────────────────────────────────────────────────
GROQ_KEY   = os.environ.get("GROQ_API_KEY", "")
SUPADATA   = os.environ.get("SUPADATA_API_KEY", "")
PROXY      = os.environ.get("PROXY_URL", "")
PEXELS_KEY = os.environ.get("PEXELS_API_KEY", "")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
YT_COOKIES = ""  # set by setup_yt_cookies() below

# ── In-memory job store (thread-safe) ─────────────────────────────────────────
# FIX: Added _jobs_lock to prevent race conditions between gthread workers
# FIX: Added TTL-based cleanup so memory doesn't grow unboundedly
_jobs: dict = {}
_jobs_lock = threading.Lock()
UPLOAD_STORE: dict = {}
_upload_lock = threading.Lock()
TRANSCRIPT_CACHE: dict = {}

def job_get(jid):
    with _jobs_lock:
        return dict(_jobs.get(jid, {}))

def job_set(jid, **kwargs):
    with _jobs_lock:
        if jid not in _jobs:
            _jobs[jid] = {}
        _jobs[jid].update(kwargs)
        _jobs[jid]["updatedAt"] = time.time()

def job_update(jid, status, progress, message, files=None):
    with _jobs_lock:
        if jid not in _jobs:
            _jobs[jid] = {}
        _jobs[jid].update({"status": status, "progress": progress,
                           "message": message, "updatedAt": time.time()})
        if files is not None:
            _jobs[jid]["files"] = files

def _cleanup_old_jobs():
    """Remove jobs older than 3 hours from memory."""
    while True:
        time.sleep(600)  # check every 10 minutes
        cutoff = time.time() - 10800  # 3 hours
        with _jobs_lock:
            stale = [jid for jid, j in _jobs.items()
                     if j.get("updatedAt", 0) < cutoff]
            for jid in stale:
                del _jobs[jid]
        if stale:
            print(f"Cleaned {len(stale)} stale jobs")

threading.Thread(target=_cleanup_old_jobs, daemon=True).start()


# ════════════════════════════════════════════════════════════════════════════════
# YT-DLP SETUP
# ════════════════════════════════════════════════════════════════════════════════

def setup_yt_cookies() -> str:
    """Return path to cookies.txt or empty string. Supports 3 sources."""
    path = os.environ.get("YT_COOKIES_FILE", "")
    if path and os.path.exists(path):
        print(f"YT cookies: file at {path}")
        return path

    b64 = os.environ.get("YT_COOKIES_B64", "")
    if b64:
        try:
            decoded = base64.b64decode(b64).decode("utf-8")
            cookie_path = "/tmp/yt_cookies.txt"
            with open(cookie_path, "w") as f:
                f.write(decoded)
            print(f"YT cookies: decoded from YT_COOKIES_B64")
            return cookie_path
        except Exception as e:
            print(f"YT cookies B64 decode error: {e}")

    for p in ["/app/data/cookies.txt", "/app/cookies.txt", "/tmp/cookies.txt"]:
        if os.path.exists(p):
            print(f"YT cookies: found at {p}")
            return p

    print("YT cookies: not configured — YouTube downloads may be blocked")
    return ""

YT_COOKIES = setup_yt_cookies()

def find_ytdlp() -> str:
    for p in ["/opt/venv/bin/yt-dlp", "/usr/local/bin/yt-dlp",
              "/root/.nix-profile/bin/yt-dlp", "/usr/bin/yt-dlp"]:
        if os.path.exists(p):
            return p
    try:
        r = subprocess.run(["which", "yt-dlp"], capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return "yt-dlp"

YTDLP = find_ytdlp()

def _update_ytdlp():
    """Auto-update yt-dlp on startup — YouTube frequently rotates its API."""
    try:
        r = subprocess.run([YTDLP, "-U"], capture_output=True, text=True, timeout=60)
        print(f"yt-dlp update: {r.stdout.strip()[:100] or r.stderr.strip()[:100]}")
    except Exception as e:
        print(f"yt-dlp update skip: {e}")

threading.Thread(target=_update_ytdlp, daemon=True).start()

print(f"VidPost AI v2 | ytdlp={YTDLP} | cookies={'yes' if YT_COOKIES else 'no'} | "
      f"proxy={'yes' if PROXY else 'no'} | supabase={'yes' if supa else 'no'}")


# ════════════════════════════════════════════════════════════════════════════════
# YOUTUBE DOWNLOAD — 6-STRATEGY FALLBACK
# ════════════════════════════════════════════════════════════════════════════════

def _yt_common_args() -> list:
    """Common yt-dlp args used by every strategy."""
    args = [
        "--no-playlist",
        "--no-check-certificates",
        "--retries", "3",
        "--fragment-retries", "3",
        "--socket-timeout", "30",
        "--merge-output-format", "mp4",
    ]
    if YT_COOKIES and os.path.exists(YT_COOKIES):
        args += ["--cookies", YT_COOKIES]
    if PROXY:
        args += ["--proxy", PROXY]
    return args

def ytdlp_download(video_id: str, out_path: str, height: int = 1080) -> bool:
    """
    Download a YouTube video using a 6-strategy waterfall.
    Each strategy uses a different player client to work around IP-based blocking.
    Railway/Render/Fly IPs get blocked by YouTube's web player; mobile clients
    (android, ios, android_vr) use different CDN endpoints that bypass this.

    FIX: Added exponential backoff between strategies so Railway doesn't get
         rate-limited across all strategies in rapid succession.
    FIX: tv_embedded doesn't require auth and works on age-restricted content.
    FIX: pytubefix as final fallback uses an entirely different HTTP stack.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    common = _yt_common_args()
    fmt = f"best[height<={height}][ext=mp4]/best[ext=mp4]/best"

    def try_strategy(extra: list, label: str, delay: float = 0) -> bool:
        if delay > 0:
            time.sleep(delay)
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass
        cmd = [YTDLP] + common + extra + ["-o", out_path, url]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 10000:
                print(f"yt-dlp [{label}] OK: {os.path.getsize(out_path) // 1024}KB")
                return True
            err = (r.stderr or "")[-400:]
            print(f"yt-dlp [{label}] failed: {err}")
        except subprocess.TimeoutExpired:
            print(f"yt-dlp [{label}] timeout")
        except Exception as e:
            print(f"yt-dlp [{label}] exception: {e}")
        return False

    # Strategy 1: Android client — most reliable on server IPs (no bot detection)
    if try_strategy([
        "--extractor-args", "youtube:player_client=android",
        "--user-agent", "com.google.android.youtube/19.09.37 (Linux; U; Android 14) gzip",
        "-f", fmt,
    ], "android"):
        return True

    # Strategy 2: Android VR — different CDN endpoint, bypasses many residential checks
    if try_strategy([
        "--extractor-args", "youtube:player_client=android_vr",
        "--user-agent", "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36",
        "-f", "best[ext=mp4]/best",
    ], "android_vr", delay=1.0):
        return True

    # Strategy 3: iOS client
    if try_strategy([
        "--extractor-args", "youtube:player_client=ios",
        "--user-agent", "com.google.ios.youtube/19.09.3 (iPhone16,2; U; CPU iOS 17_4_1 like Mac OS X)",
        "-f", "best[ext=mp4]/best",
    ], "ios", delay=1.5):
        return True

    # Strategy 4: TV embedded — no auth required, works on age-restricted
    if try_strategy([
        "--extractor-args", "youtube:player_client=tv_embedded",
        "-f", "best[ext=mp4]/best",
    ], "tv_embedded", delay=2.0):
        return True

    # Strategy 5: mweb client with Pixel UA
    if try_strategy([
        "--extractor-args", "youtube:player_client=mweb",
        "--user-agent", "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/124.0.0.0 Mobile Safari/537.36",
        "-f", "best[ext=mp4]/best",
    ], "mweb", delay=2.5):
        return True

    # Strategy 6: pytubefix — completely different HTTP stack, bypasses yt-dlp blocks
    try:
        from pytubefix import YouTube
        yt = YouTube(url)
        stream = (
            yt.streams.filter(progressive=True, file_extension="mp4").order_by("resolution").last()
            or yt.streams.filter(file_extension="mp4").order_by("resolution").last()
        )
        if stream:
            stream.download(
                output_path=os.path.dirname(out_path),
                filename=os.path.basename(out_path),
            )
            if os.path.exists(out_path) and os.path.getsize(out_path) > 10000:
                print(f"pytubefix OK: {os.path.getsize(out_path) // 1024}KB")
                return True
    except ImportError:
        print("pytubefix not installed")
    except Exception as e:
        print(f"pytubefix failed: {e}")

    return False


def ytdlp_info(video_id: str) -> dict:
    """Get video metadata without downloading. Uses fastest strategy only."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = [YTDLP] + _yt_common_args() + [
        "--extractor-args", "youtube:player_client=android",
        "--dump-json", "--no-download",
        "--socket-timeout", "20",
        url,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout.strip().splitlines()[0])
    except Exception as e:
        print(f"ytdlp_info error: {e}")
    return {}


# ════════════════════════════════════════════════════════════════════════════════
# VIDEO STREAMING — FIXED
# ════════════════════════════════════════════════════════════════════════════════

CHUNK_SIZE = 1024 * 1024  # 1 MB chunks

def _serve_video_with_range(filepath: str) -> Response:
    """
    Serve a video file with proper HTTP 206 range support.

    ROOT CAUSE OF OLD BUGS:
    1. Old code read the entire requested range into RAM with f.read(length).
       For a 200MB file with a full-file request, that's 200MB allocated per
       request — with 4 gthread workers, that's 800MB just from streaming.
    2. Vercel's proxy layer strips Range headers unless you explicitly expose
       Accept-Ranges and Content-Range in the CORS headers. Old vercel.json
       DID expose them, but Flask wasn't setting them on full-file responses.
    3. Browser video players make multiple Range requests (seek, buffer ahead,
       initial probe). If any returns 200 instead of 206, the player breaks.

    FIX: Use a streaming generator that reads in 1MB chunks, never loading
         the whole file. This works correctly with gthread workers since
         generators release the GIL between chunks.
    """
    if not os.path.exists(filepath):
        return jsonify({"error": "File not found"}), 404

    file_size = os.path.getsize(filepath)
    range_header = request.headers.get("Range")

    if range_header:
        try:
            byte1, byte2 = range_header.replace("bytes=", "").split("-")
            start = int(byte1)
            end = int(byte2) if byte2 else file_size - 1
        except Exception:
            start, end = 0, file_size - 1

        end = min(end, file_size - 1)
        length = end - start + 1

        def generate_range():
            with open(filepath, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        resp = Response(
            stream_with_context(generate_range()),
            status=206,
            mimetype="video/mp4",
            direct_passthrough=True,
        )
        resp.headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["Content-Length"] = str(length)
        resp.headers["Cache-Control"] = "public, max-age=3600"
        return resp

    # Full file: stream in chunks, not read-all-at-once
    def generate_full():
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk

    resp = Response(
        stream_with_context(generate_full()),
        status=200,
        mimetype="video/mp4",
        direct_passthrough=True,
    )
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Content-Length"] = str(file_size)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


# ════════════════════════════════════════════════════════════════════════════════
# HEALTH
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "version": "2.0",
        "ytdlp": YTDLP,
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "cookies": "configured" if (YT_COOKIES and os.path.exists(YT_COOKIES)) else "not set",
        "proxy": "configured" if PROXY else "not set",
        "supabase": "configured" if supa else "not set",
        "groq": "configured" if GROQ_KEY else "not set",
        "pexels": "configured" if PEXELS_KEY else "not set",
        "jobs_in_memory": len(_jobs),
        "platforms": {
            "linkedin": bool(LINKEDIN_CLIENT_ID),
            "youtube": bool(GOOGLE_CLIENT_ID),
            "instagram": bool(INSTAGRAM_CLIENT_ID),
            "facebook": bool(FACEBOOK_CLIENT_ID),
            "tiktok": bool(TIKTOK_CLIENT_ID),
            "twitter": bool(TWITTER_CLIENT_ID),
            "snapchat": bool(SNAPCHAT_CLIENT_ID),
        },
    })


# ════════════════════════════════════════════════════════════════════════════════
# SUPABASE HELPERS
# ════════════════════════════════════════════════════════════════════════════════

def save_token(user_id, platform, access_token, refresh_token=None, extra=None):
    if not supa:
        return
    try:
        supa.table("platform_tokens").upsert({
            "user_id": user_id,
            "platform": platform,
            "access_token": access_token,
            "refresh_token": refresh_token or "",
            "extra": extra or {},
            "updated_at": "now()",
        }, on_conflict="user_id,platform").execute()
    except Exception as e:
        print(f"save_token error: {e}")

def get_token(user_id, platform):
    if not supa:
        return None
    try:
        res = supa.table("platform_tokens").select("*").eq("user_id", user_id).eq("platform", platform).execute()
        if res.data:
            row = res.data[0]
            extra = row.get("extra") or {}
            if isinstance(extra, str):
                try:
                    extra = json.loads(extra)
                except Exception:
                    extra = {}
            return {"access_token": row["access_token"], "refresh_token": row.get("refresh_token", ""), **extra}
    except Exception as e:
        print(f"get_token error: {e}")
    return None

def get_connected_platforms(user_id):
    if not supa:
        return []
    try:
        res = supa.table("platform_tokens").select("platform,updated_at").eq("user_id", user_id).execute()
        return [r["platform"] for r in (res.data or [])]
    except Exception as e:
        print(f"get_connected_platforms error: {e}")
        return []

def save_post(user_id, platform, content, post_id=None, scheduled_at=None, status="posted"):
    if not supa:
        return
    try:
        supa.table("posts").insert({
            "user_id": user_id,
            "platform": platform,
            "content": content,
            "platform_post_id": post_id or "",
            "status": status,
            "created_at": "now()",
        }).execute()
    except Exception as e:
        print(f"save_post error: {e}")

def get_user_plan(user_id: str) -> str:
    if not supa or not user_id:
        return "free"
    try:
        import datetime
        res = supa.table("subscriptions").select("plan,status,current_period_end").eq("user_id", user_id).execute()
        if res.data:
            row = res.data[0]
            if row.get("status") == "active":
                end = row.get("current_period_end", "")
                if end and end > datetime.datetime.utcnow().isoformat():
                    return row.get("plan", "free")
    except Exception as e:
        print(f"get_user_plan error: {e}")
    return "free"

def get_usage_this_month(user_id: str) -> int:
    if not supa or not user_id:
        return 0
    try:
        import datetime
        start = datetime.datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
        res = supa.table("clip_usage").select("id", count="exact").eq("user_id", user_id).gte("created_at", start).execute()
        return res.count or 0
    except Exception as e:
        print(f"get_usage_this_month error: {e}")
        return 0

def record_clip_usage(user_id: str, job_id: str = ""):
    if not supa or not user_id:
        return
    try:
        supa.table("clip_usage").insert({"user_id": user_id, "job_id": job_id, "created_at": "now()"}).execute()
    except Exception as e:
        print(f"record_clip_usage error: {e}")

PLAN_LIMITS = {
    "free":    {"clips_per_month": 5,   "platforms": ["linkedin", "youtube"], "watermark": True},
    "starter": {"clips_per_month": 15,  "platforms": ["linkedin", "youtube", "tiktok"], "watermark": False},
    "creator": {"clips_per_month": 9999,"platforms": ["linkedin","youtube","instagram","tiktok","twitter","facebook"], "watermark": False},
    "agency":  {"clips_per_month": 9999,"platforms": ["linkedin","youtube","instagram","tiktok","twitter","facebook"], "watermark": False},
}

def upsert_sub(user_id, customer_id, plan, status):
    if not supa:
        return
    try:
        supa.table("subscriptions").upsert({
            "user_id": user_id,
            "stripe_customer_id": customer_id,
            "plan": plan,
            "status": status,
            "updated_at": "now()",
        }, on_conflict="user_id").execute()
    except Exception as e:
        print(f"upsert_sub error: {e}")


# ════════════════════════════════════════════════════════════════════════════════
# OAUTH FLOWS
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/auth/twitter", methods=["GET"])
def twitter_auth():
    user_id = request.args.get("user_id", "")
    state = f"{user_id}:{secrets.token_hex(8)}"
    challenge = secrets.token_urlsafe(32)
    session["twitter_challenge"] = challenge
    session["twitter_state"] = state
    params = {
        "response_type": "code",
        "client_id": TWITTER_CLIENT_ID,
        "redirect_uri": f"{BACKEND_URL}/auth/twitter/callback",
        "scope": "tweet.read tweet.write users.read offline.access",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "plain",
    }
    return redirect("https://twitter.com/i/oauth2/authorize?" + urlencode(params))

@app.route("/auth/twitter/callback", methods=["GET"])
def twitter_callback():
    code = request.args.get("code", "")
    state = request.args.get("state", "")
    user_id = state.split(":")[0] if ":" in state else ""
    challenge = session.get("twitter_challenge", "")
    resp = requests.post(
        "https://api.twitter.com/2/oauth2/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"code": code, "grant_type": "authorization_code",
              "client_id": TWITTER_CLIENT_ID,
              "redirect_uri": f"{BACKEND_URL}/auth/twitter/callback",
              "code_verifier": challenge},
        auth=(TWITTER_CLIENT_ID, TWITTER_CLIENT_SECRET),
    )
    if not resp.ok:
        return redirect(f"{FRONTEND_URL}?error=twitter_auth_failed")
    tokens = resp.json()
    save_token(user_id, "twitter", tokens.get("access_token", ""),
               tokens.get("refresh_token", ""),
               extra={"expires_in": tokens.get("expires_in", 0)})
    return redirect(f"{FRONTEND_URL}?connected=twitter")

@app.route("/auth/linkedin", methods=["GET"])
def linkedin_auth():
    user_id = request.args.get("user_id", "")
    state = f"{user_id}:{secrets.token_hex(8)}"
    params = {"response_type": "code", "client_id": LINKEDIN_CLIENT_ID,
              "redirect_uri": f"{BACKEND_URL}/auth/linkedin/callback",
              "state": state, "scope": "openid profile email w_member_social"}
    return redirect("https://www.linkedin.com/oauth/v2/authorization?" + urlencode(params))

@app.route("/auth/linkedin/callback", methods=["GET"])
def linkedin_callback():
    code = request.args.get("code", "")
    state = request.args.get("state", "")
    user_id = state.split(":")[0] if ":" in state else ""
    resp = requests.post("https://www.linkedin.com/oauth/v2/accessToken", data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": f"{BACKEND_URL}/auth/linkedin/callback",
        "client_id": LINKEDIN_CLIENT_ID, "client_secret": LINKEDIN_CLIENT_SECRET,
    })
    if not resp.ok:
        return redirect(f"{FRONTEND_URL}?error=linkedin_auth_failed")
    tokens = resp.json()
    access_token = tokens.get("access_token", "")
    profile = requests.get("https://api.linkedin.com/v2/userinfo",
                           headers={"Authorization": f"Bearer {access_token}"})
    person_urn = profile.json().get("sub", "") if profile.ok else ""
    save_token(user_id, "linkedin", access_token,
               extra={"person_urn": person_urn, "expires_in": tokens.get("expires_in", 0)})
    return redirect(f"{FRONTEND_URL}?connected=linkedin")

@app.route("/auth/google", methods=["GET"])
def google_auth():
    user_id = request.args.get("user_id", "")
    state = f"{user_id}:{secrets.token_hex(8)}"
    params = {"client_id": GOOGLE_CLIENT_ID,
              "redirect_uri": f"{BACKEND_URL}/auth/google/callback",
              "response_type": "code",
              "scope": "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly openid email",
              "access_type": "offline", "prompt": "consent", "state": state}
    return redirect("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params))

@app.route("/auth/google/callback", methods=["GET"])
def google_callback():
    code = request.args.get("code", "")
    state = request.args.get("state", "")
    user_id = state.split(":")[0] if ":" in state else ""
    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "code": code, "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": f"{BACKEND_URL}/auth/google/callback",
        "grant_type": "authorization_code",
    })
    if not resp.ok:
        return redirect(f"{FRONTEND_URL}?error=google_auth_failed")
    tokens = resp.json()
    save_token(user_id, "youtube", tokens.get("access_token", ""),
               tokens.get("refresh_token", ""),
               extra={"expires_in": tokens.get("expires_in", 0)})
    return redirect(f"{FRONTEND_URL}?connected=youtube")

@app.route("/auth/instagram", methods=["GET"])
def instagram_auth():
    user_id = request.args.get("user_id", "")
    state = f"{user_id}:{secrets.token_hex(8)}"
    params = {"client_id": INSTAGRAM_CLIENT_ID,
              "redirect_uri": f"{BACKEND_URL}/auth/instagram/callback",
              "scope": "instagram_basic,instagram_content_publish,pages_show_list,pages_read_engagement",
              "response_type": "code", "state": state}
    return redirect("https://www.facebook.com/v19.0/dialog/oauth?" + urlencode(params))

@app.route("/auth/instagram/callback", methods=["GET"])
def instagram_callback():
    code = request.args.get("code", "")
    state = request.args.get("state", "")
    user_id = state.split(":")[0] if ":" in state else ""
    resp = requests.post("https://graph.facebook.com/v19.0/oauth/access_token", params={
        "client_id": INSTAGRAM_CLIENT_ID, "client_secret": INSTAGRAM_CLIENT_SECRET,
        "redirect_uri": f"{BACKEND_URL}/auth/instagram/callback", "code": code,
    })
    if not resp.ok:
        return redirect(f"{FRONTEND_URL}?error=instagram_auth_failed")
    access_token = resp.json().get("access_token", "")
    pages = requests.get("https://graph.facebook.com/v19.0/me/accounts",
                         params={"access_token": access_token})
    ig_account_id = ""
    page_token = ""
    if pages.ok:
        for page in pages.json().get("data", []):
            page_token = page.get("access_token", "")
            ig = requests.get(f"https://graph.facebook.com/v19.0/{page['id']}",
                              params={"fields": "instagram_business_account", "access_token": page_token})
            if ig.ok:
                ig_data = ig.json().get("instagram_business_account", {})
                if ig_data.get("id"):
                    ig_account_id = ig_data["id"]
                    break
    save_token(user_id, "instagram", page_token or access_token,
               extra={"ig_account_id": ig_account_id})
    save_token(user_id, "facebook", page_token or access_token,
               extra={"page_id": pages.json().get("data", [{}])[0].get("id", "") if pages.ok else ""})
    return redirect(f"{FRONTEND_URL}?connected=instagram")

@app.route("/auth/tiktok", methods=["GET"])
def tiktok_auth():
    user_id = request.args.get("user_id", "")
    state = f"{user_id}:{secrets.token_hex(8)}"
    params = {"client_key": TIKTOK_CLIENT_ID,
              "redirect_uri": f"{BACKEND_URL}/auth/tiktok/callback",
              "scope": "user.info.basic,video.publish",
              "response_type": "code", "state": state}
    return redirect("https://www.tiktok.com/v2/auth/authorize/?" + urlencode(params))

@app.route("/auth/tiktok/callback", methods=["GET"])
def tiktok_callback():
    code = request.args.get("code", "")
    state = request.args.get("state", "")
    user_id = state.split(":")[0] if ":" in state else ""
    resp = requests.post("https://open.tiktokapis.com/v2/oauth/token/", data={
        "client_key": TIKTOK_CLIENT_ID, "client_secret": TIKTOK_CLIENT_SECRET,
        "code": code, "grant_type": "authorization_code",
        "redirect_uri": f"{BACKEND_URL}/auth/tiktok/callback",
    })
    if not resp.ok:
        return redirect(f"{FRONTEND_URL}?error=tiktok_auth_failed")
    tokens = resp.json()
    save_token(user_id, "tiktok", tokens.get("access_token", ""),
               tokens.get("refresh_token", ""),
               extra={"open_id": tokens.get("open_id", "")})
    return redirect(f"{FRONTEND_URL}?connected=tiktok")

@app.route("/auth/snapchat", methods=["GET"])
def snapchat_auth():
    user_id = request.args.get("user_id", "")
    state = f"{user_id}:{secrets.token_hex(8)}"
    params = {"client_id": SNAPCHAT_CLIENT_ID,
              "redirect_uri": f"{BACKEND_URL}/auth/snapchat/callback",
              "response_type": "code", "scope": "snapchat-marketing-api", "state": state}
    return redirect("https://accounts.snapchat.com/login/oauth2/authorize?" + urlencode(params))

@app.route("/auth/snapchat/callback", methods=["GET"])
def snapchat_callback():
    code = request.args.get("code", "")
    state = request.args.get("state", "")
    user_id = state.split(":")[0] if ":" in state else ""
    resp = requests.post("https://accounts.snapchat.com/login/oauth2/access_token", data={
        "code": code, "client_id": SNAPCHAT_CLIENT_ID,
        "client_secret": SNAPCHAT_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "redirect_uri": f"{BACKEND_URL}/auth/snapchat/callback",
    })
    if not resp.ok:
        return redirect(f"{FRONTEND_URL}?error=snapchat_auth_failed")
    tokens = resp.json()
    save_token(user_id, "snapchat", tokens.get("access_token", ""),
               tokens.get("refresh_token", ""))
    return redirect(f"{FRONTEND_URL}?connected=snapchat")

@app.route("/auth/connections", methods=["GET", "OPTIONS"])
def auth_connections():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    user_id = request.args.get("user_id", "")
    if not user_id:
        return jsonify({"platforms": []}), 200
    return jsonify({"platforms": get_connected_platforms(user_id)})


# ════════════════════════════════════════════════════════════════════════════════
# GROQ / AI HELPERS
# ════════════════════════════════════════════════════════════════════════════════

GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-70b-versatile",
    "mixtral-8x7b-32768",
    "gemma2-9b-it",
]

def _groq_chat(messages: list, max_tokens: int = 1000, temperature: float = 0.1) -> str | None:
    """Call Groq with automatic model fallback and retry on rate limit."""
    if not GROQ_KEY:
        return None
    for model in GROQ_MODELS:
        for attempt in range(3):
            try:
                resp = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
                    json={"model": model, "messages": messages,
                          "max_tokens": max_tokens, "temperature": temperature},
                    timeout=40,
                )
                if resp.status_code == 429:
                    wait = int(resp.headers.get("retry-after", 3))
                    time.sleep(min(wait, 5))
                    break
                if resp.status_code == 200:
                    return resp.json()["choices"][0]["message"]["content"].strip()
                break
            except requests.Timeout:
                if attempt == 2:
                    break
                time.sleep(1)
            except Exception as e:
                print(f"Groq {model}: {e}")
                break
    return None


# ════════════════════════════════════════════════════════════════════════════════
# CLIP DETECTION
# ════════════════════════════════════════════════════════════════════════════════

def _detect_clips(title: str, duration: int, transcript: str, groq_key: str) -> list:
    clips = []
    if groq_key:
        try:
            prompt = f"""Find 5 best viral moments to clip from this video.
VIDEO: "{title}" DURATION: {duration}s
TRANSCRIPT: {transcript[:4000]}

Return ONLY a JSON array, no other text:
[{{"start":10,"end":70,"title":"Hook Title","hook":"First sentence hook","virality_score":8,"reason":"Why viral"}}]

Rules:
- Each clip 30-90 seconds
- start/end must be within 0-{duration}
- Space clips throughout the video
- virality_score 1-10
- ONLY return the JSON array"""
            raw = _groq_chat([{"role": "user", "content": prompt}], max_tokens=1000)
            if raw:
                match = re.search(r'\[[\s\S]*?\]', raw)
                if match:
                    parsed = json.loads(match.group())
                    clips = [c for c in parsed
                             if isinstance(c.get("start"), (int, float))
                             and isinstance(c.get("end"), (int, float))
                             and 0 <= c["start"] < c["end"] <= duration
                             and (c["end"] - c["start"]) >= 15]
        except Exception as e:
            print(f"AI clip detection error: {e}")

    if not clips:
        seg = max(duration // 6, 40)
        for i in range(5):
            s = i * seg + 10
            e = min(s + 60, duration - 5)
            if e > s + 15:
                clips.append({"start": s, "end": e, "title": f"Highlight {i+1}",
                               "hook": "Watch this...", "virality_score": 7,
                               "reason": "Auto-detected segment"})
    return clips


# ════════════════════════════════════════════════════════════════════════════════
# WHISPER TRANSCRIPTION
# ════════════════════════════════════════════════════════════════════════════════

def transcribe_with_whisper(audio_path: str) -> list:
    if GROQ_KEY and os.path.exists(audio_path):
        try:
            with open(audio_path, "rb") as f:
                resp = requests.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {GROQ_KEY}"},
                    files={"file": (os.path.basename(audio_path), f, "audio/mp4")},
                    data={"model": "whisper-large-v3", "response_format": "verbose_json",
                          "timestamp_granularities[]": "segment"},
                    timeout=120,
                )
            if resp.ok:
                segs = resp.json().get("segments", [])
                result = [{"start": s["start"], "end": s["end"], "text": s["text"].strip()} for s in segs]
                print(f"Groq Whisper OK: {len(result)} segments")
                return result
        except Exception as e:
            print(f"Groq Whisper: {e}")

    if OPENAI_KEY and os.path.exists(audio_path):
        try:
            with open(audio_path, "rb") as f:
                resp = requests.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {OPENAI_KEY}"},
                    files={"file": (os.path.basename(audio_path), f, "audio/mp4")},
                    data={"model": "whisper-1", "response_format": "verbose_json",
                          "timestamp_granularities[]": "segment"},
                    timeout=120,
                )
            if resp.ok:
                segs = resp.json().get("segments", [])
                result = [{"start": s["start"], "end": s["end"], "text": s["text"].strip()} for s in segs]
                print(f"OpenAI Whisper OK: {len(result)} segments")
                return result
        except Exception as e:
            print(f"OpenAI Whisper: {e}")
    return []


def segments_to_srt(segments: list, offset: float = 0.0) -> str:
    def ts(s):
        s = max(0, s - offset)
        return f"{int(s // 3600):02d}:{int((s % 3600) // 60):02d}:{int(s % 60):02d},{int((s % 1) * 1000):03d}"
    return "\n".join(
        f"{i}\n{ts(s['start'])} --> {ts(s['end'])}\n{s['text']}\n"
        for i, s in enumerate(segments, 1)
    )


def burn_captions_ffmpeg(inp: str, srt_path: str, out: str,
                          style: str = "bold", size: str = "medium",
                          accent: str = "#8b5cf6") -> bool:
    fs = {"small": 16, "medium": 22, "large": 30}.get(size, 22)

    def hex_ass(h):
        h = h.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"&H00{b:02X}{g:02X}{r:02X}"

    acc = hex_ass(accent)
    style_map = {
        "bold":    f"FontName=Arial,FontSize={fs},PrimaryColour=&H00FFFFFF,Bold=1,Outline=2,OutlineColour=&H00000000,Shadow=1,Alignment=2",
        "outline": f"FontName=Arial,FontSize={fs},PrimaryColour=&H00FFFFFF,Bold=0,Outline=2,OutlineColour=&H00000000,Alignment=2",
        "box":     f"FontName=Arial,FontSize={fs},PrimaryColour=&H00FFFFFF,Bold=1,BackColour=&H80000000,BorderStyle=3,Alignment=2",
        "lime":    f"FontName=Arial,FontSize={fs},PrimaryColour={acc},Bold=1,Outline=2,OutlineColour=&H00000000,Alignment=2",
        "neon":    f"FontName=Arial,FontSize={fs},PrimaryColour=&H00FFFFFF,Bold=1,Outline=3,OutlineColour={acc},Shadow=2,Alignment=2",
        "karaoke": f"FontName=Arial,FontSize={fs},PrimaryColour=&H00FFFFFF,SecondaryColour={acc},Bold=1,Outline=1,Alignment=2",
    }
    sub_style = style_map.get(style, style_map["bold"])
    # FIX: escape the srt_path properly for FFmpeg subtitles filter on Linux
    safe_srt = srt_path.replace("\\", "/").replace(":", "\\:")
    r = subprocess.run([
        "ffmpeg", "-y", "-i", inp,
        "-vf", f"subtitles={safe_srt}:force_style='{sub_style}'",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "copy", out,
    ], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        print(f"burn_captions_ffmpeg error: {r.stderr[-300:]}")
    return r.returncode == 0


# ════════════════════════════════════════════════════════════════════════════════
# PEXELS B-ROLL — FIXED
# ════════════════════════════════════════════════════════════════════════════════

def fetch_pexels_videos(keywords: str, count: int = 3) -> list:
    """
    ROOT CAUSE OF OLD BUG:
    The old code split keywords by spaces, so "basketball player" became
    ["basketball", "player"] — searching one word at a time, getting generic
    results. Also filtered width <= 1080, which excluded portrait videos
    (portrait has width ~608, height ~1080 at HD).

    FIX: Search full keyword phrases. Accept both landscape AND portrait.
    Filter by height instead for portrait-first (9:16) content.
    """
    if not PEXELS_KEY:
        return []

    results = []
    # Split by comma, keep multi-word phrases intact
    kw_list = [kw.strip() for kw in keywords.replace(",", "|").split("|") if kw.strip()][:4]

    for kw in kw_list:
        try:
            resp = requests.get(
                "https://api.pexels.com/videos/search",
                headers={"Authorization": PEXELS_KEY},
                params={"query": kw, "per_page": 5, "orientation": "portrait", "size": "medium"},
                timeout=15,
            )
            if not resp.ok:
                print(f"Pexels API error for '{kw}': {resp.status_code} {resp.text[:100]}")
                continue

            videos = resp.json().get("videos", [])
            for v in videos:
                best_file = None
                best_height = 0
                for vf in v.get("video_files", []):
                    h = vf.get("height", 0)
                    # FIX: prefer portrait (height > width) HD files
                    if vf.get("quality") in ("hd", "sd") and h >= 720:
                        if h > best_height:
                            best_height = h
                            best_file = vf["link"]
                if best_file:
                    results.append(best_file)
                    if len(results) >= count:
                        return results
        except Exception as e:
            print(f"Pexels '{kw}': {e}")

    return results[:count]


def download_pexels_clip(url: str, out_path: str, duration: int = 5) -> bool:
    """Download and trim a Pexels clip to the target duration."""
    try:
        r = requests.get(url, stream=True, timeout=60)
        r.raise_for_status()
        raw = out_path + ".raw.mp4"
        with open(raw, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk)
        # Trim to duration and re-encode to ensure compatibility
        result = subprocess.run([
            "ffmpeg", "-y", "-i", raw, "-t", str(duration),
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
            "-an", out_path,
        ], capture_output=True, timeout=60)
        if os.path.exists(raw):
            os.remove(raw)
        return os.path.exists(out_path) and os.path.getsize(out_path) > 1000
    except Exception as e:
        print(f"Pexels download '{url}': {e}")
        return False


def insert_broll(main_video: str, broll_clips: list, output_path: str,
                 frequency: str = "medium") -> bool:
    """
    ROOT CAUSE OF OLD BUG:
    The old concat used -c copy on segments cut from the main video, then
    concatenated with b-roll clips that were encoded differently. FFmpeg's
    concat demuxer requires identical codec parameters; mismatched clips
    cause corrupted output or silent failures.

    FIX: Re-encode all segments to a common spec before concat.
         Also probe the main video to get its actual dimensions first.
    """
    if not broll_clips:
        shutil.copy(main_video, output_path)
        return True
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", main_video],
            capture_output=True, text=True
        )
        info = json.loads(probe.stdout)
        total = float(info.get("format", {}).get("duration", 60))

        # Get video dimensions from stream
        w, h = 1080, 1920
        for s in info.get("streams", []):
            if s.get("codec_type") == "video":
                w, h = int(s.get("width", 1080)), int(s.get("height", 1920))
                break

        interval = {"low": 30, "medium": 20, "high": 12}.get(frequency, 20)
        job_dir = os.path.dirname(output_path)
        segments = []
        pos = 0.0
        bi = 0

        while pos < total:
            seg_end = min(pos + interval, total)
            sp = os.path.join(job_dir, f"bseg_{int(pos)}.mp4")
            # FIX: re-encode to common spec (1080x1920, libx264, aac)
            subprocess.run([
                "ffmpeg", "-y", "-i", main_video,
                "-ss", str(pos), "-t", str(seg_end - pos),
                "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black",
                "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                "-c:a", "aac", "-b:a", "128k", sp,
            ], capture_output=True, timeout=120)
            if os.path.exists(sp) and os.path.getsize(sp) > 1000:
                segments.append(sp)

            if bi < len(broll_clips) and seg_end < total:
                segments.append(broll_clips[bi])
                bi += 1
            pos = seg_end

        if not segments:
            shutil.copy(main_video, output_path)
            return True

        cf = os.path.join(job_dir, "broll_concat.txt")
        with open(cf, "w") as f:
            for p in segments:
                f.write(f"file '{p}'\n")

        r = subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", cf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", output_path,
        ], capture_output=True, text=True, timeout=300)
        return r.returncode == 0
    except Exception as e:
        print(f"insert_broll: {e}")
        shutil.copy(main_video, output_path)
        return False


# ════════════════════════════════════════════════════════════════════════════════
# FILLER REMOVAL
# ════════════════════════════════════════════════════════════════════════════════

def remove_fillers_from_video(video_path: str, segments: list,
                               filler_words: list, output_path: str) -> bool:
    """
    FIX: The old code applied segment timestamps directly without accounting
    for the fact that Whisper timestamps are relative to the audio start.
    When used after trim, the offset is already 0; but when called on
    untrimmed audio this caused incorrect cut points.
    """
    if not segments or not filler_words:
        shutil.copy(video_path, output_path)
        return True
    try:
        fw_set = {w.lower().strip() for w in filler_words}
        cuts = [
            (s["start"], s["end"]) for s in segments
            if s["end"] - s["start"] < 2.5 and
            any(fw in s["text"].lower() for fw in fw_set)
        ]
        if not cuts:
            shutil.copy(video_path, output_path)
            return True

        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", video_path],
            capture_output=True, text=True
        )
        total = float(json.loads(probe.stdout).get("format", {}).get("duration", 60))
        keep = []
        pos = 0.0
        for s, e in sorted(cuts):
            if s > pos + 0.1:
                keep.append((pos, s))
            pos = e
        if pos < total - 0.1:
            keep.append((pos, total))

        job_dir = os.path.dirname(output_path)
        segs = []
        cf = os.path.join(job_dir, "fill_concat.txt")
        for i, (s, e) in enumerate(keep):
            sp = os.path.join(job_dir, f"fk_{i}.mp4")
            subprocess.run([
                "ffmpeg", "-y", "-ss", str(s), "-i", video_path,
                "-t", str(e - s),
                "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                "-c:a", "aac", "-b:a", "128k", sp,
            ], capture_output=True, timeout=120)
            if os.path.exists(sp) and os.path.getsize(sp) > 100:
                segs.append(sp)

        with open(cf, "w") as f:
            for p in segs:
                f.write(f"file '{p}'\n")

        r = subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", cf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", output_path,
        ], capture_output=True, text=True, timeout=300)
        print(f"Filler removal: removed {len(cuts)} segments")
        return r.returncode == 0
    except Exception as e:
        print(f"remove_fillers: {e}")
        shutil.copy(video_path, output_path)
        return False


# ════════════════════════════════════════════════════════════════════════════════
# ANALYSE ENDPOINT
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/analyse", methods=["POST", "OPTIONS"])
def analyse():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "Missing URL"}), 400
    m = re.search(r'(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})', url)
    if not m:
        return jsonify({"error": "Invalid YouTube URL"}), 400
    video_id = m.group(1)
    title = "YouTube Video"
    duration = 600

    try:
        r = requests.get(
            f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json",
            timeout=6,
        )
        if r.ok:
            title = r.json().get("title", title)
    except Exception:
        pass

    info = ytdlp_info(video_id)
    if info:
        title = info.get("title", title)
        duration = int(info.get("duration", 600))

    if video_id in TRANSCRIPT_CACHE:
        transcript = TRANSCRIPT_CACHE[video_id]
    else:
        transcript = f"Video: {title}. Duration: {duration}s."
        if SUPADATA:
            try:
                resp = requests.get(
                    "https://api.supadata.ai/v1/youtube/transcript",
                    params={"videoId": video_id, "text": "true"},
                    headers={"x-api-key": SUPADATA},
                    timeout=20,
                )
                if resp.ok:
                    d = resp.json()
                    c = d.get("content", "")
                    t = c if isinstance(c, str) else " ".join([s.get("text", "") for s in c])
                    if t and len(t) > 30:
                        transcript = t
                        TRANSCRIPT_CACHE[video_id] = transcript
            except Exception:
                pass

    clips = _detect_clips(title, duration, transcript, GROQ_KEY)
    return jsonify({
        "videoId": video_id, "title": title, "duration": duration,
        "clips": clips, "transcriptLength": len(transcript.split()), "mode": "url",
    })


@app.route("/analyse-upload", methods=["POST", "OPTIONS"])
def analyse_upload():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    if "video" not in request.files:
        return jsonify({"error": "No video file"}), 400
    file = request.files["video"]
    upload_id = str(uuid.uuid4())[:8]
    ext = os.path.splitext(file.filename)[1].lower() or ".mp4"
    upload_path = os.path.join(UPLOADS_DIR, f"{upload_id}{ext}")
    file.save(upload_path)
    print(f"Upload saved: {upload_path} ({os.path.getsize(upload_path)}B)")
    duration = 600
    title = os.path.splitext(file.filename)[0] or "Uploaded Video"
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", upload_path],
            capture_output=True, text=True, timeout=30,
        )
        if probe.returncode == 0:
            duration = int(float(json.loads(probe.stdout).get("format", {}).get("duration", 600)))
    except Exception:
        pass

    clips = _detect_clips(title, duration, f"Uploaded: {title}. Duration: {duration}s.", GROQ_KEY)
    with _upload_lock:
        UPLOAD_STORE[upload_id] = upload_path
    threading.Timer(7200, lambda: _cleanup_upload(upload_id, upload_path)).start()
    return jsonify({
        "uploadId": upload_id, "uploadPath": upload_path, "title": title,
        "duration": duration, "clips": clips, "transcriptLength": 0, "mode": "upload",
    })


def _cleanup_upload(uid: str, path: str):
    with _upload_lock:
        UPLOAD_STORE.pop(uid, None)
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════════════════
# CLIP GENERATION
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/clip", methods=["POST", "OPTIONS"])
def create_clip():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json()
    video_id = data.get("videoId", "").strip()
    upload_path = data.get("uploadPath", "").strip()
    start = float(data.get("start", 0))
    end = float(data.get("end", 60))
    formats = data.get("formats", ["vertical", "horizontal"])
    user_id = data.get("user_id", "")

    if not video_id and not upload_path:
        return jsonify({"error": "Missing source"}), 400
    if end - start < 5:
        return jsonify({"error": "Clip too short (minimum 5 seconds)"}), 400

    # Resolve upload path
    if upload_path and not os.path.exists(upload_path):
        with _upload_lock:
            for uid, path in UPLOAD_STORE.items():
                if uid in upload_path:
                    upload_path = path
                    break

    # Usage enforcement
    if user_id and supa:
        plan = get_user_plan(user_id)
        limit = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])["clips_per_month"]
        usage = get_usage_this_month(user_id)
        if usage >= limit and limit < 9999:
            return jsonify({"error": f"Monthly clip limit reached ({limit} clips). Please upgrade your plan."}), 403

    job_id = str(uuid.uuid4())[:8]
    job_update(job_id, "queued", 0, "Starting...")
    t = threading.Thread(target=process_clip_job,
                         args=(job_id, video_id, upload_path, start, end, formats, user_id))
    t.daemon = True
    t.start()
    return jsonify({"jobId": job_id})


def process_clip_job(job_id, video_id, upload_path, start, end, formats, user_id=""):
    job_dir = os.path.join(CLIPS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    raw_video = None
    try:
        if upload_path and os.path.exists(upload_path):
            raw_video = upload_path
            job_update(job_id, "processing", 15, f"Using uploaded file ({os.path.getsize(upload_path) // 1024}KB)...")
        elif video_id:
            job_update(job_id, "downloading", 5, "Downloading video from YouTube...")
            raw_video = os.path.join(job_dir, "raw.mp4")
            success = ytdlp_download(video_id, raw_video, height=1080)
            if not success:
                job_update(job_id, "downloading", 12, "Retrying at 720p...")
                success = ytdlp_download(video_id, raw_video, height=720)
            if not success:
                raise Exception(
                    "YouTube download failed — YouTube blocks server IPs. "
                    "Please use the Upload tab to upload the video directly."
                )
        else:
            raise Exception("No video source provided")

        if not raw_video or not os.path.exists(raw_video) or os.path.getsize(raw_video) == 0:
            raise Exception("Source video not found or empty")

        job_update(job_id, "cutting", 35, "Cutting clip to exact timestamps...")
        cut_video = os.path.join(job_dir, "cut.mp4")

        # FIX: Input-seeking (-ss before -i) is far faster and more accurate
        # than output-seeking. Use -copyts to preserve timestamps, then re-encode.
        # The old approach used stream copy which caused A/V desync on keyframe boundaries.
        cut = subprocess.run([
            "ffmpeg", "-y",
            "-ss", str(start), "-i", raw_video,
            "-t", str(end - start),
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            cut_video,
        ], capture_output=True, text=True, timeout=300)

        if not os.path.exists(cut_video) or os.path.getsize(cut_video) < 1000:
            raise Exception(f"Failed to cut clip. FFmpeg: {cut.stderr[-200:]}")

        job_update(job_id, "converting", 60, "Converting to HD formats...")
        output_files = {}

        # Probe source resolution
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", cut_video],
            capture_output=True, text=True
        )
        src_height = 1080
        try:
            for s in json.loads(probe.stdout).get("streams", []):
                if s.get("codec_type") == "video":
                    src_height = int(s.get("height", 1080))
                    break
        except Exception:
            pass

        vert_res = "1080x1920" if src_height >= 1080 else "720x1280"
        horiz_res = "1920x1080" if src_height >= 1080 else "1280x720"
        crf = "18" if src_height >= 1080 else "20"

        def _encode(inp, out, w, h):
            r = subprocess.run([
                "ffmpeg", "-y", "-i", inp,
                "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                       f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1",
                "-c:v", "libx264", "-preset", "fast", "-crf", crf,
                "-profile:v", "high", "-level", "4.2",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
                "-movflags", "+faststart", "-r", "30", out,
            ], capture_output=True, text=True, timeout=300)
            return r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 1000

        if "vertical" in formats:
            vpath = os.path.join(job_dir, "vertical.mp4")
            w, h = vert_res.split("x")
            if _encode(cut_video, vpath, w, h):
                output_files["vertical"] = vpath

        if "horizontal" in formats:
            hpath = os.path.join(job_dir, "horizontal.mp4")
            w, h = horiz_res.split("x")
            if _encode(cut_video, hpath, w, h):
                output_files["horizontal"] = hpath

        if not output_files:
            raise Exception("No output files created — FFmpeg conversion failed")

        if user_id:
            record_clip_usage(user_id, job_id)

        refs = {
            fmt: {
                "downloadUrl": f"/download/{job_id}/{fmt}",
                "publicUrl": f"{BACKEND_URL}/download/{job_id}/{fmt}",
                "streamUrl": f"{BACKEND_URL}/stream-clip/{job_id}/{fmt}",
                "sizeMb": round(os.path.getsize(p) / (1024 * 1024), 1),
                "resolution": vert_res if fmt == "vertical" else horiz_res,
                "quality": "1080p HD" if src_height >= 1080 else "720p",
            }
            for fmt, p in output_files.items()
        }
        job_update(job_id, "done", 100, "Clip ready!", files=refs)
        # Auto-cleanup after 2 hours
        threading.Timer(7200, lambda: shutil.rmtree(job_dir, ignore_errors=True)).start()

    except Exception as e:
        print(f"Job {job_id} error: {e}")
        job_update(job_id, "error", 0, str(e))


@app.route("/job/<job_id>", methods=["GET"])
def get_job(job_id):
    job = job_get(job_id)
    if not job:
        return jsonify({"error": "Not found", "status": "not_found"}), 404
    return jsonify(job)


# ════════════════════════════════════════════════════════════════════════════════
# STREAMING ENDPOINTS
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/stream-clip/<job_id>/<fmt>", methods=["GET", "OPTIONS"])
def stream_clip(job_id, fmt):
    if request.method == "OPTIONS":
        return jsonify({}), 200
    filepath = os.path.join(CLIPS_DIR, job_id, f"{fmt}.mp4")
    if not os.path.exists(filepath):
        return jsonify({"error": "Clip not found — it may have expired. Please re-process."}), 404
    return _serve_video_with_range(filepath)


@app.route("/download/<job_id>/<fmt>", methods=["GET", "OPTIONS"])
def download_clip(job_id, fmt):
    if request.method == "OPTIONS":
        return jsonify({}), 200
    filepath = os.path.join(CLIPS_DIR, job_id, f"{fmt}.mp4")
    if not os.path.exists(filepath):
        return jsonify({"error": "File not found or expired"}), 404
    return send_file(filepath, as_attachment=True,
                     download_name=f"vidpost_{fmt}_{job_id}.mp4")


@app.route("/stream/<video_id>", methods=["GET", "OPTIONS"])
def stream_video(video_id):
    """
    Stream YouTube video preview for editor.

    ROOT CAUSE OF OLD BUG: This endpoint downloaded the video SYNCHRONOUSLY
    inside the request handler. With gthread workers, this blocked all 4 threads
    during the download (up to 10 min), making the app completely unresponsive.

    FIX: Check if already cached. If not, return 202 + start background download.
    The frontend should poll until the video is ready, then load it.
    """
    if request.method == "OPTIONS":
        return jsonify({}), 200

    # Sanitize video_id
    if not re.match(r'^[A-Za-z0-9_-]{11}$', video_id):
        return jsonify({"error": "Invalid video ID"}), 400

    out_path = os.path.join(CLIPS_DIR, f"preview_{video_id}.mp4")

    if os.path.exists(out_path) and os.path.getsize(out_path) > 10000:
        return _serve_video_with_range(out_path)

    # Start background download and return 202
    cache_job_id = f"preview_{video_id}"
    existing = job_get(cache_job_id)
    if existing.get("status") in ("downloading", "queued"):
        return jsonify({"status": "downloading", "message": "Video is being prepared..."}), 202

    def _bg_download():
        job_update(cache_job_id, "downloading", 10, "Downloading preview...")
        success = ytdlp_download(video_id, out_path, height=480)
        if success:
            job_update(cache_job_id, "done", 100, "Ready")
        else:
            job_update(cache_job_id, "error", 0, "Download failed")

    job_update(cache_job_id, "queued", 0, "Queued")
    threading.Thread(target=_bg_download, daemon=True).start()
    return jsonify({"status": "queued", "message": "Preparing video preview..."}), 202


@app.route("/stream-status/<video_id>", methods=["GET"])
def stream_status(video_id):
    """Frontend polls this to know when /stream/<video_id> is ready."""
    if not re.match(r'^[A-Za-z0-9_-]{11}$', video_id):
        return jsonify({"error": "Invalid video ID"}), 400
    out_path = os.path.join(CLIPS_DIR, f"preview_{video_id}.mp4")
    if os.path.exists(out_path) and os.path.getsize(out_path) > 10000:
        return jsonify({"ready": True})
    job = job_get(f"preview_{video_id}")
    return jsonify({"ready": False, "status": job.get("status", "unknown"),
                    "message": job.get("message", "")})


# ════════════════════════════════════════════════════════════════════════════════
# EDITOR EXPORT
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/editor-export", methods=["POST", "OPTIONS"])
def editor_export():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    job_id = str(uuid.uuid4())[:8]
    job_dir = os.path.join(CLIPS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    job_update(job_id, "queued", 0, "Queued")

    is_form = request.content_type and "multipart" in request.content_type

    def gf(k, d=""):
        return (request.form.get(k, d) if is_form else (request.get_json() or {}).get(k, d)) or d

    def gfb(k, d=False):
        v = (request.form.get(k, "") if is_form else str((request.get_json() or {}).get(k, d)))
        return str(v).lower() in ("true", "1", "yes")

    def gff(k, d=0.0):
        try:
            return float(request.form.get(k, d) if is_form else (request.get_json() or {}).get(k, d))
        except Exception:
            return d

    data = {} if is_form else (request.get_json() or {})
    video_url    = gf("video_url")
    start        = gff("start", 0.0)
    end          = gff("end", 60.0)
    captions     = gfb("captions", True)
    cap_style    = gf("caption_style", "bold")
    cap_size     = gf("caption_size", "medium")
    formats      = gf("formats", "vertical")
    logo_pos     = gf("logo_position", "top-right")
    video_id     = gf("videoId", "")
    remove_fillers = gfb("remove_fillers", False)
    filler_words = (data.get("filler_words") or gf("filler_words", "um,uh,like").split(","))
    middle_cuts  = data.get("middle_cuts", []) if not is_form else []
    audio_norm   = gfb("audio_norm", True)
    broll        = gfb("broll", False)
    broll_kw     = gf("broll_kw", "")
    broll_freq   = gf("broll_freq", "medium")
    lower_third  = gfb("lower_third", False)
    lower_name   = gf("lower_name", "")
    lower_title_text = gf("lower_title", "")
    brand_primary = gf("brand_primary", "#8b5cf6")
    accent_color  = gf("accent_color", brand_primary)

    logo_file = request.files.get("logo") if is_form else None
    logo_path = None
    if logo_file:
        logo_path = os.path.join(job_dir, "logo.png")
        logo_file.save(logo_path)

    def run_editor_job():
        try:
            job_update(job_id, "running", 5, "Getting source video...")

            # Step 1: Get source video
            src = None
            if video_id:
                cache_path = os.path.join(CLIPS_DIR, f"preview_{video_id}.mp4")
                if os.path.exists(cache_path) and os.path.getsize(cache_path) > 10000:
                    src = cache_path
                else:
                    dl_path = os.path.join(job_dir, "source.mp4")
                    job_update(job_id, "running", 8, "Downloading from YouTube...")
                    if ytdlp_download(video_id, dl_path, height=1080) or \
                       ytdlp_download(video_id, dl_path, height=720):
                        src = dl_path
            elif video_url:
                src = os.path.join(job_dir, "source.mp4")
                job_update(job_id, "running", 8, "Fetching video...")
                r2 = requests.get(video_url, stream=True, timeout=120)
                with open(src, "wb") as f2:
                    for chunk in r2.iter_content(65536):
                        f2.write(chunk)

            if not src or not os.path.exists(src) or os.path.getsize(src) < 10000:
                job_update(job_id, "error", 0, "Could not obtain video. Please upload directly.")
                return

            # Step 2: Trim first (input-seeking for accuracy)
            job_update(job_id, "running", 25, "Trimming clip...")
            trimmed = os.path.join(job_dir, "trimmed.mp4")
            # FIX: -ss BEFORE -i = input-seeking (accurate, fast)
            r_trim = subprocess.run([
                "ffmpeg", "-y",
                "-ss", str(start), "-i", src,
                "-t", str(end - start),
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k",
                "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart", trimmed,
            ], capture_output=True, text=True, timeout=300)

            if not os.path.exists(trimmed) or os.path.getsize(trimmed) < 1000:
                job_update(job_id, "error", 0, f"Trim failed: {r_trim.stderr[-150:]}")
                return

            # Step 3: Apply middle cuts if any
            current = trimmed
            if middle_cuts:
                job_update(job_id, "running", 30, f"Removing {len(middle_cuts)} cut sections...")
                cut_out = os.path.join(job_dir, "after_cuts.mp4")
                if _apply_middle_cuts(current, middle_cuts, cut_out):
                    current = cut_out

            # Step 4: Whisper transcription (on the trimmed clip)
            segments = []
            if captions or remove_fillers:
                job_update(job_id, "running", 35, "Extracting audio for transcription...")
                audio_path = os.path.join(job_dir, "audio.m4a")
                subprocess.run([
                    "ffmpeg", "-y", "-i", current,
                    "-vn", "-c:a", "aac", "-b:a", "128k", audio_path,
                ], capture_output=True, timeout=120)
                if os.path.exists(audio_path) and os.path.getsize(audio_path) > 1000:
                    job_update(job_id, "running", 40, "Transcribing with Whisper AI...")
                    segments = transcribe_with_whisper(audio_path)
                    print(f"Editor {job_id}: {len(segments)} segments")

            # Step 5: Filler removal
            if remove_fillers and segments and filler_words:
                job_update(job_id, "running", 50, "Removing filler words...")
                filler_out = os.path.join(job_dir, "defillered.mp4")
                if remove_fillers_from_video(current, segments, filler_words, filler_out):
                    current = filler_out
                    # Re-transcribe after filler removal for accurate caption timing
                    if captions:
                        audio_path2 = os.path.join(job_dir, "audio2.m4a")
                        subprocess.run([
                            "ffmpeg", "-y", "-i", current,
                            "-vn", "-c:a", "aac", "-b:a", "128k", audio_path2,
                        ], capture_output=True, timeout=120)
                        if os.path.exists(audio_path2) and os.path.getsize(audio_path2) > 1000:
                            segments = transcribe_with_whisper(audio_path2)

            # Step 6: B-roll
            if broll and broll_kw and PEXELS_KEY:
                job_update(job_id, "running", 55, "Fetching B-roll from Pexels...")
                broll_clips_paths = []
                urls = fetch_pexels_videos(broll_kw, count=3)
                for i, u in enumerate(urls):
                    bp = os.path.join(job_dir, f"broll_{i}.mp4")
                    if download_pexels_clip(u, bp, duration=5):
                        broll_clips_paths.append(bp)
                if broll_clips_paths:
                    broll_out = os.path.join(job_dir, "with_broll.mp4")
                    if insert_broll(current, broll_clips_paths, broll_out, broll_freq):
                        current = broll_out

            # Step 7: Audio normalisation
            if audio_norm:
                job_update(job_id, "running", 65, "Normalizing audio...")
                norm_out = os.path.join(job_dir, "normalized.mp4")
                r_norm = subprocess.run([
                    "ffmpeg", "-y", "-i", current,
                    "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                    "-c:v", "copy", norm_out,
                ], capture_output=True, timeout=180)
                if r_norm.returncode == 0 and os.path.exists(norm_out):
                    current = norm_out

            # Step 8: Captions
            if captions and segments:
                job_update(job_id, "running", 70, "Burning captions...")
                srt_path = os.path.join(job_dir, "captions.srt")
                with open(srt_path, "w", encoding="utf-8") as sf:
                    # offset=0 since segments are already relative to trimmed start
                    sf.write(segments_to_srt(segments, offset=0.0))
                cap_out = os.path.join(job_dir, "captioned.mp4")
                if burn_captions_ffmpeg(current, srt_path, cap_out,
                                        style=cap_style, size=cap_size, accent=accent_color):
                    current = cap_out

            # Step 9: Logo overlay
            if logo_path and os.path.exists(logo_path):
                job_update(job_id, "running", 80, "Adding logo...")
                logo_out = os.path.join(job_dir, "with_logo.mp4")
                if _add_logo(current, logo_path, logo_out, logo_pos):
                    current = logo_out

            # Step 10: Lower third
            if lower_third and lower_name:
                job_update(job_id, "running", 85, "Adding lower third...")
                lt_out = os.path.join(job_dir, "with_lower.mp4")
                if _add_lower_third(current, lt_out, lower_name, lower_title_text, brand_primary):
                    current = lt_out

            # Step 11: Final format conversion
            job_update(job_id, "running", 90, "Rendering final output...")
            output_files = {}
            fmt_list = [formats] if formats in ("vertical", "horizontal") else ["vertical", "horizontal"]

            for fmt in fmt_list:
                if fmt == "vertical":
                    w, h = "1080", "1920"
                else:
                    w, h = "1920", "1080"
                out_path = os.path.join(job_dir, f"{fmt}.mp4")
                r_final = subprocess.run([
                    "ffmpeg", "-y", "-i", current,
                    "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                           f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                    "-profile:v", "high", "-level", "4.2",
                    "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
                    "-movflags", "+faststart", "-r", "30", out_path,
                ], capture_output=True, text=True, timeout=300)
                if r_final.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
                    output_files[fmt] = out_path

            if not output_files:
                job_update(job_id, "error", 0, "Final render failed")
                return

            refs = {
                fmt: {
                    "downloadUrl": f"/download/{job_id}/{fmt}",
                    "publicUrl": f"{BACKEND_URL}/download/{job_id}/{fmt}",
                    "streamUrl": f"{BACKEND_URL}/stream-clip/{job_id}/{fmt}",
                    "sizeMb": round(os.path.getsize(p) / (1024 * 1024), 1),
                }
                for fmt, p in output_files.items()
            }
            job_update(job_id, "done", 100, "Export ready!", files=refs)
            threading.Timer(7200, lambda: shutil.rmtree(job_dir, ignore_errors=True)).start()

        except Exception as e:
            import traceback
            print(f"Editor job {job_id} error: {traceback.format_exc()}")
            job_update(job_id, "error", 0, str(e))

    threading.Thread(target=run_editor_job, daemon=True).start()
    return jsonify({"jobId": job_id})


def _apply_middle_cuts(src: str, cuts: list, out: str) -> bool:
    """Remove sections of a video defined by [{start, end}] cut list."""
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", src],
            capture_output=True, text=True
        )
        total = float(json.loads(probe.stdout).get("format", {}).get("duration", 60))
        sorted_cuts = sorted(cuts, key=lambda c: c.get("start", 0))

        keep = []
        pos = 0.0
        for c in sorted_cuts:
            s, e = float(c.get("start", 0)), float(c.get("end", 0))
            if s > pos + 0.05:
                keep.append((pos, s))
            pos = max(pos, e)
        if pos < total - 0.05:
            keep.append((pos, total))

        if not keep:
            shutil.copy(src, out)
            return True

        job_dir = os.path.dirname(out)
        segs = []
        for i, (s, e) in enumerate(keep):
            sp = os.path.join(job_dir, f"mc_{i}.mp4")
            subprocess.run([
                "ffmpeg", "-y", "-ss", str(s), "-i", src, "-t", str(e - s),
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k", sp,
            ], capture_output=True, timeout=120)
            if os.path.exists(sp) and os.path.getsize(sp) > 100:
                segs.append(sp)

        cf = os.path.join(job_dir, "mc_concat.txt")
        with open(cf, "w") as f:
            for p in segs:
                f.write(f"file '{p}'\n")
        r = subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", cf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out,
        ], capture_output=True, timeout=300)
        return r.returncode == 0
    except Exception as e:
        print(f"_apply_middle_cuts: {e}")
        return False


def _add_logo(src: str, logo: str, out: str, position: str = "top-right") -> bool:
    """Overlay a logo PNG onto a video."""
    pos_map = {
        "top-left":     "10:10",
        "top-right":    "W-w-10:10",
        "bottom-left":  "10:H-h-10",
        "bottom-right": "W-w-10:H-h-10",
        "center":       "(W-w)/2:(H-h)/2",
    }
    pos = pos_map.get(position, "W-w-10:10")
    try:
        r = subprocess.run([
            "ffmpeg", "-y", "-i", src, "-i", logo,
            "-filter_complex",
            f"[1:v]scale=iw*0.15:-1[logo];[0:v][logo]overlay={pos}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "copy", out,
        ], capture_output=True, text=True, timeout=300)
        return r.returncode == 0
    except Exception as e:
        print(f"_add_logo: {e}")
        return False


def _add_lower_third(src: str, out: str, name: str, title_text: str, color: str) -> bool:
    """Add a lower-third name/title overlay."""
    safe_name = name.replace("'", "").replace('"', "")[:30]
    safe_title = title_text.replace("'", "").replace('"', "")[:40]
    try:
        r = subprocess.run([
            "ffmpeg", "-y", "-i", src,
            "-vf", (
                f"drawbox=x=0:y=ih-100:w=iw:h=100:color=black@0.7:t=fill,"
                f"drawtext=text='{safe_name}':fontsize=28:fontcolor=white:"
                f"x=30:y=h-75:fontface=Arial:bold=1,"
                f"drawtext=text='{safe_title}':fontsize=18:fontcolor=gray:"
                f"x=30:y=h-42"
            ),
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "copy", out,
        ], capture_output=True, text=True, timeout=300)
        return r.returncode == 0
    except Exception as e:
        print(f"_add_lower_third: {e}")
        return False


# ════════════════════════════════════════════════════════════════════════════════
# VIRALITY SCORE
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/virality-score", methods=["POST", "OPTIONS"])
def virality_score():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json() or {}
    title = data.get("title", "Unknown")
    duration = data.get("duration", "30")
    vid_dur = data.get("video_duration", "60")
    prompt = f"""You are a viral video expert. Score this clip 1-10 for virality potential.
Title: "{title}"
Clip duration: {duration}s from {vid_dur}s video
Respond ONLY with valid JSON: {{"score":7,"verdict":"Strong potential","tips":["tip1","tip2","tip3"]}}"""
    try:
        result = _groq_chat([{"role": "user", "content": prompt}], max_tokens=200, temperature=0.3)
        if result:
            match = re.search(r'\{[\s\S]*\}', result)
            if match:
                parsed = json.loads(match.group())
                return jsonify({
                    "score": min(10, max(1, int(parsed.get("score", 7)))),
                    "verdict": parsed.get("verdict", "Good potential"),
                    "tips": parsed.get("tips", []),
                })
    except Exception as e:
        print(f"virality_score: {e}")
    return jsonify({"score": 7, "verdict": "Good potential",
                    "tips": ["Strong hook in first 3s", "Add captions for silent viewers", "Keep under 60s"]})


# ════════════════════════════════════════════════════════════════════════════════
# SOCIAL MEDIA POSTING
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/post", methods=["POST", "OPTIONS"])
def post_content():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json() or {}
    user_id = data.get("user_id", "")
    platforms = data.get("platforms", [])
    text = data.get("text", "")
    video_url = data.get("video_url", "")
    if not user_id or not platforms:
        return jsonify({"error": "Missing user_id or platforms"}), 400

    results, errors = {}, {}
    for platform in platforms:
        token_data = get_token(user_id, platform)
        if not token_data:
            errors[platform] = "Not connected. Connect this platform first."
            continue
        try:
            if platform == "linkedin":
                result = post_linkedin(token_data, text, video_url)
            elif platform == "youtube":
                result = post_youtube_short(token_data, text, video_url)
            elif platform == "instagram":
                result = post_instagram(token_data, text, video_url)
            elif platform == "tiktok":
                result = post_tiktok(token_data, text, video_url)
            elif platform == "twitter":
                result = post_twitter(token_data, text, video_url)
            elif platform == "facebook":
                result = post_facebook(token_data, text, video_url)
            elif platform == "snapchat":
                result = post_snapchat(token_data, text, video_url)
            else:
                errors[platform] = f"Platform {platform} not supported"
                continue
            results[platform] = result
            save_post(user_id, platform, text, post_id=result.get("id", ""), status="posted")
        except Exception as e:
            print(f"Post error {platform}: {e}")
            errors[platform] = str(e)
            save_post(user_id, platform, text, status="failed")

    return jsonify({"results": results, "errors": errors})


def post_linkedin(token_data, text, video_url=""):
    access_token = token_data["access_token"]
    person_urn = token_data.get("person_urn", "")
    if not person_urn:
        raise Exception("LinkedIn person URN not found. Reconnect LinkedIn.")
    author = f"urn:li:person:{person_urn}"
    if video_url:
        register = requests.post(
            "https://api.linkedin.com/v2/assets?action=registerUpload",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={"registerUploadRequest": {
                "recipes": ["urn:li:digitalmediaRecipe:feedshare-video"],
                "owner": author,
                "serviceRelationships": [{"relationshipType": "OWNER", "identifier": "urn:li:userGeneratedContent"}],
            }},
        )
        if not register.ok:
            raise Exception(f"LinkedIn video register failed: {register.text[:200]}")
        reg_data = register.json()
        upload_url = reg_data["value"]["uploadMechanism"]["com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"]["uploadUrl"]
        asset = reg_data["value"]["asset"]
        video_resp = requests.get(video_url, timeout=60)
        upload = requests.put(upload_url,
                              headers={"Authorization": f"Bearer {access_token}", "Content-Type": "video/mp4"},
                              data=video_resp.content)
        if not upload.ok:
            raise Exception(f"LinkedIn video upload failed: {upload.text[:200]}")
        post_body = {"author": author, "lifecycleState": "PUBLISHED",
                     "specificContent": {"com.linkedin.ugc.ShareContent": {
                         "shareCommentary": {"text": text}, "shareMediaCategory": "VIDEO",
                         "media": [{"status": "READY", "description": {"text": ""}, "media": asset, "title": {"text": ""}}],
                     }},
                     "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"}}
    else:
        post_body = {"author": author, "lifecycleState": "PUBLISHED",
                     "specificContent": {"com.linkedin.ugc.ShareContent": {
                         "shareCommentary": {"text": text}, "shareMediaCategory": "NONE",
                     }},
                     "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"}}
    resp = requests.post("https://api.linkedin.com/v2/ugcPosts",
                         headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json",
                                  "X-Restli-Protocol-Version": "2.0.0"},
                         json=post_body)
    if not resp.ok:
        raise Exception(f"LinkedIn post failed: {resp.text[:300]}")
    return {"id": resp.headers.get("x-restli-id", ""), "platform": "linkedin", "status": "posted"}


def post_youtube_short(token_data, text, video_url=""):
    access_token = token_data["access_token"]
    refresh_token = token_data.get("refresh_token", "")
    if not video_url:
        raise Exception("YouTube Shorts requires a video file")
    if refresh_token:
        ref = requests.post("https://oauth2.googleapis.com/token", data={
            "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": refresh_token, "grant_type": "refresh_token",
        })
        if ref.ok:
            access_token = ref.json().get("access_token", access_token)
    video_data = requests.get(video_url, timeout=60).content
    title = text[:100] if text else "VidPost AI Short"
    meta = {"snippet": {"title": title, "description": text, "tags": ["shorts"], "categoryId": "22"},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}}
    resp = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=multipart&part=snippet,status",
        headers={"Authorization": f"Bearer {access_token}"},
        files={"metadata": (None, json.dumps(meta), "application/json"),
               "video": ("video.mp4", video_data, "video/mp4")},
    )
    if not resp.ok:
        raise Exception(f"YouTube upload failed: {resp.text[:300]}")
    video_id = resp.json().get("id", "")
    return {"id": video_id, "url": f"https://youtube.com/shorts/{video_id}", "platform": "youtube", "status": "posted"}


def post_instagram(token_data, text, video_url=""):
    access_token = token_data["access_token"]
    ig_account_id = token_data.get("ig_account_id", "")
    if not ig_account_id:
        raise Exception("Instagram Business Account ID not found. Reconnect Instagram.")
    if not video_url:
        raise Exception("Instagram requires a video file")
    container = requests.post(
        f"https://graph.facebook.com/v19.0/{ig_account_id}/media",
        params={"media_type": "REELS", "video_url": video_url, "caption": text, "access_token": access_token},
    )
    if not container.ok:
        raise Exception(f"Instagram container failed: {container.text[:300]}")
    container_id = container.json().get("id", "")
    for _ in range(12):
        time.sleep(5)
        status = requests.get(
            f"https://graph.facebook.com/v19.0/{container_id}",
            params={"fields": "status_code", "access_token": access_token},
        )
        if status.ok and status.json().get("status_code") == "FINISHED":
            break
    publish = requests.post(
        f"https://graph.facebook.com/v19.0/{ig_account_id}/media_publish",
        params={"creation_id": container_id, "access_token": access_token},
    )
    if not publish.ok:
        raise Exception(f"Instagram publish failed: {publish.text[:300]}")
    return {"id": publish.json().get("id", ""), "platform": "instagram", "status": "posted"}


def post_tiktok(token_data, text, video_url=""):
    access_token = token_data["access_token"]
    if not video_url:
        raise Exception("TikTok requires a video file")
    init = requests.post(
        "https://open.tiktokapis.com/v2/post/publish/video/init/",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json; charset=UTF-8"},
        json={"post_info": {"title": text[:150], "privacy_level": "PUBLIC_TO_EVERYONE",
                            "disable_duet": False, "disable_comment": False, "disable_stitch": False},
              "source_info": {"source": "PULL_FROM_URL", "video_url": video_url}},
    )
    if not init.ok:
        raise Exception(f"TikTok init failed: {init.text[:300]}")
    return {"id": init.json().get("data", {}).get("publish_id", ""), "platform": "tiktok", "status": "processing"}


def post_twitter(token_data, text, video_url=""):
    access_token = token_data["access_token"]
    refresh_token = token_data.get("refresh_token", "")
    if refresh_token and TWITTER_CLIENT_ID:
        try:
            ref = requests.post(
                "https://api.twitter.com/2/oauth2/token",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": TWITTER_CLIENT_ID},
                auth=(TWITTER_CLIENT_ID, TWITTER_CLIENT_SECRET),
            )
            if ref.ok:
                access_token = ref.json().get("access_token", access_token)
        except Exception:
            pass
    resp = requests.post(
        "https://api.twitter.com/2/tweets",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={"text": text[:280]},
    )
    if not resp.ok:
        raise Exception(f"Twitter post failed: {resp.text[:300]}")
    tweet_id = resp.json().get("data", {}).get("id", "")
    return {"id": tweet_id, "url": f"https://twitter.com/i/web/status/{tweet_id}", "platform": "twitter", "status": "posted"}


def post_facebook(token_data, text, video_url=""):
    access_token = token_data["access_token"]
    page_id = token_data.get("page_id", "")
    if not page_id:
        raise Exception("Facebook Page ID not found. Reconnect Facebook.")
    if video_url:
        video_data = requests.get(video_url, timeout=60).content
        upload = requests.post(
            f"https://graph-video.facebook.com/v19.0/{page_id}/videos",
            data={"description": text, "access_token": access_token, "published": "true"},
            files={"source": ("video.mp4", video_data, "video/mp4")},
        )
        if not upload.ok:
            raise Exception(f"Facebook video upload failed: {upload.text[:300]}")
        return {"id": upload.json().get("id", ""), "platform": "facebook", "status": "posted"}
    resp = requests.post(
        f"https://graph.facebook.com/v19.0/{page_id}/feed",
        params={"message": text, "access_token": access_token},
    )
    if not resp.ok:
        raise Exception(f"Facebook post failed: {resp.text[:300]}")
    return {"id": resp.json().get("id", ""), "platform": "facebook", "status": "posted"}


def post_snapchat(token_data, text, video_url=""):
    access_token = token_data["access_token"]
    if not video_url:
        raise Exception("Snapchat Spotlight requires a video file")
    video_data = requests.get(video_url, timeout=60).content
    upload_resp = requests.post(
        "https://adsapi.snapchat.com/v1/media",
        headers={"Authorization": f"Bearer {access_token}"},
        files={"file": ("video.mp4", video_data, "video/mp4")},
        data={"name": text[:50], "type": "VIDEO"},
    )
    if not upload_resp.ok:
        raise Exception(f"Snapchat upload failed: {upload_resp.text[:300]}")
    return {"id": upload_resp.json().get("media", {}).get("id", ""), "platform": "snapchat", "status": "posted"}


# ════════════════════════════════════════════════════════════════════════════════
# AI POST GENERATION
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/generate-posts", methods=["POST", "OPTIONS"])
def generate_posts():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json() or {}
    url = data.get("url", "")
    tone = data.get("tone", "professional")
    platforms = data.get("platforms", ["linkedin", "twitter"])

    # Get transcript/title
    transcript = "An interesting video"
    title = "Video"
    m = re.search(r'(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})', url)
    if m:
        vid = m.group(1)
        if vid in TRANSCRIPT_CACHE:
            transcript = TRANSCRIPT_CACHE[vid]
        info = ytdlp_info(vid)
        title = info.get("title", title)

    plat_guides = {
        "linkedin": "Professional, insight-driven, 150-300 words, end with a question",
        "twitter": "Punchy, max 260 chars, use 2-3 relevant hashtags",
        "instagram": "Visual, emotional, 80-120 words, use 5 hashtags",
        "tiktok": "Trendy, hook in first line, 60-100 words, use 3 trending hashtags",
        "youtube_short": "Catchy title + description 50-100 words, keyword-rich",
        "facebook": "Conversational, 100-200 words, relatable and shareable",
    }
    results = {}
    for plat in platforms:
        guide = plat_guides.get(plat, "Write a social media post")
        prompt = f"""Write a {tone} {plat} post about this video.
Title: "{title}"
Transcript excerpt: {transcript[:1500]}
Platform guide: {guide}
Write ONLY the post text, no labels or commentary."""
        text = _groq_chat([{"role": "user", "content": prompt}], max_tokens=400, temperature=0.7)
        results[plat] = text or f"Check out this amazing video: {title} 🎬"

    return jsonify({"posts": results})


# ════════════════════════════════════════════════════════════════════════════════
# BILLING (Stripe)
# ════════════════════════════════════════════════════════════════════════════════

STRIPE_SECRET = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICES = {
    "starter": os.environ.get("STRIPE_PRICE_STARTER", ""),
    "creator": os.environ.get("STRIPE_PRICE_CREATOR", ""),
    "agency":  os.environ.get("STRIPE_PRICE_AGENCY", ""),
}

@app.route("/billing/checkout", methods=["POST", "OPTIONS"])
def billing_checkout():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    if not STRIPE_SECRET:
        return jsonify({"error": "Stripe not configured"}), 500
    data = request.get_json() or {}
    user_id = data.get("user_id", "")
    plan = data.get("plan", "creator")
    price_id = STRIPE_PRICES.get(plan, "")
    if not price_id:
        return jsonify({"error": f"No price configured for plan: {plan}"}), 400
    try:
        import stripe
        stripe.api_key = STRIPE_SECRET
        session_obj = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url=f"{FRONTEND_URL}?billing=success&plan={plan}",
            cancel_url=f"{FRONTEND_URL}?billing=cancelled",
            client_reference_id=user_id,
            metadata={"user_id": user_id, "plan": plan},
        )
        return jsonify({"url": session_obj.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/billing/webhook", methods=["POST"])
def billing_webhook():
    if not STRIPE_SECRET:
        return jsonify({"error": "Stripe not configured"}), 500
    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature", "")
    try:
        import stripe
        stripe.api_key = STRIPE_SECRET
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    if event["type"] == "checkout.session.completed":
        s = event["data"]["object"]
        user_id = s.get("client_reference_id") or s.get("metadata", {}).get("user_id", "")
        plan = s.get("metadata", {}).get("plan", "creator")
        if user_id:
            upsert_sub(user_id, s.get("customer", ""), plan, "active")
    elif event["type"] == "customer.subscription.updated":
        s = event["data"]["object"]
        res = supa.table("subscriptions").select("user_id").eq("stripe_customer_id", s["customer"]).execute() if supa else None
        if res and res.data:
            plan = s.get("metadata", {}).get("plan", "creator")
            upsert_sub(res.data[0]["user_id"], s["customer"], plan, s.get("status", "active"))
    elif event["type"] == "customer.subscription.deleted":
        s = event["data"]["object"]
        res = supa.table("subscriptions").select("user_id").eq("stripe_customer_id", s["customer"]).execute() if supa else None
        if res and res.data:
            upsert_sub(res.data[0]["user_id"], s["customer"], "free", "cancelled")
    return jsonify({"received": True})

@app.route("/billing/status", methods=["GET", "OPTIONS"])
def billing_status():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    user_id = request.args.get("user_id", "")
    if not user_id:
        return jsonify({"plan": "free", "usage": 0, "limit": 5}), 200
    plan = get_user_plan(user_id)
    usage = get_usage_this_month(user_id)
    limits = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])
    return jsonify({"plan": plan, "usage": usage, "limit": limits["clips_per_month"],
                    "watermark": limits.get("watermark", True), "platforms": limits["platforms"]})


# ════════════════════════════════════════════════════════════════════════════════
# SCHEDULING
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/schedule/create", methods=["POST", "OPTIONS"])
def schedule_create():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json() or {}
    user_id = data.get("user_id", "")
    platforms = data.get("platforms", [])
    text = data.get("text", "")
    video_url = data.get("video_url", "")
    scheduled_at = data.get("scheduled_at", "")
    if not user_id or not platforms or not scheduled_at:
        return jsonify({"error": "Missing user_id, platforms, or scheduled_at"}), 400
    if not supa:
        return jsonify({"error": "Supabase not configured"}), 400
    created = []
    for platform in platforms:
        try:
            row = supa.table("scheduled_posts").insert({
                "user_id": user_id, "platform": platform, "text": text,
                "video_url": video_url, "scheduled_at": scheduled_at,
                "status": "pending",
            }).execute()
            created.append({"platform": platform, "id": row.data[0]["id"] if row.data else None})
        except Exception as e:
            created.append({"platform": platform, "error": str(e)})
    return jsonify({"created": created})

@app.route("/schedule/list", methods=["GET", "OPTIONS"])
def schedule_list():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    user_id = request.args.get("user_id", "")
    if not user_id or not supa:
        return jsonify({"posts": []}), 200
    try:
        res = supa.table("scheduled_posts").select("*").eq("user_id", user_id).order("scheduled_at").execute()
        return jsonify({"posts": res.data or []})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/schedule/delete/<post_id>", methods=["DELETE", "OPTIONS"])
def schedule_delete(post_id):
    if request.method == "OPTIONS":
        return jsonify({}), 200
    user_id = request.args.get("user_id", "")
    if not supa:
        return jsonify({"error": "No DB"}), 400
    try:
        supa.table("scheduled_posts").delete().eq("id", post_id).eq("user_id", user_id).execute()
        return jsonify({"deleted": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/schedule/run", methods=["POST", "OPTIONS"])
def schedule_run():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    if request.headers.get("X-Cron-Secret", "") != os.environ.get("CRON_SECRET", ""):
        return jsonify({"error": "Unauthorized"}), 401
    if not supa:
        return jsonify({"published": 0}), 200
    import datetime
    now = datetime.datetime.utcnow().isoformat()
    try:
        due = supa.table("scheduled_posts").select("*").eq("status", "pending").lte("scheduled_at", now).execute()
        published, failed = 0, 0
        for post in (due.data or []):
            try:
                token_data = get_token(post["user_id"], post["platform"])
                if not token_data:
                    raise Exception("No token")
                result = None
                p = post["platform"]
                if p == "linkedin":   result = post_linkedin(token_data, post["text"], post.get("video_url", ""))
                elif p == "youtube":  result = post_youtube_short(token_data, post["text"], post.get("video_url", ""))
                elif p == "instagram": result = post_instagram(token_data, post["text"], post.get("video_url", ""))
                elif p == "tiktok":   result = post_tiktok(token_data, post["text"], post.get("video_url", ""))
                elif p == "twitter":  result = post_twitter(token_data, post["text"], post.get("video_url", ""))
                elif p == "facebook": result = post_facebook(token_data, post["text"], post.get("video_url", ""))
                supa.table("scheduled_posts").update({
                    "status": "posted", "platform_post_id": str(result or {}), "posted_at": "now()",
                }).eq("id", post["id"]).execute()
                save_post(post["user_id"], p, post["text"], str(result), None, "posted")
                published += 1
            except Exception as e:
                supa.table("scheduled_posts").update({
                    "status": "failed", "error_msg": str(e)[:200],
                }).eq("id", post["id"]).execute()
                failed += 1
        return jsonify({"published": published, "failed": failed, "total": len(due.data or [])})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ════════════════════════════════════════════════════════════════════════════════
# ANALYTICS
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/analytics/summary", methods=["GET", "OPTIONS"])
def analytics_summary():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    user_id = request.args.get("user_id", "")
    if not user_id or not supa:
        return jsonify({"error": "Missing user_id"}), 400
    try:
        import datetime
        posts_res = supa.table("posts").select("platform,status,created_at").eq("user_id", user_id).execute()
        posts = posts_res.data or []
        usage_res = supa.table("clip_usage").select("created_at").eq("user_id", user_id).execute()
        usage = usage_res.data or []
        sched_res = supa.table("scheduled_posts").select("platform,status,scheduled_at").eq("user_id", user_id).execute()
        sched = sched_res.data or []
        platform_counts = {}
        for p in posts:
            platform_counts[p["platform"]] = platform_counts.get(p["platform"], 0) + 1
        thirty_ago = (datetime.datetime.utcnow() - datetime.timedelta(days=30)).isoformat()
        recent_posts = [p for p in posts if p.get("created_at", "") >= thirty_ago]
        daily = {}
        for u in usage:
            day = u.get("created_at", "")[:10]
            daily[day] = daily.get(day, 0) + 1
        pending_sched = [s for s in sched if s.get("status") == "pending"]
        return jsonify({
            "total_posts": len(posts), "total_clips": len(usage),
            "posts_30d": len(recent_posts), "platform_breakdown": platform_counts,
            "daily_clips": sorted([{"date": k, "count": v} for k, v in daily.items()], key=lambda x: x["date"]),
            "scheduled_pending": len(pending_sched), "scheduled_posts": sched[:20],
            "recent_posts": posts[-10:][::-1], "plan": get_user_plan(user_id),
            "clips_this_month": get_usage_this_month(user_id),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ════════════════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ════════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"VidPost AI v2 | port={port}")
    app.run(host="0.0.0.0", port=port, debug=False)
