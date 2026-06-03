#!/bin/bash
# Find and export ffmpeg location before starting the app
export PATH="/root/.nix-profile/bin:/nix/var/nix/profiles/default/bin:$PATH"

# Find ffmpeg in nix store and add to PATH
NIX_FFMPEG=$(find /nix/store -name "ffmpeg" -type f 2>/dev/null | head -1)
if [ -n "$NIX_FFMPEG" ]; then
    export PATH="$(dirname $NIX_FFMPEG):$PATH"
    echo "ffmpeg found: $NIX_FFMPEG"
else
    echo "WARNING: ffmpeg not found in nix store"
fi

# Verify
which ffmpeg && ffmpeg -version | head -1 || echo "ERROR: ffmpeg still not in PATH"
which yt-dlp && yt-dlp --version || echo "WARNING: yt-dlp not found"

echo "Starting gunicorn..."
exec gunicorn app:app     --bind 0.0.0.0:$PORT     --workers 2     --worker-class gthread     --threads 4     --timeout 600     --graceful-timeout 60     --keep-alive 5     --max-requests 200     --max-requests-jitter 30     --access-logfile -     --error-logfile -     --log-level info
