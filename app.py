from flask import Flask, request, jsonify, send_file, redirect, session
from flask_cors import CORS
import re, os, uuid, json, subprocess, threading, time, shutil, secrets
import requests
from urllib.parse import urlencode
import supabase as sb

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", secrets.token_hex(32))
CORS(app, resources={r"/*": {"origins": os.environ.get("ALLOWED_ORIGIN", "*")}},
     supports_credentials=True)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024

# ── Supabase client ───────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
supa = sb.create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL else None

# ── Platform OAuth credentials (set in Railway Variables) ─────────────────────
LINKEDIN_CLIENT_ID     = os.environ.get("LINKEDIN_CLIENT_ID", "")
LINKEDIN_CLIENT_SECRET = os.environ.get("LINKEDIN_CLIENT_SECRET", "")
GOOGLE_CLIENT_ID       = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET   = os.environ.get("GOOGLE_CLIENT_SECRET", "")
INSTAGRAM_CLIENT_ID    = os.environ.get("INSTAGRAM_CLIENT_ID", "")
INSTAGRAM_CLIENT_SECRET= os.environ.get("INSTAGRAM_CLIENT_SECRET", "")
FACEBOOK_CLIENT_ID     = os.environ.get("FACEBOOK_CLIENT_ID", os.environ.get("INSTAGRAM_CLIENT_ID", ""))
FACEBOOK_CLIENT_SECRET = os.environ.get("FACEBOOK_CLIENT_SECRET", os.environ.get("INSTAGRAM_CLIENT_SECRET", ""))
TIKTOK_CLIENT_ID       = os.environ.get("TIKTOK_CLIENT_ID", "")
TIKTOK_CLIENT_SECRET   = os.environ.get("TIKTOK_CLIENT_SECRET", "")
TWITTER_CLIENT_ID      = os.environ.get("TWITTER_CLIENT_ID", "")
TWITTER_CLIENT_SECRET  = os.environ.get("TWITTER_CLIENT_SECRET", "")
SNAPCHAT_CLIENT_ID     = os.environ.get("SNAPCHAT_CLIENT_ID", "")
SNAPCHAT_CLIENT_SECRET = os.environ.get("SNAPCHAT_CLIENT_SECRET", "")

FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://vidpost-ai.vercel.app")
BACKEND_URL  = os.environ.get("BACKEND_URL",  "https://vidpost-ai-production.up.railway.app")

JOBS     = {}
CLIPS_DIR   = "/tmp/vidpost_clips"
UPLOADS_DIR = "/tmp/vidpost_uploads"
UPLOAD_STORE = {}
os.makedirs(CLIPS_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)

GROQ_KEY    = os.environ.get("GROQ_API_KEY", "")
SUPADATA    = os.environ.get("SUPADATA_API_KEY", "")
PROXY       = os.environ.get("PROXY_URL", "")
PEXELS_KEY  = os.environ.get("PEXELS_API_KEY", "")
OPENAI_KEY  = os.environ.get("OPENAI_API_KEY", "")   # for Whisper transcription

def find_ytdlp():
    for p in ["/usr/bin/yt-dlp","/usr/local/bin/yt-dlp","/root/.nix-profile/bin/yt-dlp"]:
        if os.path.exists(p): return p
    try:
        r = subprocess.run(["which","yt-dlp"], capture_output=True, text=True)
        if r.returncode == 0: return r.stdout.strip()
    except: pass
    return "yt-dlp"

YTDLP = find_ytdlp()
print(f"VidPost AI | yt-dlp:{YTDLP} | proxy:{'yes' if PROXY else 'no'} | supabase:{'yes' if supa else 'no'}")



# ── X / Twitter OAuth (OAuth 2.0 PKCE) ───────────────────────────────────────
@app.route("/auth/twitter", methods=["GET"])
def twitter_auth():
    user_id   = request.args.get("user_id","")
    state     = f"{user_id}:{secrets.token_hex(8)}"
    challenge = secrets.token_urlsafe(32)
    # Store challenge in session for verification
    session['twitter_challenge'] = challenge
    session['twitter_state']     = state
    params = {
        "response_type":         "code",
        "client_id":             TWITTER_CLIENT_ID,
        "redirect_uri":          f"{BACKEND_URL}/auth/twitter/callback",
        "scope":                 "tweet.read tweet.write users.read offline.access",
        "state":                 state,
        "code_challenge":        challenge,
        "code_challenge_method": "plain"
    }
    return redirect("https://twitter.com/i/oauth2/authorize?" + urlencode(params))

@app.route("/auth/twitter/callback", methods=["GET"])
def twitter_callback():
    code     = request.args.get("code","")
    state    = request.args.get("state","")
    user_id  = state.split(":")[0] if ":" in state else ""
    challenge= session.get('twitter_challenge','')

    resp = requests.post(
        "https://api.twitter.com/2/oauth2/token",
        headers={"Content-Type":"application/x-www-form-urlencoded"},
        data={
            "code":            code,
            "grant_type":      "authorization_code",
            "client_id":       TWITTER_CLIENT_ID,
            "redirect_uri":    f"{BACKEND_URL}/auth/twitter/callback",
            "code_verifier":   challenge
        },
        auth=(TWITTER_CLIENT_ID, TWITTER_CLIENT_SECRET)
    )
    if not resp.ok:
        print(f"Twitter token error: {resp.text}")
        return redirect(f"{FRONTEND_URL}?error=twitter_auth_failed")

    tokens = resp.json()
    save_token(user_id, "twitter",
               tokens.get("access_token",""),
               tokens.get("refresh_token",""),
               extra={"expires_in": tokens.get("expires_in",0)})
    return redirect(f"{FRONTEND_URL}?connected=twitter")


# ── Snapchat OAuth ────────────────────────────────────────────────────────────
@app.route("/auth/snapchat", methods=["GET"])
def snapchat_auth():
    user_id = request.args.get("user_id","")
    state   = f"{user_id}:{secrets.token_hex(8)}"
    params  = {
        "client_id":     SNAPCHAT_CLIENT_ID,
        "redirect_uri":  f"{BACKEND_URL}/auth/snapchat/callback",
        "response_type": "code",
        "scope":         "snapchat-marketing-api",
        "state":         state
    }
    return redirect("https://accounts.snapchat.com/login/oauth2/authorize?" + urlencode(params))

@app.route("/auth/snapchat/callback", methods=["GET"])
def snapchat_callback():
    code    = request.args.get("code","")
    state   = request.args.get("state","")
    user_id = state.split(":")[0] if ":" in state else ""

    resp = requests.post(
        "https://accounts.snapchat.com/login/oauth2/access_token",
        data={
            "code":          code,
            "client_id":     SNAPCHAT_CLIENT_ID,
            "client_secret": SNAPCHAT_CLIENT_SECRET,
            "grant_type":    "authorization_code",
            "redirect_uri":  f"{BACKEND_URL}/auth/snapchat/callback"
        }
    )
    if not resp.ok:
        print(f"Snapchat token error: {resp.text}")
        return redirect(f"{FRONTEND_URL}?error=snapchat_auth_failed")

    tokens = resp.json()
    save_token(user_id, "snapchat",
               tokens.get("access_token",""),
               tokens.get("refresh_token",""),
               extra={"expires_in": tokens.get("expires_in",0)})
    return redirect(f"{FRONTEND_URL}?connected=snapchat")

# ════════════════════════════════════════════════════════════════════════════════
# SUPABASE HELPERS
# ════════════════════════════════════════════════════════════════════════════════

def save_token(user_id, platform, access_token, refresh_token=None, extra=None):
    """Save/update OAuth token for a user+platform."""
    if not supa: return
    data = {
        "user_id": user_id,
        "platform": platform,
        "access_token": access_token,
        "refresh_token": refresh_token or "",
        "extra": json.dumps(extra or {}),
        "updated_at": "now()"
    }
    supa.table("platform_tokens").upsert(data, on_conflict="user_id,platform").execute()

def get_token(user_id, platform):
    """Get OAuth token for a user+platform."""
    if not supa: return None
    res = supa.table("platform_tokens").select("*").eq("user_id", user_id).eq("platform", platform).execute()
    if res.data:
        row = res.data[0]
        extra = json.loads(row.get("extra", "{}"))
        return {"access_token": row["access_token"], "refresh_token": row["refresh_token"], **extra}
    return None

def get_connected_platforms(user_id):
    """List all platforms a user has connected."""
    if not supa: return []
    res = supa.table("platform_tokens").select("platform,updated_at").eq("user_id", user_id).execute()
    return [r["platform"] for r in (res.data or [])]

def save_post(user_id, platform, content, post_id=None, scheduled_at=None, status="posted"):
    """Save a post record."""
    if not supa: return
    supa.table("posts").insert({
        "user_id": user_id,
        "platform": platform,
        "content": content,
        "platform_post_id": post_id or "",
        "scheduled_at": scheduled_at,
        "status": status,
        "created_at": "now()"
    }).execute()


# ════════════════════════════════════════════════════════════════════════════════
# HEALTH
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "ytdlp": YTDLP,
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "proxy": "configured" if PROXY else "not set",
        "supadata": "configured" if SUPADATA else "not set",
        "supabase": "configured" if supa else "not set",
        "platforms": {
            "linkedin":  bool(LINKEDIN_CLIENT_ID),
            "youtube":   bool(GOOGLE_CLIENT_ID),
            "instagram": bool(INSTAGRAM_CLIENT_ID),
            "facebook":  bool(FACEBOOK_CLIENT_ID),
            "tiktok":    bool(TIKTOK_CLIENT_ID),
            "twitter":   bool(TWITTER_CLIENT_ID),
            "snapchat":  bool(SNAPCHAT_CLIENT_ID)
        }
    })


# ════════════════════════════════════════════════════════════════════════════════
# OAUTH FLOWS
# ════════════════════════════════════════════════════════════════════════════════

# ── LinkedIn OAuth ─────────────────────────────────────────────────────────────
@app.route("/auth/linkedin", methods=["GET"])
def linkedin_auth():
    user_id = request.args.get("user_id","")
    state   = f"{user_id}:{secrets.token_hex(8)}"
    params  = {
        "response_type": "code",
        "client_id":     LINKEDIN_CLIENT_ID,
        "redirect_uri":  f"{BACKEND_URL}/auth/linkedin/callback",
        "state":         state,
        "scope":         "openid profile email w_member_social"
    }
    return redirect("https://www.linkedin.com/oauth/v2/authorization?" + urlencode(params))

@app.route("/auth/linkedin/callback", methods=["GET"])
def linkedin_callback():
    code    = request.args.get("code","")
    state   = request.args.get("state","")
    user_id = state.split(":")[0] if ":" in state else ""

    # Exchange code for token
    resp = requests.post("https://www.linkedin.com/oauth/v2/accessToken", data={
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  f"{BACKEND_URL}/auth/linkedin/callback",
        "client_id":     LINKEDIN_CLIENT_ID,
        "client_secret": LINKEDIN_CLIENT_SECRET
    })
    if not resp.ok:
        return redirect(f"{FRONTEND_URL}?error=linkedin_auth_failed")

    tokens = resp.json()
    access_token = tokens.get("access_token","")

    # Get LinkedIn profile (person URN needed for posting)
    profile = requests.get("https://api.linkedin.com/v2/userinfo",
        headers={"Authorization": f"Bearer {access_token}"})
    person_urn = ""
    if profile.ok:
        pdata = profile.json()
        person_urn = pdata.get("sub","")

    save_token(user_id, "linkedin", access_token,
               extra={"person_urn": person_urn, "expires_in": tokens.get("expires_in",0)})

    return redirect(f"{FRONTEND_URL}?connected=linkedin")


