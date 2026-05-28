/**
 * /api/[...path].js — Vercel Serverless Proxy (CommonJS)
 *
 * ROOT CAUSE OF 404: Two compounding issues:
 *
 * 1. vercel.json used `routes[]` which overrides Vercel's filesystem routing,
 *    causing it to never discover this file as an API function.
 *    Fixed: removed routes[], using rewrites[] for SPA fallback only.
 *
 * 2. Previous version used ES module syntax (export default / export const config)
 *    without a package.json declaring "type":"module". Vercel's Node runtime
 *    defaults to CommonJS — ES module syntax silently fails with NOT_FOUND.
 *    Fixed: rewritten as CommonJS (module.exports).
 *
 * How it works:
 *   Browser → POST /api/analyse
 *   Vercel filesystem → finds api/[...path].js → runs this handler
 *   This handler → forwards full request (body + headers) → Railway
 *   Railway → processes → returns response
 *   This handler → streams response back to browser
 */

const RAILWAY = process.env.RAILWAY_URL || 'https://vidpost-ai-production.up.railway.app';

// Vercel config: allow up to 5-minute execution for FFmpeg jobs
module.exports.config = {
  api: {
    bodyParser: false,        // CRITICAL: we read raw body ourselves
    responseLimit: false,     // allow large video responses
    externalResolver: true,   // suppress Vercel's "no response" warning
  },
  maxDuration: 300,           // 5 minutes (Vercel Pro: up to 900s)
};

module.exports.default = async function handler(req, res) {
  // ── Build target URL ──────────────────────────────────────────────────────
  const pathSegments = req.query.path;
  const pathStr = Array.isArray(pathSegments)
    ? pathSegments.join('/')
    : (pathSegments || '');

  // Rebuild query string without the 'path' param
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(req.query || {})) {
    if (k !== 'path') qs.append(k, v);
  }
  const queryPart = qs.toString() ? '?' + qs.toString() : '';
  const targetUrl = `${RAILWAY}/${pathStr}${queryPart}`;

  // ── CORS ──────────────────────────────────────────────────────────────────
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,PUT,DELETE,OPTIONS,PATCH');
  res.setHeader('Access-Control-Allow-Headers',
    'Content-Type,Authorization,X-Cron-Secret,Range,Accept-Ranges');
  res.setHeader('Access-Control-Expose-Headers',
    'Content-Range,Accept-Ranges,Content-Length');

  if (req.method === 'OPTIONS') {
    res.status(200).end();
    return;
  }

  // ── Read raw request body ─────────────────────────────────────────────────
  // bodyParser:false means req is a raw stream — must collect manually
  let bodyBuffer = null;
  if (!['GET', 'HEAD', 'OPTIONS'].includes(req.method)) {
    bodyBuffer = await new Promise((resolve, reject) => {
      const chunks = [];
      req.on('data', chunk => chunks.push(chunk));
      req.on('end', () => resolve(Buffer.concat(chunks)));
      req.on('error', reject);
    });
  }

  // ── Build forwarded headers ───────────────────────────────────────────────
  const FORWARD = new Set([
    'content-type', 'authorization', 'x-cron-secret',
    'range', 'accept', 'accept-encoding', 'user-agent',
    'x-forwarded-for',
  ]);
  const forwardHeaders = { host: new URL(RAILWAY).host };
  for (const [k, v] of Object.entries(req.headers || {})) {
    if (FORWARD.has(k.toLowerCase())) forwardHeaders[k] = v;
  }
  if (bodyBuffer && bodyBuffer.length > 0) {
    forwardHeaders['content-length'] = String(bodyBuffer.length);
  }

  // ── Proxy to Railway ──────────────────────────────────────────────────────
  let upstream;
  try {
    upstream = await fetch(targetUrl, {
      method:  req.method,
      headers: forwardHeaders,
      body:    bodyBuffer && bodyBuffer.length > 0 ? bodyBuffer : undefined,
      redirect: 'manual',
      signal:  AbortSignal.timeout(290000), // 4m50s — just under Vercel's limit
    });
  } catch (err) {
    console.error(`[proxy] ${req.method} ${targetUrl} →`, err.message);
    res.status(502).json({
      error:  'Backend unreachable',
      detail:  err.message,
      target:  targetUrl,
    });
    return;
  }

  // ── Copy response headers ─────────────────────────────────────────────────
  const BLOCKED = new Set([
    'transfer-encoding', 'connection', 'keep-alive',
    'upgrade', 'trailer',
  ]);
  for (const [k, v] of upstream.headers.entries()) {
    if (!BLOCKED.has(k.toLowerCase())) {
      try { res.setHeader(k, v); } catch (_) {}
    }
  }

  res.status(upstream.status);

  // ── Stream body back ──────────────────────────────────────────────────────
  const ct = upstream.headers.get('content-type') || '';
  const isVideo = ct.startsWith('video/') || upstream.status === 206;

  if (isVideo && upstream.body) {
    // Stream video without buffering into RAM
    const reader = upstream.body.getReader();
    const pump = () => reader.read().then(({ done, value }) => {
      if (done) { res.end(); return; }
      res.write(value);
      return pump();
    }).catch(() => res.end());
    await pump();
  } else {
    // Buffer JSON / HTML responses
    const buf = await upstream.arrayBuffer();
    res.end(Buffer.from(buf));
  }
};
