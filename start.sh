#!/bin/bash
set -e

echo "=== VidPost AI Startup ==="

# Install ffmpeg if not present (Railway containers have apt-get)
if ! command -v ffmpeg &> /dev/null; then
    echo "ffmpeg not found - installing via apt-get..."
    apt-get update -qq 2>/dev/null && apt-get install -y ffmpeg -qq 2>/dev/null || true
fi

# Install yt-dlp if not present
if ! command -v yt-dlp &> /dev/null; then
    echo "yt-dlp not found - installing..."
    pip install -U yt-dlp -q 2>/dev/null || true
fi

# Add nix paths if they exist
NIX_BIN=$(find /nix/store -name "ffmpeg" -type f 2>/dev/null | head -1)
if [ -n "$NIX_BIN" ]; then
    export PATH="$(dirname $NIX_BIN):$PATH"
    echo "ffmpeg found in nix: $NIX_BIN"
fi

for p in /root/.nix-profile/bin /nix/var/nix/profiles/default/bin /usr/local/bin; do
    [ -d "$p" ] && export PATH="$p:$PATH"
done

echo "ffmpeg: $(which ffmpeg 2>/dev/null || echo NOT FOUND)"
echo "yt-dlp: $(which yt-dlp 2>/dev/null || echo NOT FOUND)"
echo "ffmpeg version: $(ffmpeg -version 2>&1 | head -1 || echo N/A)"
echo "yt-dlp version: $(yt-dlp --version 2>/dev/null || echo N/A)"

echo "=== Starting Gunicorn ==="
exec gunicorn app:app \
    --bind 0.0.0.0:$PORT \
    --workers 2 \
    --worker-class gthread \
    --threads 4 \
    --timeout 600 \
    --graceful-timeout 60 \
    --keep-alive 5 \
    --max-requests 200 \
    --max-requests-jitter 30 \
    --access-logfile - \
    --error-logfile - \
    --log-level info