# ── Google / YouTube OAuth ─────────────────────────────────────────────────────
@app.route("/auth/google", methods=["GET"])
def google_auth():
    user_id = request.args.get("user_id","")
    state   = f"{user_id}:{secrets.token_hex(8)}"
    params  = {
        "client_id":     GOOGLE_CLIENT_ID,
        "redirect_uri":  f"{BACKEND_URL}/auth/google/callback",
        "response_type": "code",
        "scope":         "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly openid email",
        "access_type":   "offline",
        "prompt":        "consent",
        "state":         state
    }
    return redirect("https://accounts.google.com/o/oauth2/auth?" + urlencode(params))

@app.route("/auth/google/callback", methods=["GET"])
def google_callback():
    code    = request.args.get("code","")
    state   = request.args.get("state","")
    user_id = state.split(":")[0] if ":" in state else ""

    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "code":          code,
        "client_id":     GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri":  f"{BACKEND_URL}/auth/google/callback",
        "grant_type":    "authorization_code"
    })
    if not resp.ok:
        return redirect(f"{FRONTEND_URL}?error=google_auth_failed")

    tokens = resp.json()
    save_token(user_id, "youtube",
               tokens.get("access_token",""),
               tokens.get("refresh_token",""),
               extra={"expires_in": tokens.get("expires_in",0)})

    return redirect(f"{FRONTEND_URL}?connected=youtube")


# ── Instagram / Facebook OAuth ─────────────────────────────────────────────────
@app.route("/auth/instagram", methods=["GET"])
def instagram_auth():
    user_id = request.args.get("user_id","")
    state   = f"{user_id}:{secrets.token_hex(8)}"
    params  = {
        "client_id":     INSTAGRAM_CLIENT_ID,
        "redirect_uri":  f"{BACKEND_URL}/auth/instagram/callback",
        "scope":         "instagram_basic,instagram_content_publish,pages_read_engagement",
        "response_type": "code",
        "state":         state
    }
    return redirect("https://www.facebook.com/v19.0/dialog/oauth?" + urlencode(params))

@app.route("/auth/instagram/callback", methods=["GET"])
def instagram_callback():
    code    = request.args.get("code","")
    state   = request.args.get("state","")
    user_id = state.split(":")[0] if ":" in state else ""

    resp = requests.get("https://graph.facebook.com/v19.0/oauth/access_token", params={
        "client_id":     INSTAGRAM_CLIENT_ID,
        "client_secret": INSTAGRAM_CLIENT_SECRET,
        "redirect_uri":  f"{BACKEND_URL}/auth/instagram/callback",
        "code":          code
    })
    if not resp.ok:
        return redirect(f"{FRONTEND_URL}?error=instagram_auth_failed")

    tokens    = resp.json()
    page_token= tokens.get("access_token","")

    # Get Instagram Business Account ID
    ig_account_id = ""
    try:
        pages = requests.get("https://graph.facebook.com/v19.0/me/accounts",
            params={"access_token": page_token})
        if pages.ok and pages.json().get("data"):
            page_id = pages.json()["data"][0]["id"]
            page_access_token = pages.json()["data"][0]["access_token"]
            ig = requests.get(f"https://graph.facebook.com/v19.0/{page_id}",
                params={"fields":"instagram_business_account","access_token": page_access_token})
            if ig.ok:
                ig_account_id = ig.json().get("instagram_business_account",{}).get("id","")
    except: pass

    save_token(user_id, "instagram", page_token,
               extra={"ig_account_id": ig_account_id})

    return redirect(f"{FRONTEND_URL}?connected=instagram")


# ── Facebook Page OAuth (separate from Instagram) ──────────────────────────────
@app.route("/auth/facebook", methods=["GET"])
def facebook_auth():
    user_id = request.args.get("user_id","")
    state   = f"{user_id}:{secrets.token_hex(8)}"
    params  = {
        "client_id":     FACEBOOK_CLIENT_ID,
        "redirect_uri":  f"{BACKEND_URL}/auth/facebook/callback",
        "scope":         "pages_manage_posts,pages_read_engagement,publish_video",
        "response_type": "code",
        "state":         state
    }
    return redirect("https://www.facebook.com/v19.0/dialog/oauth?" + urlencode(params))

@app.route("/auth/facebook/callback", methods=["GET"])
def facebook_callback():
    code    = request.args.get("code","")
    state   = request.args.get("state","")
    user_id = state.split(":")[0] if ":" in state else ""

    resp = requests.get("https://graph.facebook.com/v19.0/oauth/access_token", params={
        "client_id":     FACEBOOK_CLIENT_ID,
        "client_secret": FACEBOOK_CLIENT_SECRET,
        "redirect_uri":  f"{BACKEND_URL}/auth/facebook/callback",
        "code":          code
    })
    if not resp.ok:
        return redirect(f"{FRONTEND_URL}?error=facebook_auth_failed")

    tokens     = resp.json()
    user_token = tokens.get("access_token","")

    # Get the first managed Page + its page-scoped token
    page_id    = ""
    page_token = user_token
    try:
        pages = requests.get("https://graph.facebook.com/v19.0/me/accounts",
            params={"access_token": user_token})
        if pages.ok and pages.json().get("data"):
            first_page = pages.json()["data"][0]
            page_id    = first_page["id"]
            page_token = first_page["access_token"]   # page-scoped (never expires)
    except: pass

    save_token(user_id, "facebook", page_token,
               extra={"page_id": page_id, "user_token": user_token})

    return redirect(f"{FRONTEND_URL}?connected=facebook")


# ── TikTok OAuth ───────────────────────────────────────────────────────────────
@app.route("/auth/tiktok", methods=["GET"])
def tiktok_auth():
    user_id    = request.args.get("user_id","")
    csrf_state = f"{user_id}:{secrets.token_hex(8)}"
    params = {
        "client_key":    TIKTOK_CLIENT_ID,
        "scope":         "user.info.basic,video.publish,video.upload",
        "response_type": "code",
        "redirect_uri":  f"{BACKEND_URL}/auth/tiktok/callback",
        "state":         csrf_state
    }
    return redirect("https://www.tiktok.com/v2/auth/authorize/?" + urlencode(params))

@app.route("/auth/tiktok/callback", methods=["GET"])
def tiktok_callback():
    code    = request.args.get("code","")
    state   = request.args.get("state","")
    user_id = state.split(":")[0] if ":" in state else ""

    resp = requests.post("https://open.tiktokapis.com/v2/oauth/token/", data={
        "client_key":    TIKTOK_CLIENT_ID,
        "client_secret": TIKTOK_CLIENT_SECRET,
        "code":          code,
        "grant_type":    "authorization_code",
        "redirect_uri":  f"{BACKEND_URL}/auth/tiktok/callback"
    })
    if not resp.ok:
        return redirect(f"{FRONTEND_URL}?error=tiktok_auth_failed")

    tokens = resp.json().get("data",{})
    save_token(user_id, "tiktok",
               tokens.get("access_token",""),
               tokens.get("refresh_token",""),
               extra={"open_id": tokens.get("open_id","")})

    return redirect(f"{FRONTEND_URL}?connected=tiktok")


# ── Get connected platforms for a user ─────────────────────────────────────────
@app.route("/connections", methods=["GET","OPTIONS"])
def connections():
    if request.method == "OPTIONS": return jsonify({}), 200
    user_id = request.args.get("user_id","")
    if not user_id: return jsonify({"error":"Missing user_id"}), 400
    platforms = get_connected_platforms(user_id)
    return jsonify({"connected": platforms})

@app.route("/disconnect", methods=["POST","OPTIONS"])
def disconnect():
    if request.method == "OPTIONS": return jsonify({}), 200
    data     = request.get_json()
    user_id  = data.get("user_id","")
    platform = data.get("platform","")
    if not supa: return jsonify({"error":"No database"}), 500
    supa.table("platform_tokens").delete().eq("user_id", user_id).eq("platform", platform).execute()
    return jsonify({"disconnected": platform})


# ════════════════════════════════════════════════════════════════════════════════
# AUTO-POSTING
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/post", methods=["POST","OPTIONS"])
def auto_post():
    """Post content to one or more platforms for a user."""
    if request.method == "OPTIONS": return jsonify({}), 200

    data      = request.get_json()
    user_id   = data.get("user_id","")
    platforms = data.get("platforms", [])
    text      = data.get("text","").strip()
    video_url = data.get("video_url","")  # public URL to video file

    if not user_id:   return jsonify({"error":"Missing user_id"}), 400
    if not platforms: return jsonify({"error":"No platforms selected"}), 400
    if not text:      return jsonify({"error":"Missing text content"}), 400

    results = {}
    errors  = {}

    for platform in platforms:
        token_data = get_token(user_id, platform)
        if not token_data:
            errors[platform] = "Not connected. User needs to authenticate first."
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
                errors[platform] = f"Platform {platform} not supported yet"
                continue

            results[platform] = result
            save_post(user_id, platform, text, post_id=result.get("id",""), status="posted")

        except Exception as e:
            print(f"Post error {platform}: {e}")
            errors[platform] = str(e)
            save_post(user_id, platform, text, status="failed")

    return jsonify({"results": results, "errors": errors})


# ── LinkedIn post ──────────────────────────────────────────────────────────────
def post_linkedin(token_data, text, video_url=""):
    access_token = token_data["access_token"]
    person_urn   = token_data.get("person_urn","")

    if not person_urn:
        raise Exception("LinkedIn person URN not found. Reconnect LinkedIn.")

    author = f"urn:li:person:{person_urn}"

    if video_url:
        # Video post — register upload first
        register = requests.post(
            "https://api.linkedin.com/v2/assets?action=registerUpload",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={
                "registerUploadRequest": {
                    "recipes": ["urn:li:digitalmediaRecipe:feedshare-video"],
                    "owner": author,
                    "serviceRelationships": [{"relationshipType":"OWNER","identifier":"urn:li:userGeneratedContent"}]
                }
            }
        )
        if not register.ok:
            raise Exception(f"LinkedIn video register failed: {register.text[:200]}")

        reg_data    = register.json()
        upload_url  = reg_data["value"]["uploadMechanism"]["com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"]["uploadUrl"]
        asset       = reg_data["value"]["asset"]

        # Upload video binary
        video_resp = requests.get(video_url, timeout=60)
        upload     = requests.put(upload_url,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "video/mp4"},
            data=video_resp.content)
        if not upload.ok:
            raise Exception(f"LinkedIn video upload failed: {upload.text[:200]}")

        post_body = {
            "author": author,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": text},
                    "shareMediaCategory": "VIDEO",
                    "media": [{"status":"READY","description":{"text":""},"media":asset,"title":{"text":""}}]
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"}
        }
    else:
        post_body = {
            "author": author,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": text},
                    "shareMediaCategory": "NONE"
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"}
        }

    resp = requests.post("https://api.linkedin.com/v2/ugcPosts",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json",
                 "X-Restli-Protocol-Version": "2.0.0"},
        json=post_body)

    if not resp.ok:
        raise Exception(f"LinkedIn post failed: {resp.text[:300]}")

    post_id = resp.headers.get("x-restli-id","")
    return {"id": post_id, "platform": "linkedin", "status": "posted"}


