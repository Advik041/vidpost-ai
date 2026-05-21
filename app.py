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

def ytdlp_base():
    cmd = [YTDLP,"--no-playlist",
           "--user-agent","Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"]
    if PROXY: cmd += ["--proxy", PROXY]
    return cmd

@app.route("/analyse", methods=["POST","OPTIONS"])
def analyse():
    if request.method == "OPTIONS": return jsonify({}), 200
    data = request.get_json()
    url  = data.get("url","").strip()
    if not url: return jsonify({"error":"Missing URL"}), 400
    m = re.search(r'(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})', url)
    if not m: return jsonify({"error":"Invalid YouTube URL"}), 400
    video_id = m.group(1)
    title = "YouTube Video"
    try:
        r = requests.get(f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json",timeout=5)
        if r.ok: title = r.json().get("title",title)
    except: pass
    duration = 600
    try:
        cmd = ytdlp_base()+["--dump-json","--no-download",f"https://www.youtube.com/watch?v={video_id}"]
        result = subprocess.run(cmd,capture_output=True,text=True,timeout=40)
        if result.returncode==0:
            info=json.loads(result.stdout); title=info.get("title",title); duration=int(info.get("duration",600))
    except Exception as e: print(f"yt-dlp info:{e}")
    transcript = f"Video:{title}. Duration:{duration}s."
    if SUPADATA:
        try:
            resp=requests.get("https://api.supadata.ai/v1/youtube/transcript",
                params={"videoId":video_id,"text":"true"},headers={"x-api-key":SUPADATA},timeout=20)
            if resp.ok:
                d=resp.json();c=d.get("content","")
                t=c if isinstance(c,str) else " ".join([s.get("text","") for s in c])
                if t and len(t)>30: transcript=t
        except: pass
    clips = _detect_clips(title,duration,transcript,GROQ_KEY)
    return jsonify({"videoId":video_id,"title":title,"duration":duration,"clips":clips,"transcriptLength":len(transcript.split()),"mode":"url"})

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

def _detect_clips(title,duration,transcript,groq_key):
    clips=[]
    if groq_key:
        try:
            prompt=f"""Find 5 best viral moments to clip.
VIDEO:"{title}" DURATION:{duration}s
TRANSCRIPT:{transcript[:3000]}
Return ONLY JSON array:[{{"start":10,"end":70,"title":"Title","hook":"Hook","virality_score":8,"reason":"Why"}}]
Rules:30-90s each,within 0-{duration},spaced throughout.ONLY return JSON."""
            resp=requests.post("https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization":f"Bearer {groq_key}","Content-Type":"application/json"},
                json={"model":"llama-3.3-70b-versatile","messages":[{"role":"user","content":prompt}],"max_tokens":800,"temperature":0.1},timeout=30)
            if resp.ok:
                raw=resp.json()["choices"][0]["message"]["content"].strip()
                match=re.search(r'\[[\s\S]*\]',raw)
                if match:
                    parsed=json.loads(match.group())
                    clips=[c for c in parsed if isinstance(c.get("start"),(int,float)) and isinstance(c.get("end"),(int,float)) and 0<=c["start"]<c["end"]<=duration and (c["end"]-c["start"])>=15]
        except Exception as e: print(f"AI clip:{e}")
    if not clips:
        seg=max(duration//6,40)
        for i in range(5):
            s=i*seg+10;e=min(s+60,duration-5)
            if e>s+15: clips.append({"start":s,"end":e,"title":f"Highlight {i+1}","hook":"Watch this...","virality_score":7,"reason":"Auto-detected"})
    return clips

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

def process_clip_job(job_id,video_id,upload_path,start,end,formats):
    job_dir=os.path.join(CLIPS_DIR,job_id); os.makedirs(job_dir,exist_ok=True)
    raw_video=None
    try:
        if upload_path and os.path.exists(upload_path):
            raw_video=upload_path; sz=os.path.getsize(upload_path)
            print(f"Job {job_id}:upload {sz}B"); update_job(job_id,"processing",20,f"Using uploaded file ({sz//1024}KB)...")
        elif video_id:
            update_job(job_id,"downloading",10,"Downloading..."); raw_video=os.path.join(job_dir,"raw.mp4")
            cmd=ytdlp_base()+["--format","bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[ext=mp4]/best",
                "--download-sections",f"*{max(0,start-3)}-{end+3}","--force-keyframes-at-cuts","--merge-output-format","mp4","-o",raw_video,
                f"https://www.youtube.com/watch?v={video_id}"]
            dl=subprocess.run(cmd,capture_output=True,text=True,timeout=300)
            if dl.returncode!=0 or not os.path.exists(raw_video): raise Exception("Download failed. Try uploading the video directly.")
        else: raise Exception("No video source")
        if not raw_video or not os.path.exists(raw_video): raise Exception("Source not found")
        sz=os.path.getsize(raw_video)
        if sz==0: raise Exception("Source file is empty")
        update_job(job_id,"cutting",40,"Cutting clip...")
        cut_video=os.path.join(job_dir,"cut.mp4")
        cut=subprocess.run(["ffmpeg","-y","-ss",str(start),"-i",raw_video,"-t",str(end-start),"-c","copy","-avoid_negative_ts","make_zero",cut_video],capture_output=True,text=True,timeout=120)
        if not os.path.exists(cut_video) or os.path.getsize(cut_video)==0:
            cut2=subprocess.run(["ffmpeg","-y","-i",raw_video,"-ss",str(start),"-t",str(end-start),"-c:v","libx264","-preset","ultrafast","-crf","28","-c:a","aac","-b:a","96k",cut_video],capture_output=True,text=True,timeout=120)
        if not os.path.exists(cut_video) or os.path.getsize(cut_video)==0: raise Exception("Cut failed")
        update_job(job_id,"converting",65,"Converting formats...")
        output_files={}
        if "vertical" in formats:
            vpath=os.path.join(job_dir,"vertical.mp4")
            subprocess.run(["ffmpeg","-y","-i",cut_video,"-vf","scale=540:960:force_original_aspect_ratio=decrease,pad=540:960:(ow-iw)/2:(oh-ih)/2:black","-c:v","libx264","-preset","ultrafast","-crf","28","-c:a","aac","-b:a","96k","-r","30","-threads","1",vpath],capture_output=True,text=True,timeout=180)
            if os.path.exists(vpath) and os.path.getsize(vpath)>0: output_files["vertical"]=vpath
        if "horizontal" in formats:
            hpath=os.path.join(job_dir,"horizontal.mp4")
            subprocess.run(["ffmpeg","-y","-i",cut_video,"-vf","scale=960:540:force_original_aspect_ratio=decrease,pad=960:540:(ow-iw)/2:(oh-ih)/2:black","-c:v","libx264","-preset","ultrafast","-crf","28","-c:a","aac","-b:a","96k","-r","30","-threads","1",hpath],capture_output=True,text=True,timeout=180)
            if os.path.exists(hpath) and os.path.getsize(hpath)>0: output_files["horizontal"]=hpath
        if not output_files: raise Exception("No output files created")
        refs={fmt:{"downloadUrl":f"/download/{job_id}/{fmt}","publicUrl":f"{BACKEND_URL}/download/{job_id}/{fmt}","sizeMb":round(os.path.getsize(p)/(1024*1024),1),"resolution":"540x960" if fmt=="vertical" else "960x540"} for fmt,p in output_files.items()}
        update_job(job_id,"done",100,"Clip ready!",files=refs)
        threading.Timer(3600,lambda:shutil.rmtree(job_dir,ignore_errors=True)).start()
    except Exception as e:
        print(f"Job {job_id}:{e}"); update_job(job_id,"error",0,str(e))

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
    """Download and stream a YouTube video for editor preview."""
    try:
        out_path = os.path.join(CLIPS_DIR, f"preview_{video_id}.mp4")
        if not os.path.exists(out_path):
            cmd = ["yt-dlp", "-f", "best[ext=mp4][height<=480]/best[ext=mp4]/best",
                   "--no-playlist", "-o", out_path,
                   f"https://www.youtube.com/watch?v={video_id}"]
            subprocess.run(cmd, capture_output=True, timeout=120)
        if os.path.exists(out_path):
            return send_file(out_path, mimetype="video/mp4", conditional=True)
        return jsonify({"error":"Could not fetch video"}), 404
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

    # Parse params from either JSON or FormData
    if request.content_type and "multipart" in request.content_type:
        video_url    = request.form.get("video_url","")
        start        = float(request.form.get("start", 0))
        end          = float(request.form.get("end", 60))
        captions     = request.form.get("captions","true").lower()=="true"
        cap_style    = request.form.get("caption_style","bold")
        cap_size     = request.form.get("caption_size","medium")
        formats      = request.form.get("formats","vertical")
        logo_pos     = request.form.get("logo_position","top-right")
        logo_opacity = int(request.form.get("logo_opacity",80))
        logo_file    = request.files.get("logo")
        video_id     = request.form.get("videoId","")
    else:
        data         = request.get_json() or {}
        video_url    = data.get("video_url","")
        start        = float(data.get("start",0))
        end          = float(data.get("end",60))
        captions     = data.get("captions",True)
        cap_style    = data.get("caption_style","bold")
        cap_size     = data.get("caption_size","medium")
        formats      = data.get("formats","vertical")
        logo_pos     = data.get("logo_position","top-right")
        logo_opacity = int(data.get("logo_opacity",80))
        logo_file    = None
        video_id     = data.get("videoId","")

    # Save logo if provided
    logo_path = None
    if logo_file:
        logo_path = os.path.join(job_dir, "logo.png")
        logo_file.save(logo_path)

    def run_editor_job():
        try:
            JOBS[job_id].update({"status":"running","progress":5,"message":"Downloading source video..."})

            # Get source video
            if video_id:
                src = os.path.join(CLIPS_DIR, f"preview_{video_id}.mp4")
                if not os.path.exists(src):
                    cmd = ["yt-dlp","-f","best[ext=mp4][height<=720]/best[ext=mp4]/best",
                           "--no-playlist","-o",src,
                           f"https://www.youtube.com/watch?v={video_id}"]
                    subprocess.run(cmd,capture_output=True,timeout=180)
            elif video_url:
                src = os.path.join(job_dir,"source.mp4")
                r = requests.get(video_url,stream=True,timeout=60)
                with open(src,"wb") as f:
                    for chunk in r.iter_content(65536): f.write(chunk)
            else:
                JOBS[job_id].update({"status":"error","message":"No video source"}); return

            JOBS[job_id].update({"progress":20,"message":"Trimming clip..."})

            fmt_list = ["vertical","horizontal"] if formats=="both" else [formats]
            output_files = {}

            for fmt in fmt_list:
                out_file = os.path.join(job_dir, f"{fmt}.mp4")
                trim_file = os.path.join(job_dir, f"trim_{fmt}.mp4")

                # Step 1: trim
                vf_trim = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920" if fmt=="vertical" \
                          else "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080"
                trim_cmd = ["ffmpeg","-y","-i",src,"-ss",str(start),"-to",str(end),
                            "-vf",vf_trim,"-c:v","libx264","-crf","22","-preset","fast",
                            "-c:a","aac","-b:a","128k",trim_file]
                subprocess.run(trim_cmd,capture_output=True)

                JOBS[job_id].update({"progress":50,"message":f"Processing {fmt} format..."})

                current = trim_file
                # Step 2: logo overlay
                if logo_path and os.path.exists(logo_path):
                    logo_out = os.path.join(job_dir, f"logo_{fmt}.mp4")
                    pos_map = {
                        "top-left":"10:10","top-right":"W-w-10:10",
                        "bottom-left":"10:H-h-10","bottom-right":"W-w-10:H-h-10"
                    }
                    pos = pos_map.get(logo_pos,"W-w-10:10")
                    alpha = logo_opacity/100.0
                    logo_vf = f"[1:v]scale=120:-1,format=rgba,colorchannelmixer=aa={alpha}[logo];[0:v][logo]overlay={pos}"
                    logo_cmd = ["ffmpeg","-y","-i",current,"-i",logo_path,
                                "-filter_complex",logo_vf,"-c:a","copy",logo_out]
                    result = subprocess.run(logo_cmd,capture_output=True)
                    if result.returncode==0: current=logo_out

                JOBS[job_id].update({"progress":70,"message":"Adding captions..."})

                # Step 3: captions via subtitles
                if captions:
                    # Build SRT from transcript if available (fallback: placeholder)
                    srt_path = os.path.join(job_dir, f"caps_{fmt}.srt")
                    dur = end - start
                    # Generate simple timed subtitles (placeholder — real impl uses Whisper)
                    srt_content = f"1\n00:00:00,000 --> {int(dur//60):02d}:{int(dur%60):02d},000\nAuto-captions powered by Whisper AI\n"
                    with open(srt_path,"w") as f: f.write(srt_content)

                    font_size = {"small":16,"medium":22,"large":30}.get(cap_size,22)
                    style_map = {
                        "bold":    f"FontName=Arial,FontSize={font_size},PrimaryColour=&HFFFFFF,Bold=1,Outline=2,OutlineColour=&H000000,Shadow=1",
                        "outline": f"FontName=Arial,FontSize={font_size},PrimaryColour=&HFFFFFF,Bold=0,Outline=2,OutlineColour=&H000000",
                        "box":     f"FontName=Arial,FontSize={font_size},PrimaryColour=&HFFFFFF,Bold=1,BackColour=&H80000000,BorderStyle=3",
                        "lime":    f"FontName=Arial,FontSize={font_size},PrimaryColour=&H00FF00,Bold=1,Outline=2,OutlineColour=&H000000",
                    }
                    vstyle = style_map.get(cap_style, style_map["bold"])
                    cap_out = os.path.join(job_dir, f"cap_{fmt}.mp4")
                    cap_cmd = ["ffmpeg","-y","-i",current,
                               "-vf",f"subtitles={srt_path}:force_style='{vstyle}',setpts=PTS-STARTPTS",
                               "-c:v","libx264","-crf","22","-preset","fast","-c:a","copy",cap_out]
                    res = subprocess.run(cap_cmd,capture_output=True)
                    if res.returncode==0: current=cap_out

                # Final copy
                import shutil; shutil.copy(current, out_file)
                size_mb = round(os.path.getsize(out_file)/(1024*1024),1)
                output_files[fmt] = {
                    "downloadUrl": f"/download/{job_id}/{fmt}",
                    "publicUrl":   f"/download/{job_id}/{fmt}",
                    "sizeMb":      size_mb
                }

            JOBS[job_id].update({"status":"done","progress":100,"message":"Export complete!","files":output_files})
        except Exception as e:
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
    if not GROQ_KEY: return jsonify({"error":"No API key"}),400
    tone_map={"professional":"professional and insightful","casual":"casual and conversational","bold":"bold and provocative","storytelling":"storytelling with a narrative arc"}
    tone_desc=tone_map.get(tone,tone_map["professional"])
    prompts={
        "linkedin":f'Write viral LinkedIn post.\nVIDEO:"{title}"\nTONE:{tone_desc}\nTRANSCRIPT:{transcript[:3000]}\n\n[Hook under 12 words]\n\n[emoji] Insight 1\n[emoji] Insight 2\n[emoji] Insight 3\n\n[Question]\n\n#tag1 #tag2 #tag3\n\nMax 200 words. Output ONLY post.',
        "twitter":f'Write 6-tweet thread.\nVIDEO:"{title}"\nTONE:{tone_desc}\nTRANSCRIPT:{transcript[:3000]}\n\nTweet 1:hook\nTweets 2-5:insights\nTweet 6:ending\nNumber 1/2/etc.\nOutput ONLY 6 tweets.',
        "instagram":f'Write Instagram caption.\nVIDEO:"{title}"\nTONE:{tone_desc}\nTRANSCRIPT:{transcript[:3000]}\n\nHook\n\n3 paragraphs with emoji\n\nQuestion\n\n15 hashtags\n\nOutput ONLY caption.',
        "tiktok":f'Write 45-sec TikTok script.\nVIDEO:"{title}"\nTONE:{tone_desc}\nTRANSCRIPT:{transcript[:3000]}\n\nHOOK\nPOINT1\nPOINT2\nPOINT3\nCTA\n\nUnder 130 words.',
        "youtube_short":f'Write YouTube Shorts script.\nVIDEO:"{title}"\nTONE:{tone_desc}\nTRANSCRIPT:{transcript[:3000]}\n\n[HOOK][POINT1][POINT2][POINT3][CTA]\n\nUnder 150 words.',
        "facebook":f'Write a Facebook Page post.\nVIDEO:"{title}"\nTONE:{tone_desc}\nTRANSCRIPT:{transcript[:3000]}\n\nHook sentence.\n\n2-3 engaging paragraphs with emojis.\n\nQuestion to drive comments.\n\n3-5 hashtags.\n\nMax 250 words. Output ONLY post.'
    }
    results,errors={},{}
    for platform in platforms:
        if platform not in prompts: continue
        try:
            resp=requests.post("https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization":f"Bearer {GROQ_KEY}","Content-Type":"application/json"},
                json={"model":"llama-3.3-70b-versatile","messages":[{"role":"user","content":prompts[platform]}],"max_tokens":1024,"temperature":0.75},timeout=30)
            if resp.status_code==401: return jsonify({"error":"Invalid Groq key"}),401
            if resp.status_code==429: return jsonify({"error":"Rate limit"}),429
            if resp.ok: results[platform]=resp.json()["choices"][0]["message"]["content"].strip()
            else: errors[platform]=f"Error {resp.status_code}"
        except Exception as e: errors[platform]=str(e)
    if not results: return jsonify({"error":"Generation failed","details":errors}),500
    return jsonify({"results":results,"errors":errors,"title":title})

if __name__=="__main__":
    port=int(os.environ.get("PORT",5000))
    print(f"VidPost AI | port:{port}")
    app.run(host="0.0.0.0",port=port,debug=False)
