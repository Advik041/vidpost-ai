/**
 * /api/[...path].js — Vercel Edge Proxy
 *
 * ROOT CAUSE OF "Generate clip" 404/SyntaxError errors:
 * ─────────────────────────────────────────────────────
 * Vercel's `rewrites` in vercel.json work fine for GET requests but
 * STRIP THE REQUEST BODY on POST/PUT requests when the destination
 * is an external URL (Railway). This is a known Vercel limitation:
 * https://vercel.com/docs/project-configuration#rewrites
 *
 * The result:
 *   1. POST /api/clip → Railway receives an empty body
 *   2. Flask's request.get_json() returns None
 *   3. Flask returns a 400 or empty 200 with no JSON body
 *   4. Browser tries to parse '' as JSON → SyntaxError: Unexpected end of input
 *   5. pollJob() is never called, so /api/job/<id> returns 404
 *
 * FIX: Replace the static rewrite with this Vercel serverless function.
 * It acts as a proper reverse proxy that:
 *   - Forwards ALL HTTP methods (GET, POST, PUT, DELETE, OPTIONS)
 *   - Forwards the full request body
 *   - Forwards all relevant headers (Content-Type, Authorization, Range)
 *   - Returns the response body as a stream (no buffering for video)
 *   - Handles CORS preflight correctly
 *   - Sets proper streaming headers for video range requests
 */

const RAILWAY = process.env.RAILWAY_URL || 'https://vidpost-ai-production.up.railway.app';

export const config = {
  runtime: 'nodejs20.x',
  // Allow large video uploads (500MB) and long FFmpeg processing (10min)
  maxDuration: 600,
};

export default async function handler(req, res) {
  // Build target URL
  const pathSegments = req.query.path || [];
  const pathStr = Array.isArray(pathSegments) ? pathSegments.join('/') : pathSegments;
  const queryString = (() => {
    const q = { ...req.query };
    delete q.path;
    const s = new URLSearchParams(q).toString();
    return s ? '?' + s : '';
  })();
  const targetUrl = `${RAILWAY}/${pathStr}${queryString}`;

  // ── CORS preflight ────────────────────────────────────────────────────────
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,PUT,DELETE,OPTIONS,PATCH');
  res.setHeader('Access-Control-Allow-Headers',
    'Content-Type,Authorization,X-Cron-Secret,Range,Accept-Ranges,X-Requested-With');
  res.setHeader('Access-Control-Expose-Headers',
    'Content-Range,Accept-Ranges,Content-Length');

  if (req.method === 'OPTIONS') {
    res.status(200).end();
    return;
  }

  // ── Forward headers ───────────────────────────────────────────────────────
  const forwardHeaders = {};
  const PASS_THROUGH = [
    'content-type', 'authorization', 'x-cron-secret',
    'range', 'accept-ranges', 'accept', 'user-agent',
    'x-forwarded-for',
  ];
  for (const [key, value] of Object.entries(req.headers)) {
    if (PASS_THROUGH.includes(key.toLowerCase())) {
      forwardHeaders[key] = value;
    }
  }
  // Always set host to Railway's domain (not Vercel's)
  forwardHeaders['host'] = new URL(RAILWAY).host;

  // ── Read request body ─────────────────────────────────────────────────────
  // CRITICAL: We must consume the body stream here, not pass req directly,
  // because Vercel's runtime may already have partially buffered it.
  let body = undefined;
  if (!['GET', 'HEAD', 'OPTIONS'].includes(req.method)) {
    body = await new Promise((resolve, reject) => {
      const chunks = [];
      req.on('data', chunk => chunks.push(chunk));
      req.on('end', () => resolve(Buffer.concat(chunks)));
      req.on('error', reject);
    });
    if (body.length === 0) body = undefined;
  }

  // ── Proxy request to Railway ──────────────────────────────────────────────
  let railwayRes;
  try {
    railwayRes = await fetch(targetUrl, {
      method: req.method,
      headers: forwardHeaders,
      body: body,
      // Don't follow redirects — let browser handle them
      redirect: 'manual',
      // Timeout: 9 minutes (Vercel max is 10min for pro, 60s for hobby)
      signal: AbortSignal.timeout(540000),
    });
  } catch (err) {
    console.error(`Proxy error for ${req.method} ${targetUrl}:`, err.message);
    res.status(502).json({
      error: 'Backend unreachable',
      detail: err.message,
      target: targetUrl,
    });
    return;
  }

  // ── Copy response headers ─────────────────────────────────────────────────
  const BLOCKED_HEADERS = new Set([
    'transfer-encoding', 'connection', 'keep-alive',
    'upgrade', 'proxy-authenticate', 'proxy-authorization',
  ]);
  for (const [key, value] of railwayRes.headers.entries()) {
    if (!BLOCKED_HEADERS.has(key.toLowerCase())) {
      try { res.setHeader(key, value); } catch (_) {}
    }
  }
  // Always expose range headers for video streaming
  res.setHeader('Access-Control-Expose-Headers',
    'Content-Range,Accept-Ranges,Content-Length');

  res.status(railwayRes.status);

  // ── Stream response body ──────────────────────────────────────────────────
  // CRITICAL: Use streaming for video (don't buffer 200MB into RAM).
  // For JSON responses, buffering is fine and necessary for error handling.
  const contentType = railwayRes.headers.get('content-type') || '';
  const isVideo = contentType.includes('video/') ||
    railwayRes.headers.get('content-range') !== null ||
    railwayRes.status === 206;

  if (isVideo && railwayRes.body) {
    // Stream video directly without buffering
    const reader = railwayRes.body.getReader();
    const write = () => reader.read().then(({ done, value }) => {
      if (done) { res.end(); return; }
      res.write(value);
      write();
    }).catch(() => res.end());
    write();
  } else {
    // Buffer non-video responses (JSON, HTML, etc.)
    const responseBody = await railwayRes.arrayBuffer();
    res.end(Buffer.from(responseBody));
  }
}