# ── YouTube Shorts upload ──────────────────────────────────────────────────────
def post_youtube_short(token_data, text, video_url=""):
    access_token  = token_data["access_token"]
    refresh_token = token_data.get("refresh_token","")

    if not video_url:
        raise Exception("YouTube Shorts requires a video file")

    # Refresh token if needed
    if refresh_token:
        ref = requests.post("https://oauth2.googleapis.com/token", data={
            "client_id":     GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type":    "refresh_token"
        })
        if ref.ok:
            access_token = ref.json().get("access_token", access_token)

    # Download video
    video_resp = requests.get(video_url, timeout=60)
    video_data = video_resp.content

    # Upload to YouTube
    title = text[:100] if text else "VidPost AI Short"
    meta  = {
        "snippet": {
            "title": title,
            "description": text,
            "tags": ["shorts"],
            "categoryId": "22"
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}
    }

    resp = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=multipart&part=snippet,status",
        headers={"Authorization": f"Bearer {access_token}"},
        files={
            "metadata": (None, json.dumps(meta), "application/json"),
            "video":    ("video.mp4", video_data, "video/mp4")
        }
    )
    if not resp.ok:
        raise Exception(f"YouTube upload failed: {resp.text[:300]}")

    video_id = resp.json().get("id","")
    return {"id": video_id, "url": f"https://youtube.com/shorts/{video_id}", "platform": "youtube", "status": "posted"}


# ── Instagram Reels post ───────────────────────────────────────────────────────
def post_instagram(token_data, text, video_url=""):
    access_token  = token_data["access_token"]
    ig_account_id = token_data.get("ig_account_id","")

    if not ig_account_id:
        raise Exception("Instagram Business Account ID not found. Reconnect Instagram.")
    if not video_url:
        raise Exception("Instagram requires a video file for Reels")

    # Step 1: Create media container
    container = requests.post(
        f"https://graph.facebook.com/v19.0/{ig_account_id}/media",
        params={
            "media_type":    "REELS",
            "video_url":     video_url,
            "caption":       text,
            "access_token":  access_token
        }
    )
    if not container.ok:
        raise Exception(f"Instagram container failed: {container.text[:300]}")

    container_id = container.json().get("id","")

    # Step 2: Wait for processing (poll status)
    for _ in range(12):
        time.sleep(5)
        status = requests.get(
            f"https://graph.facebook.com/v19.0/{container_id}",
            params={"fields":"status_code","access_token":access_token}
        )
        if status.ok and status.json().get("status_code") == "FINISHED":
            break

    # Step 3: Publish
    publish = requests.post(
        f"https://graph.facebook.com/v19.0/{ig_account_id}/media_publish",
        params={"creation_id": container_id, "access_token": access_token}
    )
    if not publish.ok:
        raise Exception(f"Instagram publish failed: {publish.text[:300]}")

    media_id = publish.json().get("id","")
    return {"id": media_id, "platform": "instagram", "status": "posted"}


# ── TikTok video post ──────────────────────────────────────────────────────────
def post_tiktok(token_data, text, video_url=""):
    access_token = token_data["access_token"]

    if not video_url:
        raise Exception("TikTok requires a video file")

    # Init upload
    init = requests.post(
        "https://open.tiktokapis.com/v2/post/publish/video/init/",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json; charset=UTF-8"},
        json={
            "post_info": {
                "title":          text[:150],
                "privacy_level":  "PUBLIC_TO_EVERYONE",
                "disable_duet":   False,
                "disable_comment":False,
                "disable_stitch": False
            },
            "source_info": {
                "source":    "PULL_FROM_URL",
                "video_url": video_url
            }
        }
    )
    if not init.ok:
        raise Exception(f"TikTok init failed: {init.text[:300]}")

    data      = init.json().get("data",{})
    publish_id= data.get("publish_id","")
    return {"id": publish_id, "platform": "tiktok", "status": "processing"}



# ── X / Twitter post ───────────────────────────────────────────────────────────
def post_twitter(token_data, text, video_url=""):
    access_token  = token_data["access_token"]
    refresh_token = token_data.get("refresh_token","")

    # Refresh token if expired
    if refresh_token and TWITTER_CLIENT_ID:
        try:
            ref = requests.post(
                "https://api.twitter.com/2/oauth2/token",
                headers={"Content-Type":"application/x-www-form-urlencoded"},
                data={"grant_type":"refresh_token","refresh_token":refresh_token,"client_id":TWITTER_CLIENT_ID},
                auth=(TWITTER_CLIENT_ID, TWITTER_CLIENT_SECRET)
            )
            if ref.ok:
                access_token = ref.json().get("access_token", access_token)
        except: pass

    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

    # Text tweet (first 280 chars)
    tweet_text = text[:280]
    resp = requests.post(
        "https://api.twitter.com/2/tweets",
        headers=headers,
        json={"text": tweet_text}
    )
    if not resp.ok:
        raise Exception(f"Twitter post failed: {resp.text[:300]}")

    tweet_id = resp.json().get("data",{}).get("id","")
    return {"id": tweet_id, "url": f"https://twitter.com/i/web/status/{tweet_id}", "platform": "twitter", "status": "posted"}


# ── Facebook Page post ────────────────────────────────────────────────────────
def post_facebook(token_data, text, video_url=""):
    access_token = token_data["access_token"]   # page-scoped token
    page_id      = token_data.get("page_id","")

    if not page_id:
        raise Exception("Facebook Page ID not found. Reconnect Facebook.")

    if video_url:
        # Download video first
        video_data = requests.get(video_url, timeout=60).content

        # Upload video to the page
        upload = requests.post(
            f"https://graph-video.facebook.com/v19.0/{page_id}/videos",
            data={
                "description":    text,
                "access_token":   access_token,
                "published":      "true"
            },
            files={"source": ("video.mp4", video_data, "video/mp4")}
        )
        if not upload.ok:
            raise Exception(f"Facebook video upload failed: {upload.text[:300]}")
        video_id = upload.json().get("id","")
        return {"id": video_id, "platform": "facebook", "status": "posted"}
    else:
        # Text post to page feed
        resp = requests.post(
            f"https://graph.facebook.com/v19.0/{page_id}/feed",
            params={
                "message":      text,
                "access_token": access_token
            }
        )
        if not resp.ok:
            raise Exception(f"Facebook post failed: {resp.text[:300]}")
        post_id = resp.json().get("id","")
        return {"id": post_id, "platform": "facebook", "status": "posted"}
def post_snapchat(token_data, text, video_url=""):
    access_token = token_data["access_token"]

    # Get Snapchat user profile first
    profile = requests.get(
        "https://kit.snapchat.com/v1/me",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    if not profile.ok:
        raise Exception(f"Snapchat profile fetch failed: {profile.text[:200]}")

    external_id = profile.json().get("data",{}).get("me",{}).get("externalId","")
    if not external_id:
        raise Exception("Could not get Snapchat user ID")

    if not video_url:
        raise Exception("Snapchat Spotlight requires a video file")

    # Upload to Snapchat Creative Library
    video_data = requests.get(video_url, timeout=60).content

    upload_resp = requests.post(
        "https://adsapi.snapchat.com/v1/media",
        headers={"Authorization": f"Bearer {access_token}"},
        files={"file": ("video.mp4", video_data, "video/mp4")},
        data={"name": text[:50], "type": "VIDEO"}
    )
    if not upload_resp.ok:
        raise Exception(f"Snapchat upload failed: {upload_resp.text[:300]}")

    media_id = upload_resp.json().get("media",{}).get("id","")
    return {"id": media_id, "platform": "snapchat", "status": "posted"}

# ════════════════════════════════════════════════════════════════════════════════
# EXISTING ENDPOINTS (clip generation etc — kept from before)
# ════════════════════════════════════════════════════════════════════════════════

YT_COOKIES   = os.environ.get("YT_COOKIES_FILE", "")   # path to cookies.txt exported from browser
YT_API_KEY   = os.environ.get("YOUTUBE_API_KEY", "")   # optional YouTube Data API v3 key

def ytdlp_base():
    """Build yt-dlp command with all anti-bot bypass flags."""
    cmd = [
        YTDLP,
        "--no-playlist",
        "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "--add-header", "Accept-Language:en-US,en;q=0.9",
        "--add-header", "Accept:text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "--extractor-args", "youtube:player_client=web,mweb",
        "--sleep-interval", "1",
        "--max-sleep-interval", "3",
        "--retries", "5",
        "--fragment-retries", "5",
        "--socket-timeout", "30",
    ]
    if PROXY:
        cmd += ["--proxy", PROXY]
    if YT_COOKIES and os.path.exists(YT_COOKIES):
        cmd += ["--cookies", YT_COOKIES]
    else:
        # Use browser cookies if available (helps bypass bot detection)
        cmd += ["--cookies-from-browser", "chrome"] if not PROXY else []
    return cmd

def ytdlp_download(video_id, out_path, height=1080):
    """
    Download YouTube video with full fallback chain:
    1. yt-dlp with anti-bot flags
    2. yt-dlp with po_token workaround
    3. YouTube embed URL
    4. pytube fallback
    Returns True on success.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"

    # Strategy 1: yt-dlp with best quality format ladder
    format_ladder = [
        f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={height}]+bestaudio/best[height<={height}][ext=mp4]/best[ext=mp4]/best",
        f"best[height<={height}][ext=mp4]/best[ext=mp4]/best",
        "best",
    ]
    for fmt in format_ladder:
        cmd = ytdlp_base() + [
            "-f", fmt,
            "--merge-output-format", "mp4",
            "-o", out_path, url
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 10000:
                print(f"yt-dlp OK: {out_path} ({os.path.getsize(out_path)//1024}KB)")
                return True
            if os.path.exists(out_path): os.remove(out_path)
            print(f"yt-dlp fmt {fmt} failed: {result.stderr[-200:]}")
        except Exception as e:
            print(f"yt-dlp exception: {e}")
            if os.path.exists(out_path): os.remove(out_path)

    # Strategy 2: yt-dlp with android client (bypasses many restrictions)
    try:
        cmd = [YTDLP, "--no-playlist",
               "--extractor-args", "youtube:player_client=android",
               "--user-agent", "com.google.android.youtube/17.36.4 (Linux; U; Android 12) gzip",
               "-f", "best[ext=mp4]/best",
               "-o", out_path, url]
        if PROXY: cmd += ["--proxy", PROXY]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 10000:
            print(f"yt-dlp android OK: {out_path}")
            return True
        if os.path.exists(out_path): os.remove(out_path)
    except Exception as e:
        print(f"android client failed: {e}")

    # Strategy 3: yt-dlp with iOS client
    try:
        cmd = [YTDLP, "--no-playlist",
               "--extractor-args", "youtube:player_client=ios",
               "-f", "best[ext=mp4]/best",
               "-o", out_path, url]
        if PROXY: cmd += ["--proxy", PROXY]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 10000:
            print(f"yt-dlp iOS OK: {out_path}")
            return True
        if os.path.exists(out_path): os.remove(out_path)
    except Exception as e:
        print(f"iOS client failed: {e}")

    return False

TRANSCRIPT_CACHE = {}  # video_id -> transcript text

@app.route("/analyse", methods=["POST","OPTIONS"])
def analyse():
    if request.method == "OPTIONS": return jsonify({}), 200
    data = request.get_json()
    url  = data.get("url","").strip()
    if not url: return jsonify({"error":"Missing URL"}), 400
    m = re.search(r'(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})', url)
    if not m: return jsonify({"error":"Invalid YouTube URL — paste a youtube.com/watch or youtu.be link"}), 400
    video_id = m.group(1)
    title = "YouTube Video"
    duration = 600

    # 1. Fast title via oEmbed (no yt-dlp needed)
    try:
        r = requests.get(
            f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json",
            timeout=6)
        if r.ok: title = r.json().get("title", title)
    except: pass

    # 2. Duration via yt-dlp --dump-json (with retries + proper headers)
    try:
        cmd = ytdlp_base() + [
            "--dump-json", "--no-download",
            "--socket-timeout", "20",
            "--retries", "3",
            f"https://www.youtube.com/watch?v={video_id}"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        if result.returncode == 0 and result.stdout.strip():
            info = json.loads(result.stdout.strip().splitlines()[0])
            title    = info.get("title", title)
            duration = int(info.get("duration", 600))
    except Exception as e:
        print(f"yt-dlp info error: {e}")

    # 3. Transcript — cache first, then Supadata, then fallback
    if video_id in TRANSCRIPT_CACHE:
        transcript = TRANSCRIPT_CACHE[video_id]
    else:
        transcript = f"Video: {title}. Duration: {duration}s."
        if SUPADATA:
            try:
                resp = requests.get(
                    "https://api.supadata.ai/v1/youtube/transcript",
                    params={"videoId": video_id, "text": "true"},
                    headers={"x-api-key": SUPADATA}, timeout=20)
                if resp.ok:
                    d = resp.json(); c = d.get("content", "")
                    t = c if isinstance(c, str) else " ".join([s.get("text","") for s in c])
                    if t and len(t) > 30:
                        transcript = t
                        TRANSCRIPT_CACHE[video_id] = transcript
            except: pass

    clips = _detect_clips(title, duration, transcript, GROQ_KEY)
    return jsonify({
        "videoId": video_id, "title": title, "duration": duration,
        "clips": clips, "transcriptLength": len(transcript.split()), "mode": "url"
    })

@app.route("/analyse-upload", methods=["POST","OPTIONS"])
def analyse_upload():
    if request.method=="OPTIONS": return jsonify({}),200
    if "video" not in request.files: return jsonify({"error":"No video file"}),400
    file=request.files["video"]
    upload_id=str(uuid.uuid4())[:8]
    ext=os.path.splitext(file.filename)[1].lower() or ".mp4"
    upload_path=os.path.join(UPLOADS_DIR,f"{upload_id}{ext}")
    file.save(upload_path)
    print(f"Saved:{upload_path} {os.path.getsize(upload_path)}B")
    duration=600; title=os.path.splitext(file.filename)[0] or "Uploaded Video"
    try:
        probe=subprocess.run(["ffprobe","-v","quiet","-print_format","json","-show_format","-show_streams",upload_path],capture_output=True,text=True,timeout=30)
        if probe.returncode==0: duration=int(float(json.loads(probe.stdout).get("format",{}).get("duration",600)))
    except: pass
    clips=_detect_clips(title,duration,f"Uploaded:{title}. {duration}s.",GROQ_KEY)
    UPLOAD_STORE[upload_id]=upload_path
    threading.Timer(7200,lambda:_cleanup(upload_id,upload_path)).start()
    return jsonify({"uploadId":upload_id,"uploadPath":upload_path,"title":title,"duration":duration,"clips":clips,"transcriptLength":0,"mode":"upload"})

def _cleanup(uid,path):
    UPLOAD_STORE.pop(uid,None)
    if os.path.exists(path): os.remove(path)

GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-70b-versatile",
    "mixtral-8x7b-32768",
    "gemma2-9b-it"
]

def _groq_chat(messages, max_tokens=1000, temperature=0.1):
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
                    timeout=40
                )
                if resp.status_code == 429:
                    # Rate limited — wait then try next model
                    retry_after = int(resp.headers.get("retry-after", 3))
                    time.sleep(min(retry_after, 5))
                    break  # try next model
                if resp.status_code == 200:
                    return resp.json()["choices"][0]["message"]["content"].strip()
                # Other error — try next model
                break
            except requests.Timeout:
                if attempt == 2: break
                time.sleep(1)
            except Exception as e:
                print(f"Groq {model}: {e}")
                break
    return None

def _detect_clips(title, duration, transcript, groq_key):
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

    # Fallback: evenly spaced clips
    if not clips:
        seg = max(duration // 6, 40)
        for i in range(5):
            s = i * seg + 10
            e = min(s + 60, duration - 5)
            if e > s + 15:
                clips.append({
                    "start": s, "end": e,
                    "title": f"Highlight {i+1}",
                    "hook": "Watch this...",
                    "virality_score": 7,
                    "reason": "Auto-detected segment"
                })
    return clips


# ════════════════════════════════════════════════════════════════════════════════
# WHISPER TRANSCRIPTION
# ════════════════════════════════════════════════════════════════════════════════
def transcribe_with_whisper(audio_path):
    segments = []
    # Groq Whisper (free + fast)
    if GROQ_KEY and os.path.exists(audio_path):
        try:
            with open(audio_path, "rb") as f:
                resp = requests.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {GROQ_KEY}"},
                    files={"file": (os.path.basename(audio_path), f, "audio/mp4")},
                    data={"model": "whisper-large-v3", "response_format": "verbose_json",
                          "timestamp_granularities[]": "segment"},
                    timeout=120)
            if resp.ok:
                segs = resp.json().get("segments", [])
                segments = [{"start": s["start"], "end": s["end"], "text": s["text"].strip()} for s in segs]
                print(f"Groq Whisper OK: {len(segments)} segments")
                return segments
        except Exception as e:
            print(f"Groq Whisper: {e}")
    # OpenAI Whisper fallback
    if OPENAI_KEY and os.path.exists(audio_path):
        try:
            with open(audio_path, "rb") as f:
                resp = requests.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {OPENAI_KEY}"},
                    files={"file": (os.path.basename(audio_path), f, "audio/mp4")},
                    data={"model": "whisper-1", "response_format": "verbose_json",
                          "timestamp_granularities[]": "segment"},
                    timeout=120)
            if resp.ok:
                segs = resp.json().get("segments", [])
                segments = [{"start": s["start"], "end": s["end"], "text": s["text"].strip()} for s in segs]
                print(f"OpenAI Whisper OK: {len(segments)} segments")
                return segments
        except Exception as e:
            print(f"OpenAI Whisper: {e}")
    return segments

def segments_to_srt(segments, offset=0.0):
    def ts(s):
        s = max(0, s - offset)
        return f"{int(s//3600):02d}:{int((s%3600)//60):02d}:{int(s%60):02d},{int((s%1)*1000):03d}"
    return "\n".join(f"{i}\n{ts(s['start'])} --> {ts(s['end'])}\n{s['text']}\n" for i,s in enumerate(segments,1))

def burn_captions_ffmpeg(inp, srt_path, out, style="bold", size="medium", accent="#8b5cf6"):
    fs = {"small":16,"medium":22,"large":30}.get(size,22)
    def hex_ass(h):
        h=h.lstrip("#"); r,g,b=int(h[0:2],16),int(h[2:4],16),int(h[4:6],16)
        return f"&H00{b:02X}{g:02X}{r:02X}"
    acc=hex_ass(accent)
    sm = {
        "bold":    f"FontName=Arial,FontSize={fs},PrimaryColour=&H00FFFFFF,Bold=1,Outline=2,OutlineColour=&H00000000,Shadow=1,Alignment=2",
        "outline": f"FontName=Arial,FontSize={fs},PrimaryColour=&H00FFFFFF,Bold=0,Outline=2,OutlineColour=&H00000000,Alignment=2",
        "box":     f"FontName=Arial,FontSize={fs},PrimaryColour=&H00FFFFFF,Bold=1,BackColour=&H80000000,BorderStyle=3,Alignment=2",
        "lime":    f"FontName=Arial,FontSize={fs},PrimaryColour={acc},Bold=1,Outline=2,OutlineColour=&H00000000,Alignment=2",
        "neon":    f"FontName=Arial,FontSize={fs},PrimaryColour=&H00FFFFFF,Bold=1,Outline=3,OutlineColour={acc},Shadow=2,Alignment=2",
        "karaoke": f"FontName=Arial,FontSize={fs},PrimaryColour=&H00FFFFFF,SecondaryColour={acc},Bold=1,Outline=1,Alignment=2",
    }
    r = subprocess.run(["ffmpeg","-y","-i",inp,"-vf",f"subtitles={srt_path}:force_style='{sm.get(style,sm[chr(98)+chr(111)+chr(108)+chr(100)])}'","-c:v","libx264","-preset","fast","-crf","20","-c:a","copy",out], capture_output=True, text=True, timeout=300)
    return r.returncode == 0


# ════════════════════════════════════════════════════════════════════════════════
# PEXELS B-ROLL
# ════════════════════════════════════════════════════════════════════════════════
def fetch_pexels_videos(keywords, count=3):
    if not PEXELS_KEY: return []
    results = []
    for kw in [k.strip() for k in keywords.replace(","," ").split() if k.strip()][:3]:
        try:
            resp = requests.get("https://api.pexels.com/videos/search",
                headers={"Authorization": PEXELS_KEY},
                params={"query":kw,"per_page":3,"orientation":"portrait","size":"medium"},
                timeout=15)
            if resp.ok:
                for v in resp.json().get("videos",[]):
                    for vf in v.get("video_files",[]):
                        if vf.get("quality") in ("hd","sd") and vf.get("width",0)<=1080:
                            results.append(vf["link"]); break
        except Exception as e:
            print(f"Pexels {kw}: {e}")
    return results[:count]

def download_pexels_clip(url, out_path, duration=5):
    try:
        r = requests.get(url, stream=True, timeout=60)
        raw = out_path+".raw.mp4"
        with open(raw,"wb") as f:
            for chunk in r.iter_content(65536): f.write(chunk)
        subprocess.run(["ffmpeg","-y","-i",raw,"-t",str(duration),"-c:v","libx264","-preset","fast","-crf","22","-an",out_path], capture_output=True, timeout=60)
        if os.path.exists(raw): os.remove(raw)
        return os.path.exists(out_path) and os.path.getsize(out_path)>1000
    except Exception as e:
        print(f"Pexels dl: {e}"); return False

def insert_broll(main_video, broll_clips, output_path, frequency="medium"):
    if not broll_clips: shutil.copy(main_video, output_path); return True
    try:
        probe = subprocess.run(["ffprobe","-v","quiet","-print_format","json","-show_format",main_video], capture_output=True, text=True)
        total = float(json.loads(probe.stdout).get("format",{}).get("duration",60))
        interval = {"low":30,"medium":20,"high":12}.get(frequency,20)
        job_dir = os.path.dirname(output_path)
        concat_list, pos, bi = [], 0.0, 0
        while pos < total:
            seg_end = min(pos+interval, total)
            sp = os.path.join(job_dir, f"bseg_{int(pos)}.mp4")
            subprocess.run(["ffmpeg","-y","-i",main_video,"-ss",str(pos),"-t",str(seg_end-pos),"-c","copy",sp], capture_output=True, timeout=60)
            if os.path.exists(sp): concat_list.append(sp)
            if bi < len(broll_clips) and seg_end < total: concat_list.append(broll_clips[bi]); bi += 1
            pos = seg_end
        cf = os.path.join(job_dir,"broll_concat.txt")
        with open(cf,"w") as f:
            for p in concat_list: f.write(f"file '{p}'\n")
        r = subprocess.run(["ffmpeg","-y","-f","concat","-safe","0","-i",cf,"-c:v","libx264","-preset","fast","-crf","20","-c:a","aac","-b:a","128k",output_path], capture_output=True, text=True, timeout=300)
        return r.returncode==0
    except Exception as e:
        print(f"B-Roll: {e}"); shutil.copy(main_video, output_path); return False


# ════════════════════════════════════════════════════════════════════════════════
# FILLER REMOVAL
# ════════════════════════════════════════════════════════════════════════════════
def remove_fillers_from_video(video_path, segments, filler_words, output_path):
    if not segments or not filler_words: shutil.copy(video_path, output_path); return True
    try:
        fw_set = {w.lower().strip() for w in filler_words}
        cuts = [(s["start"],s["end"]) for s in segments
                if s["end"]-s["start"]<2.5 and
                any(fw in s["text"].lower() for fw in fw_set)]
        if not cuts: shutil.copy(video_path, output_path); return True
        probe = subprocess.run(["ffprobe","-v","quiet","-print_format","json","-show_format",video_path], capture_output=True, text=True)
        total = float(json.loads(probe.stdout).get("format",{}).get("duration",60))
        keep, pos = [], 0.0
        for s,e in sorted(cuts):
            if s > pos+0.1: keep.append((pos,s))
            pos = e
        if pos < total-0.1: keep.append((pos,total))
        job_dir = os.path.dirname(output_path)
        segs, cf = [], os.path.join(job_dir,"fill_concat.txt")
        for i,(s,e) in enumerate(keep):
            sp = os.path.join(job_dir,f"fk_{i}.mp4")
            subprocess.run(["ffmpeg","-y","-i",video_path,"-ss",str(s),"-t",str(e-s),"-c","copy",sp], capture_output=True, timeout=60)
            if os.path.exists(sp) and os.path.getsize(sp)>100: segs.append(sp)
        with open(cf,"w") as f:
            for p in segs: f.write(f"file '{p}'\n")
        r = subprocess.run(["ffmpeg","-y","-f","concat","-safe","0","-i",cf,"-c:v","libx264","-preset","fast","-crf","20","-c:a","aac","-b:a","128k",output_path], capture_output=True, text=True, timeout=300)
        print(f"Filler removal: {len(cuts)} cuts")
        return r.returncode==0
    except Exception as e:
        print(f"Filler: {e}"); shutil.copy(video_path,output_path); return False


# ════════════════════════════════════════════════════════════════════════════════
# THUMBNAIL GENERATION
# ════════════════════════════════════════════════════════════════════════════════
def generate_thumbnail(video_path, output_path, title="", style="bold", accent="#8b5cf6"):
    try:
        probe = subprocess.run(["ffprobe","-v","quiet","-print_format","json","-show_format",video_path], capture_output=True, text=True)
        dur = float(json.loads(probe.stdout).get("format",{}).get("duration",30))
        job_dir = os.path.dirname(output_path)
        frames = []
        for i in range(1,8):
            fp = os.path.join(job_dir,f"tc_{i}.jpg")
            subprocess.run(["ffmpeg","-y","-ss",str(dur*i/8),"-i",video_path,"-vframes","1","-q:v","2",fp], capture_output=True, timeout=30)
            if os.path.exists(fp) and os.path.getsize(fp)>1000: frames.append(fp)
        if not frames: return False
        best = frames[len(frames)//2]
        safe = (title[:40] if title else "Watch this").replace("'","").replace('"','').replace(':','')
        vf = f"scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,drawbox=x=0:y=ih-160:w=iw:h=160:color=black@0.75:t=fill,drawtext=text='{safe}':fontsize=52:fontcolor=white:x=(w-text_w)/2:y=h-130:shadowcolor=black:shadowx=2:shadowy=2"
        r = subprocess.run(["ffmpeg","-y","-i",best,"-vf",vf,"-q:v","1",output_path], capture_output=True, text=True, timeout=60)
        if r.returncode!=0:
            subprocess.run(["ffmpeg","-y","-i",best,"-vf","scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720","-q:v","1",output_path], capture_output=True, timeout=60)
        for f in frames:
            if os.path.exists(f): os.remove(f)
        return os.path.exists(output_path)
    except Exception as e:
        print(f"Thumb: {e}"); return False

@app.route("/thumbnail/<job_id>", methods=["GET"])
def get_thumbnail(job_id):
    p = os.path.join(CLIPS_DIR, job_id, "thumbnail.jpg")
    if os.path.exists(p): return send_file(p, mimetype="image/jpeg")
    return jsonify({"error":"Not found"}), 404


@app.route("/clip",methods=["POST","OPTIONS"])
def create_clip():
    if request.method=="OPTIONS": return jsonify({}),200
    data=request.get_json()
    video_id=data.get("videoId","").strip()
    upload_path=data.get("uploadPath","").strip()
    start=float(data.get("start",0)); end=float(data.get("end",60))
    formats=data.get("formats",["vertical","horizontal"])
    if not video_id and not upload_path: return jsonify({"error":"Missing source"}),400
    if end-start<5: return jsonify({"error":"Clip too short"}),400
    if upload_path and not os.path.exists(upload_path):
        for uid,path in UPLOAD_STORE.items():
            if uid in upload_path: upload_path=path; break
    job_id=str(uuid.uuid4())[:8]
    JOBS[job_id]={"status":"queued","progress":0,"message":"Starting...","files":{}}
    t=threading.Thread(target=process_clip_job,args=(job_id,video_id,upload_path,start,end,formats))
    t.daemon=True; t.start()
    return jsonify({"jobId":job_id})

def process_clip_job(job_id, video_id, upload_path, start, end, formats):
    job_dir = os.path.join(CLIPS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    raw_video = None
    try:
        if upload_path and os.path.exists(upload_path):
            raw_video = upload_path
            sz = os.path.getsize(upload_path)
            print(f"Job {job_id}: upload {sz}B")
            update_job(job_id, "processing", 15, f"Using uploaded file ({sz//1024}KB)...")
        elif video_id:
            update_job(job_id, "downloading", 5, "Downloading video from YouTube...")
            raw_video = os.path.join(job_dir, "raw.mp4")

            # Try 1080p first, fall back to 720p
            success = ytdlp_download(video_id, raw_video, height=1080)
            if not success:
                update_job(job_id, "downloading", 12, "Retrying at 720p...")
                success = ytdlp_download(video_id, raw_video, height=720)
            if not success:
                raise Exception(
                    "YouTube download failed — YouTube blocks server IPs. "
                    "Please use the 'Upload file' tab to upload the video directly instead."
                )
        else:
            raise Exception("No video source provided")

        if not raw_video or not os.path.exists(raw_video):
            raise Exception("Source video not found")
        sz = os.path.getsize(raw_video)
        if sz == 0:
            raise Exception("Source file is empty — download may have been blocked")

        update_job(job_id, "cutting", 35, "Cutting clip to exact timestamps...")
        cut_video = os.path.join(job_dir, "cut.mp4")

        # Fast cut with stream copy first
        cut = subprocess.run([
            "ffmpeg", "-y",
            "-ss", str(start), "-i", raw_video,
            "-t", str(end - start),
            "-c", "copy", "-avoid_negative_ts", "make_zero",
            cut_video
        ], capture_output=True, text=True, timeout=120)

        # Fallback: re-encode if stream copy failed
        if not os.path.exists(cut_video) or os.path.getsize(cut_video) < 1000:
            subprocess.run([
                "ffmpeg", "-y", "-i", raw_video,
                "-ss", str(start), "-t", str(end - start),
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k",
                cut_video
            ], capture_output=True, text=True, timeout=180)

        if not os.path.exists(cut_video) or os.path.getsize(cut_video) < 1000:
            raise Exception("Failed to cut clip")

        update_job(job_id, "converting", 60, "Converting to HD formats...")
        output_files = {}

        # Probe source resolution
        probe = subprocess.run([
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", cut_video
        ], capture_output=True, text=True)
        src_height = 1080
        try:
            streams = json.loads(probe.stdout).get("streams", [])
            for s in streams:
                if s.get("codec_type") == "video":
                    src_height = int(s.get("height", 1080))
                    break
        except: pass

        # Target HD: up to 1080p for vertical, up to 1080p for horizontal
        # If source is 4K (2160p), output 4K
        out_h_vert  = min(src_height, 1920)   # 9:16 height
        out_w_vert  = int(out_h_vert * 9/16)
        out_h_vert  = int(out_w_vert * 16/9)
        # Force standard sizes
        vert_res  = "1080x1920" if src_height >= 1080 else "720x1280"
        horiz_res = "1920x1080" if src_height >= 1080 else "1280x720"
        crf = "18" if src_height >= 1080 else "20"

        if "vertical" in formats:
            vpath = os.path.join(job_dir, "vertical.mp4")
            w, h = vert_res.split("x")
            subprocess.run([
                "ffmpeg", "-y", "-i", cut_video,
                "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                       f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,"
                       f"setsar=1",
                "-c:v", "libx264", "-preset", "slow", "-crf", crf,
                "-profile:v", "high", "-level", "4.2",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
                "-movflags", "+faststart",
                "-r", "30", vpath
            ], capture_output=True, text=True, timeout=300)
            if os.path.exists(vpath) and os.path.getsize(vpath) > 1000:
                output_files["vertical"] = vpath

        if "horizontal" in formats:
            hpath = os.path.join(job_dir, "horizontal.mp4")
            w, h = horiz_res.split("x")
            subprocess.run([
                "ffmpeg", "-y", "-i", cut_video,
                "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                       f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,"
                       f"setsar=1",
                "-c:v", "libx264", "-preset", "slow", "-crf", crf,
                "-profile:v", "high", "-level", "4.2",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
                "-movflags", "+faststart",
                "-r", "30", hpath
            ], capture_output=True, text=True, timeout=300)
            if os.path.exists(hpath) and os.path.getsize(hpath) > 1000:
                output_files["horizontal"] = hpath

        if not output_files:
            raise Exception("No output files created — FFmpeg conversion failed")

        refs = {
            fmt: {
                "downloadUrl": f"/download/{job_id}/{fmt}",
                "publicUrl": f"{BACKEND_URL}/download/{job_id}/{fmt}",
                "sizeMb": round(os.path.getsize(p) / (1024*1024), 1),
                "resolution": vert_res if fmt=="vertical" else horiz_res,
                "quality": "1080p HD" if src_height >= 1080 else "720p"
            }
            for fmt, p in output_files.items()
        }
        update_job(job_id, "done", 100, "Clip ready!", files=refs)
        threading.Timer(3600, lambda: shutil.rmtree(job_dir, ignore_errors=True)).start()

    except Exception as e:
        print(f"Job {job_id}: {e}")
        update_job(job_id, "error", 0, str(e))

def update_job(job_id,status,progress,message,files=None):
    JOBS[job_id].update({"status":status,"progress":progress,"message":message,"updatedAt":time.time()})
    if files is not None: JOBS[job_id]["files"]=files

@app.route("/job/<job_id>",methods=["GET"])
def get_job(job_id):
    job=JOBS.get(job_id)
    if not job: return jsonify({"error":"Not found"}),404
    return jsonify(job)

@app.route("/download/<job_id>/<fmt>",methods=["GET"])
def download_clip(job_id,fmt):
    job=JOBS.get(job_id)
    if not job or job["status"]!="done": return jsonify({"error":"Not ready"}),404
    filepath=os.path.join(CLIPS_DIR,job_id,f"{fmt}.mp4")
    if not os.path.exists(filepath): return jsonify({"error":"File not found"}),404
    return send_file(filepath,as_attachment=True,download_name=f"vidpost_{fmt}_{job_id}.mp4")


# ── Stream proxy for YouTube (editor preview) ─────────────────────────────────
@app.route("/stream/<video_id>", methods=["GET"])
def stream_video(video_id):
    try:
        out_path = os.path.join(CLIPS_DIR, f"preview_{video_id}.mp4")
        if not os.path.exists(out_path) or os.path.getsize(out_path) < 10000:
            if os.path.exists(out_path): os.remove(out_path)
            success = ytdlp_download(video_id, out_path, height=480)
        else:
            success = True
        if success and os.path.exists(out_path):
            return send_file(out_path, mimetype="video/mp4", conditional=True)
        return jsonify({"error": "Could not stream — please upload the file directly"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Editor export (trim + optional captions + optional logo) ──────────────────
@app.route("/editor-export", methods=["POST","OPTIONS"])
def editor_export():
    if request.method == "OPTIONS": return jsonify({}), 200
    job_id = str(uuid.uuid4())[:8]
    job_dir = os.path.join(CLIPS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    JOBS[job_id] = {"status":"queued","progress":0,"message":"Queued","files":{}}

    is_form = request.content_type and "multipart" in request.content_type
    def gf(k,d=""): return request.form.get(k,d) if is_form else (request.get_json() or {}).get(k,d)
    def gfb(k,d=False):
        v = request.form.get(k,"") if is_form else str((request.get_json() or {}).get(k,d))
        return v.lower() in ("true","1","yes")
    def gfi(k,d=0): 
        try: return int(request.form.get(k,d) if is_form else (request.get_json() or {}).get(k,d))
        except: return d

    data = {} if is_form else (request.get_json() or {})
    video_url       = gf("video_url")
    start           = float(gf("start","0") or "0")
    end             = float(gf("end","60") or "60")
    captions        = gfb("captions", True)
    cap_style       = gf("caption_style","bold")
    cap_size        = gf("caption_size","medium")
    formats         = gf("formats","vertical")
    quality         = gf("quality","1080")
    logo_pos        = gf("logo_position","top-right")
    logo_opacity    = gfi("logo_opacity", 80)
    video_id        = gf("videoId","")
    remove_fillers  = gfb("remove_fillers", False)
    filler_words    = data.get("filler_words", gf("filler_words","um,uh,like").split(",")) if not is_form else gf("filler_words","um,uh,like").split(",")
    remove_pauses   = gfb("remove_pauses", False)
    audio_norm      = gfb("audio_norm", True)
    broll           = gfb("broll", False)
    broll_kw        = gf("broll_kw","")
    broll_freq      = gf("broll_freq","medium") or gf("broll_frequency","medium")
    lower_third     = gfb("lower_third", False)
    lower_name      = gf("lower_name","")
    lower_title_text= gf("lower_title","")
    brand_primary   = gf("brand_primary","#8b5cf6")

    logo_file  = request.files.get("logo") if is_form else None
    logo_path  = None
    if logo_file:
        logo_path = os.path.join(job_dir, "logo.png")
        logo_file.save(logo_path)

    def run_editor_job():
        try:
            JOBS[job_id].update({"status":"running","progress":5,"message":"Getting source video..."})

            # ── Step 1: Get source ──────────────────────────────────────────
            if video_id:
                src = os.path.join(CLIPS_DIR, f"preview_{video_id}.mp4")
                if not os.path.exists(src) or os.path.getsize(src) < 10000:
                    JOBS[job_id].update({"progress":8,"message":"Downloading from YouTube..."})
                    if not ytdlp_download(video_id, src, height=1080):
                        ytdlp_download(video_id, src, height=720)
            elif video_url:
                src = os.path.join(job_dir,"source.mp4")
                JOBS[job_id].update({"progress":8,"message":"Fetching video..."})
                r2 = requests.get(video_url, stream=True, timeout=120)
                with open(src,"wb") as f2:
                    for chunk in r2.iter_content(65536): f2.write(chunk)
            else:
                JOBS[job_id].update({"status":"error","message":"No video source"}); return

            if not os.path.exists(src) or os.path.getsize(src) < 10000:
                JOBS[job_id].update({"status":"error","message":"Could not obtain video. Please upload directly."}); return

            # ── Step 2: Extract audio for Whisper ──────────────────────────
            segments = []
            audio_path = os.path.join(job_dir, "audio.mp4")
            if captions or remove_fillers:
                JOBS[job_id].update({"progress":15,"message":"Extracting audio for transcription..."})
                subprocess.run(["ffmpeg","-y","-i",src,"-ss",str(start),"-t",str(end-start),
                                "-vn","-c:a","aac","-b:a","128k",audio_path],
                               capture_output=True, timeout=120)
                if os.path.exists(audio_path) and os.path.getsize(audio_path) > 1000:
                    JOBS[job_id].update({"progress":20,"message":"Transcribing with Whisper AI..."})
                    segments = transcribe_with_whisper(audio_path)
                    print(f"Editor job {job_id}: got {len(segments)} Whisper segments")

            # ── Step 3: Filler removal (on raw segment before trimming) ────
            JOBS[job_id].update({"progress":28,"message":"Trimming clip..."})
            # First trim the source
            trimmed = os.path.join(job_dir,"trimmed.mp4")
            r_trim = subprocess.run(["ffmpeg","-y","-ss",str(start),"-i",src,"-t",str(end-start),
                                     "-c","copy","-avoid_negative_ts","make_zero",trimmed],
                                    capture_output=True, timeout=180)
            if r_trim.returncode != 0 or not os.path.exists(trimmed):
                subprocess.run(["ffmpeg","-y","-i",src,"-ss",str(start),"-t",str(end-start),
                                "-c:v","libx264","-preset","fast","-crf","20",
                                "-c:a","aac","-b:a","128k",trimmed],
                               capture_output=True, timeout=180)

            current = trimmed
            if not os.path.exists(current): raise Exception("Trim failed")

            # Filler removal
            if remove_fillers and segments and filler_words:
                JOBS[job_id].update({"progress":33,"message":f"Removing filler words ({len(filler_words)} types)..."})
                filler_out = os.path.join(job_dir,"no_fillers.mp4")
                # Adjust segment times relative to trim start
                adj_segs = [{"start":max(0,s["start"]-start),"end":max(0,s["end"]-start),"text":s["text"]}
                            for s in segments]
                remove_fillers_from_video(current, adj_segs, filler_words, filler_out)
                if os.path.exists(filler_out) and os.path.getsize(filler_out) > 10000:
                    current = filler_out

            # Pause removal (silence detection)
            if remove_pauses:
                JOBS[job_id].update({"progress":36,"message":"Removing long pauses..."})
                pause_out = os.path.join(job_dir,"no_pauses.mp4")
                subprocess.run([
                    "ffmpeg","-y","-i",current,
                    "-af","silenceremove=stop_periods=-1:stop_duration=0.8:stop_threshold=-50dB",
                    "-c:v","copy",pause_out
                ], capture_output=True, timeout=180)
                if os.path.exists(pause_out) and os.path.getsize(pause_out) > 10000:
                    current = pause_out

            # Audio normalise
            if audio_norm:
                JOBS[job_id].update({"progress":39,"message":"Normalising audio levels..."})
                norm_out = os.path.join(job_dir,"normalised.mp4")
                subprocess.run([
                    "ffmpeg","-y","-i",current,
                    "-af","loudnorm=I=-16:TP=-1.5:LRA=11",
                    "-c:v","copy",norm_out
                ], capture_output=True, timeout=180)
                if os.path.exists(norm_out) and os.path.getsize(norm_out) > 10000:
                    current = norm_out

            # ── Step 4: B-Roll insertion ────────────────────────────────────
            if broll and broll_kw:
                JOBS[job_id].update({"progress":43,"message":"Fetching B-Roll from Pexels..."})
                pexels_urls = fetch_pexels_videos(broll_kw, count=4)
                if pexels_urls:
                    broll_files = []
                    for i,pu in enumerate(pexels_urls):
                        bf = os.path.join(job_dir,f"broll_{i}.mp4")
                        if download_pexels_clip(pu, bf, duration=5):
                            broll_files.append(bf)
                    if broll_files:
                        JOBS[job_id].update({"progress":50,"message":f"Inserting {len(broll_files)} B-Roll clips..."})
                        broll_out = os.path.join(job_dir,"with_broll.mp4")
                        insert_broll(current, broll_files, broll_out, broll_freq)
                        if os.path.exists(broll_out) and os.path.getsize(broll_out) > 10000:
                            current = broll_out

            # ── Step 5: Format conversion (vertical + horizontal) ───────────
            fmt_list = ["vertical","horizontal"] if formats=="both" else [formats]
            output_files = {}

            for i_fmt, fmt in enumerate(fmt_list):
                JOBS[job_id].update({"progress":55+i_fmt*15,"message":f"Converting to {fmt} HD format..."})
                if fmt == "vertical":
                    vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1"
                    res_label = "1080x1920"
                else:
                    vf = "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,setsar=1"
                    res_label = "1920x1080"

                crf = "18" if quality=="1080" else "22"
                fmt_out = os.path.join(job_dir, f"fmt_{fmt}.mp4")
                subprocess.run([
                    "ffmpeg","-y","-i",current,"-vf",vf,
                    "-c:v","libx264","-preset","slow","-crf",crf,
                    "-profile:v","high","-level","4.2",
                    "-c:a","aac","-b:a","192k","-ar","44100",
                    "-movflags","+faststart","-r","30",fmt_out
                ], capture_output=True, timeout=600)

                if not os.path.exists(fmt_out) or os.path.getsize(fmt_out) < 1000:
                    JOBS[job_id].update({"status":"error","message":f"Format conversion failed for {fmt}"}); return

                # ── Step 6: Logo watermark ──────────────────────────────────
                logo_result = fmt_out
                if logo_path and os.path.exists(logo_path):
                    JOBS[job_id].update({"progress":70+i_fmt*5,"message":"Applying logo watermark..."})
                    pos_map = {"top-left":"10:10","top-right":"W-w-10:10",
                               "bottom-left":"10:H-h-10","bottom-right":"W-w-10:H-h-10"}
                    pos = pos_map.get(logo_pos,"W-w-10:10")
                    alpha = logo_opacity/100.0
                    logo_vf = f"[1:v]scale=150:-1,format=rgba,colorchannelmixer=aa={alpha}[logo];[0:v][logo]overlay={pos}"
                    logo_out = os.path.join(job_dir, f"logo_{fmt}.mp4")
                    r3 = subprocess.run(["ffmpeg","-y","-i",fmt_out,"-i",logo_path,
                                        "-filter_complex",logo_vf,"-c:a","copy",logo_out],
                                       capture_output=True, timeout=300)
                    if r3.returncode == 0 and os.path.exists(logo_out):
                        logo_result = logo_out

                # ── Step 7: Lower third ────────────────────────────────────
                lower_result = logo_result
                if lower_third and lower_name:
                    lt_out = os.path.join(job_dir, f"lt_{fmt}.mp4")
                    safe_name  = lower_name.replace("'","").replace('"','')[:30]
                    safe_title_lt = lower_title_text.replace("'","").replace('"','')[:40] if lower_title_text else ""
                    lt_vf = (f"drawbox=x=0:y=ih-90:w=iw:h=90:color=black@0.8:t=fill,"
                             f"drawtext=text='{safe_name}':fontsize=28:fontcolor=white:x=20:y=ih-75:bold=1,"
                             f"drawtext=text='{safe_title_lt}':fontsize=18:fontcolor=#aaaaaa:x=20:y=ih-42")
                    r4 = subprocess.run(["ffmpeg","-y","-i",logo_result,"-vf",lt_vf,
                                        "-c:v","libx264","-preset","fast","-crf","20",
                                        "-c:a","copy",lt_out], capture_output=True, timeout=300)
                    if r4.returncode == 0 and os.path.exists(lt_out):
                        lower_result = lt_out

                # ── Step 8: Real Whisper captions ──────────────────────────
                cap_result = lower_result
                if captions:
                    JOBS[job_id].update({"progress":78+i_fmt*5,"message":"Burning captions..."})
                    srt_path = os.path.join(job_dir, f"caps_{fmt}.srt")
                    if segments:
                        # Real Whisper segments — adjust for trim offset
                        adj = [{"start":max(0,s["start"]-start),"end":max(0,s["end"]-start),"text":s["text"]}
                               for s in segments]
                        srt_content = segments_to_srt(adj)
                    else:
                        # Fallback placeholder SRT
                        dur2 = end - start
                        srt_content = f"1\n00:00:00,000 --> {int(dur2//60):02d}:{int(dur2%60):02d},000\nAuto-captions\n"
                    with open(srt_path,"w",encoding="utf-8") as f3: f3.write(srt_content)
                    cap_out2 = os.path.join(job_dir, f"cap_{fmt}.mp4")
                    if burn_captions_ffmpeg(lower_result, srt_path, cap_out2, cap_style, cap_size, brand_primary):
                        cap_result = cap_out2

                # Final output
                final = os.path.join(job_dir, f"{fmt}.mp4")
                shutil.copy(cap_result, final)
                size_mb = round(os.path.getsize(final)/(1024*1024),1)
                output_files[fmt] = {
                    "downloadUrl": f"/download/{job_id}/{fmt}",
                    "publicUrl":   f"/download/{job_id}/{fmt}",
                    "sizeMb":      size_mb,
                    "resolution":  res_label,
                    "quality":     f"{quality}p HD"
                }

            # ── Step 9: Generate thumbnail ──────────────────────────────────
            JOBS[job_id].update({"progress":95,"message":"Generating thumbnail..."})
            thumb_path = os.path.join(job_dir,"thumbnail.jpg")
            first_fmt = list(output_files.keys())[0] if output_files else None
            if first_fmt:
                thumb_src = os.path.join(job_dir, f"{first_fmt}.mp4")
                generate_thumbnail(thumb_src, thumb_path, title=video_url or "", accent=brand_primary)
                if os.path.exists(thumb_path):
                    for fmt2 in output_files:
                        output_files[fmt2]["thumbnailUrl"] = f"/thumbnail/{job_id}"

            JOBS[job_id].update({"status":"done","progress":100,"message":"Export complete!","files":output_files})
            threading.Timer(3600, lambda: shutil.rmtree(job_dir, ignore_errors=True)).start()

        except Exception as e:
            import traceback; traceback.print_exc()
            JOBS[job_id].update({"status":"error","message":str(e),"progress":0})

    import threading
    threading.Thread(target=run_editor_job, daemon=True).start()
    return jsonify({"jobId": job_id, "status":"queued"}), 200

@app.route("/transcript",methods=["POST","OPTIONS"])
def get_transcript():
    if request.method=="OPTIONS": return jsonify({}),200
    data=request.get_json(); video_id=data.get("videoId","").strip()
    if not video_id: return jsonify({"error":"Missing videoId"}),400
    if not SUPADATA: return jsonify({"error":"Supadata not configured"}),500
    title="YouTube Video"
    try:
        r=requests.get(f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json",timeout=5)
        if r.ok: title=r.json().get("title",title)
    except: pass
    try:
        resp=requests.get("https://api.supadata.ai/v1/youtube/transcript",params={"videoId":video_id,"text":"true"},headers={"x-api-key":SUPADATA},timeout=15)
        if resp.ok:
            d=resp.json();c=d.get("content","")
            text=c if isinstance(c,str) else " ".join([s.get("text","") for s in c])
            if text and len(text)>30:
                text=re.sub(r'\[.*?\]','',text);text=re.sub(r'\s+',' ',text).strip()
                return jsonify({"transcript":text[:10000],"wordCount":len(text.split()),"title":title,"videoId":video_id})
    except Exception as e: print(f"Transcript:{e}")
    return jsonify({"error":"Could not fetch transcript"}),422

@app.route("/generate",methods=["POST","OPTIONS"])
def generate_posts():
    if request.method=="OPTIONS": return jsonify({}),200
    data=request.get_json()
    if not data: return jsonify({"error":"No body"}),400
    transcript=data.get("transcript","").strip(); title=data.get("title","YouTube Video")
    tone=data.get("tone","professional"); platforms=data.get("platforms",["linkedin"])
    if not transcript: return jsonify({"error":"Missing transcript"}),400
    if not GROQ_KEY: return jsonify({"error":"No Groq API key set in Railway environment"}),400
    tone_map={"professional":"professional and insightful","casual":"casual and conversational","bold":"bold and provocative","storytelling":"storytelling with a narrative arc"}
    tone_desc=tone_map.get(tone,tone_map["professional"])
    trunc=transcript[:4000]
    prompts={
        "linkedin":f'Write viral LinkedIn post.\nVIDEO:"{title}"\nTONE:{tone_desc}\nTRANSCRIPT:{trunc}\n\n[Hook under 12 words]\n\n[emoji] Insight 1\n[emoji] Insight 2\n[emoji] Insight 3\n\n[Question]\n\n#tag1 #tag2 #tag3\n\nMax 200 words. Output ONLY the post text.',
        "twitter":f'Write 6-tweet thread.\nVIDEO:"{title}"\nTONE:{tone_desc}\nTRANSCRIPT:{trunc}\n\nTweet 1:hook\nTweets 2-5:insights\nTweet 6:CTA\nNumber each 1/6 etc.\nOutput ONLY the 6 tweets.',
        "instagram":f'Write Instagram caption.\nVIDEO:"{title}"\nTONE:{tone_desc}\nTRANSCRIPT:{trunc}\n\nHook line\n\n3 paragraphs with emojis\n\nEngaging question\n\n15 hashtags\n\nOutput ONLY the caption.',
        "tiktok":f'Write 45-sec TikTok script.\nVIDEO:"{title}"\nTONE:{tone_desc}\nTRANSCRIPT:{trunc}\n\nHOOK→POINT1→POINT2→POINT3→CTA\n\nUnder 130 words. Output ONLY the script.',
        "youtube_short":f'Write YouTube Shorts script.\nVIDEO:"{title}"\nTONE:{tone_desc}\nTRANSCRIPT:{trunc}\n\n[HOOK][POINT1][POINT2][POINT3][CTA]\n\nUnder 150 words. Output ONLY the script.',
        "facebook":f'Write a Facebook Page post.\nVIDEO:"{title}"\nTONE:{tone_desc}\nTRANSCRIPT:{trunc}\n\nHook → 2-3 paragraphs with emojis → comment-driving question → 3-5 hashtags.\n\nMax 250 words. Output ONLY the post.'
    }
    results,errors={},{}
    for platform in platforms:
        if platform not in prompts: continue
        try:
            text = _groq_chat(
                [{"role":"user","content":prompts[platform]}],
                max_tokens=1024, temperature=0.75
            )
            if text: results[platform]=text
            else: errors[platform]="Generation failed — rate limit or API error. Try again in a moment."
        except Exception as e: errors[platform]=str(e)
    if not results: return jsonify({"error":"Generation failed — check your GROQ_API_KEY in Railway","details":errors}),500
    return jsonify({"results":results,"errors":errors,"title":title})


# ════════════════════════════════════════════════════════════════════════════════
# STRIPE BILLING
# ════════════════════════════════════════════════════════════════════════════════
STRIPE_SECRET      = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SEC = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_CREATOR = os.environ.get("STRIPE_PRICE_CREATOR", "")  # $19/mo price ID
STRIPE_PRICE_AGENCY  = os.environ.get("STRIPE_PRICE_AGENCY",  "")  # $99/mo price ID

PLAN_LIMITS = {
    "free":    {"clips_per_month": 5,  "platforms": ["linkedin","youtube"], "watermark": True},
    "creator": {"clips_per_month": 999,"platforms": ["linkedin","youtube","instagram","tiktok","twitter","facebook"], "watermark": False},
    "agency":  {"clips_per_month": 999,"platforms": ["linkedin","youtube","instagram","tiktok","twitter","facebook"], "watermark": False, "white_label": True},
}

def get_user_plan(user_id):
    """Get user's current plan from Supabase."""
    if not supa: return "free"
    try:
        res = supa.table("subscriptions").select("plan,status").eq("user_id", user_id).execute()
        if res.data:
            row = res.data[0]
            if row.get("status") in ("active","trialing"): return row.get("plan","free")
    except: pass
    return "free"

def get_usage_this_month(user_id):
    """Count clips created this month."""
    if not supa: return 0
    try:
        import datetime
        start = datetime.datetime.utcnow().replace(day=1,hour=0,minute=0,second=0,microsecond=0).isoformat()
        res = supa.table("clip_usage").select("id",count="exact").eq("user_id",user_id).gte("created_at",start).execute()
        return res.count or 0
    except: return 0

def record_usage(user_id, job_id):
    """Record a clip creation event."""
    if not supa: return
    try:
        supa.table("clip_usage").insert({"user_id":user_id,"job_id":job_id,"created_at":"now()"}).execute()
    except: pass

@app.route("/billing/create-checkout", methods=["POST","OPTIONS"])
def create_checkout():
    if request.method=="OPTIONS": return jsonify({}),200
    if not STRIPE_SECRET: return jsonify({"error":"Stripe not configured"}),400
    import stripe as st
    st.api_key = STRIPE_SECRET
    data = request.get_json() or {}
    user_id  = data.get("user_id","")
    plan     = data.get("plan","creator")
    price_id = STRIPE_PRICE_CREATOR if plan=="creator" else STRIPE_PRICE_AGENCY
    if not price_id: return jsonify({"error":f"Price ID for {plan} not set in Railway Variables"}),400
    try:
        session_obj = st.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url=f"{FRONTEND_URL}?billing=success&plan={plan}",
            cancel_url=f"{FRONTEND_URL}?billing=cancelled",
            client_reference_id=user_id,
            metadata={"user_id": user_id, "plan": plan},
            allow_promotion_codes=True,
        )
        return jsonify({"url": session_obj.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/billing/portal", methods=["POST","OPTIONS"])
def billing_portal():
    if request.method=="OPTIONS": return jsonify({}),200
    if not STRIPE_SECRET: return jsonify({"error":"Stripe not configured"}),400
    import stripe as st
    st.api_key = STRIPE_SECRET
    data = request.get_json() or {}
    user_id = data.get("user_id","")
    try:
        res = supa.table("subscriptions").select("stripe_customer_id").eq("user_id",user_id).execute()
        cust_id = res.data[0]["stripe_customer_id"] if res.data else None
        if not cust_id: return jsonify({"error":"No subscription found"}),404
        portal = st.billing_portal.Session.create(
            customer=cust_id,
            return_url=f"{FRONTEND_URL}?tab=billing"
        )
        return jsonify({"url": portal.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/billing/webhook", methods=["POST"])
def stripe_webhook():
    if not STRIPE_SECRET: return jsonify({}),200
    import stripe as st
    st.api_key = STRIPE_SECRET
    payload = request.get_data()
    sig     = request.headers.get("Stripe-Signature","")
    try:
        event = st.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SEC)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    def upsert_sub(user_id, customer_id, plan, status):
        if not supa: return
        supa.table("subscriptions").upsert({
            "user_id": user_id, "stripe_customer_id": customer_id,
            "plan": plan, "status": status, "updated_at": "now()"
        }, on_conflict="user_id").execute()

    if event["type"] == "checkout.session.completed":
        s = event["data"]["object"]
        upsert_sub(s["metadata"]["user_id"], s["customer"], s["metadata"]["plan"], "active")
    elif event["type"] == "customer.subscription.updated":
        s = event["data"]["object"]
        res = supa.table("subscriptions").select("user_id").eq("stripe_customer_id",s["customer"]).execute()
        if res.data:
            plan = "creator" if STRIPE_PRICE_CREATOR in str(s.get("items","")) else "agency"
            upsert_sub(res.data[0]["user_id"], s["customer"], plan, s["status"])
    elif event["type"] == "customer.subscription.deleted":
        s = event["data"]["object"]
        res = supa.table("subscriptions").select("user_id").eq("stripe_customer_id",s["customer"]).execute()
        if res.data:
            upsert_sub(res.data[0]["user_id"], s["customer"], "free", "cancelled")
    return jsonify({"received": True})

@app.route("/billing/status", methods=["GET","OPTIONS"])
def billing_status():
    if request.method=="OPTIONS": return jsonify({}),200
    user_id = request.args.get("user_id","")
    if not user_id: return jsonify({"plan":"free","usage":0,"limit":5}),200
    plan    = get_user_plan(user_id)
    usage   = get_usage_this_month(user_id)
    limits  = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])
    return jsonify({"plan": plan, "usage": usage, "limit": limits["clips_per_month"],
                    "watermark": limits.get("watermark",True),
                    "platforms": limits["platforms"]})


# ════════════════════════════════════════════════════════════════════════════════
# POST SCHEDULING
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/schedule/create", methods=["POST","OPTIONS"])
def schedule_create():
    if request.method=="OPTIONS": return jsonify({}),200
    data = request.get_json() or {}
    user_id      = data.get("user_id","")
    platforms    = data.get("platforms",[])
    text         = data.get("text","")
    video_url    = data.get("video_url","")
    scheduled_at = data.get("scheduled_at","")  # ISO8601 string
    if not user_id or not platforms or not scheduled_at:
        return jsonify({"error":"Missing user_id, platforms, or scheduled_at"}),400
    if not supa: return jsonify({"error":"Supabase not configured"}),400
    created = []
    for platform in platforms:
        try:
            row = supa.table("scheduled_posts").insert({
                "user_id":      user_id,
                "platform":     platform,
                "text":         text,
                "video_url":    video_url,
                "scheduled_at": scheduled_at,
                "status":       "pending",
                "created_at":   "now()"
            }).execute()
            created.append({"platform": platform, "id": row.data[0]["id"] if row.data else None})
        except Exception as e:
            created.append({"platform": platform, "error": str(e)})
    return jsonify({"created": created})

@app.route("/schedule/list", methods=["GET","OPTIONS"])
def schedule_list():
    if request.method=="OPTIONS": return jsonify({}),200
    user_id = request.args.get("user_id","")
    if not user_id or not supa: return jsonify({"posts":[]}),200
    try:
        res = supa.table("scheduled_posts").select("*").eq("user_id",user_id).order("scheduled_at").execute()
        return jsonify({"posts": res.data or []})
    except Exception as e:
        return jsonify({"error": str(e)}),500

@app.route("/schedule/delete/<post_id>", methods=["DELETE","OPTIONS"])
def schedule_delete(post_id):
    if request.method=="OPTIONS": return jsonify({}),200
    user_id = request.args.get("user_id","")
    if not supa: return jsonify({"error":"No DB"}),400
    try:
        supa.table("scheduled_posts").delete().eq("id",post_id).eq("user_id",user_id).execute()
        return jsonify({"deleted": True})
    except Exception as e:
        return jsonify({"error": str(e)}),500

@app.route("/schedule/run", methods=["POST","OPTIONS"])
def schedule_run():
    """Called by a cron job (Railway Cron) every minute to publish due posts."""
    if request.method=="OPTIONS": return jsonify({}),200
    # Simple auth: shared secret in header
    if request.headers.get("X-Cron-Secret","") != os.environ.get("CRON_SECRET",""):
        return jsonify({"error":"Unauthorized"}),401
    if not supa: return jsonify({"published":0}),200
    import datetime
    now = datetime.datetime.utcnow().isoformat()
    try:
        due = supa.table("scheduled_posts").select("*").eq("status","pending").lte("scheduled_at",now).execute()
        published, failed = 0, 0
        for post in (due.data or []):
            try:
                token_data = get_token(post["user_id"], post["platform"])
                if not token_data: raise Exception("No token")
                result = None
                p = post["platform"]
                if p=="linkedin":  result = post_linkedin(token_data, post["text"], post.get("video_url",""))
                elif p=="youtube": result = post_youtube(token_data, post["text"], post.get("video_url",""))
                elif p=="instagram": result = post_instagram(token_data, post["text"], post.get("video_url",""))
                elif p=="tiktok":  result = post_tiktok(token_data, post["text"], post.get("video_url",""))
                elif p=="twitter": result = post_twitter(token_data, post["text"], post.get("video_url",""))
                elif p=="facebook": result = post_facebook(token_data, post["text"], post.get("video_url",""))
                supa.table("scheduled_posts").update({"status":"posted","platform_post_id":str(result or {}),"posted_at":"now()"}).eq("id",post["id"]).execute()
                save_post(post["user_id"],p,post["text"],str(result),None,"posted")
                published += 1
            except Exception as e:
                supa.table("scheduled_posts").update({"status":"failed","error_msg":str(e)[:200]}).eq("id",post["id"]).execute()
                failed += 1
        return jsonify({"published": published, "failed": failed, "total": len(due.data or [])})
    except Exception as e:
        return jsonify({"error": str(e)}),500


# ════════════════════════════════════════════════════════════════════════════════
# ANALYTICS
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/analytics/summary", methods=["GET","OPTIONS"])
def analytics_summary():
    if request.method=="OPTIONS": return jsonify({}),200
    user_id = request.args.get("user_id","")
    if not user_id or not supa: return jsonify({"error":"Missing user_id"}),400
    try:
        # Posts per platform
        posts_res = supa.table("posts").select("platform,status,created_at").eq("user_id",user_id).execute()
        posts = posts_res.data or []

        # Clip usage
        usage_res = supa.table("clip_usage").select("created_at").eq("user_id",user_id).execute()
        usage = usage_res.data or []

        # Scheduled posts
        sched_res = supa.table("scheduled_posts").select("platform,status,scheduled_at").eq("user_id",user_id).execute()
        sched = sched_res.data or []

        # Aggregations
        platform_counts = {}
        for p in posts:
            platform_counts[p["platform"]] = platform_counts.get(p["platform"],0)+1

        # Last 30 days activity
        import datetime
        thirty_ago = (datetime.datetime.utcnow()-datetime.timedelta(days=30)).isoformat()
        recent_posts = [p for p in posts if p.get("created_at","") >= thirty_ago]

        # Daily clip trend (last 14 days)
        daily = {}
        for u in usage:
            day = u.get("created_at","")[:10]
            daily[day] = daily.get(day,0)+1

        # Pending scheduled
        pending_sched = [s for s in sched if s.get("status")=="pending"]

        return jsonify({
            "total_posts":    len(posts),
            "total_clips":    len(usage),
            "posts_30d":      len(recent_posts),
            "platform_breakdown": platform_counts,
            "daily_clips":    sorted([{"date":k,"count":v} for k,v in daily.items()], key=lambda x:x["date"]),
            "scheduled_pending": len(pending_sched),
            "scheduled_posts": sched[:20],
            "recent_posts":   posts[-10:][::-1],
            "plan":           get_user_plan(user_id),
            "clips_this_month": get_usage_this_month(user_id),
        })
    except Exception as e:
        return jsonify({"error": str(e)}),500

@app.route("/analytics/record-post", methods=["POST","OPTIONS"])
def record_post_analytics():
    if request.method=="OPTIONS": return jsonify({}),200
    data = request.get_json() or {}
    save_post(data.get("user_id",""), data.get("platform",""), data.get("content",""),
              data.get("post_id",""), None, data.get("status","posted"))
    return jsonify({"saved": True})


if __name__=="__main__":
    port=int(os.environ.get("PORT",5000))
    print(f"VidPost AI | port:{port}")
    app.run(host="0.0.0.0",port=port,debug=False)
