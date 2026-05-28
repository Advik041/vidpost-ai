const RAILWAY = process.env.RAILWAY_URL || 'https://vidpost-ai-production.up.railway.app';

async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,PUT,DELETE,OPTIONS,PATCH');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type,Authorization,X-Cron-Secret,Range,Accept-Ranges');
  res.setHeader('Access-Control-Expose-Headers', 'Content-Range,Accept-Ranges,Content-Length');
  if (req.method === 'OPTIONS') { res.status(200).end(); return; }

  // req.query.slug is array of path segments after /api/
  // e.g. /api/analyse -> ['analyse']
  // e.g. /api/job/abc123 -> ['job','abc123']
  const slug = req.query.slug || [];
  const pathStr = Array.isArray(slug) ? slug.join('/') : slug;
  
  // Preserve query string (minus the slug param)
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(req.query || {})) {
    if (k !== 'slug') qs.append(k, v);
  }
  const qsPart = qs.toString() ? '?' + qs.toString() : '';
  const targetUrl = RAILWAY + '/' + pathStr + qsPart;

  // Read raw body
  let body = null;
  if (!['GET','HEAD','OPTIONS'].includes(req.method)) {
    body = await new Promise((resolve, reject) => {
      const chunks = [];
      req.on('data', c => chunks.push(c));
      req.on('end', () => resolve(Buffer.concat(chunks)));
      req.on('error', reject);
    });
  }

  // Forward headers
  const PASS = new Set(['content-type','authorization','x-cron-secret','range','accept','user-agent']);
  const fwd = { host: new URL(RAILWAY).host };
  for (const [k, v] of Object.entries(req.headers || {})) {
    if (PASS.has(k.toLowerCase())) fwd[k] = v;
  }
  if (body && body.length > 0) fwd['content-length'] = String(body.length);

  let upstream;
  try {
    upstream = await fetch(targetUrl, {
      method: req.method,
      headers: fwd,
      body: body && body.length > 0 ? body : undefined,
      redirect: 'manual',
      signal: AbortSignal.timeout(280000),
    });
  } catch (err) {
    console.error('[proxy]', req.method, targetUrl, err.message);
    return res.status(502).json({ error: 'Backend unreachable', detail: err.message });
  }

  const BLOCK = new Set(['transfer-encoding','connection','keep-alive','upgrade','trailer']);
  for (const [k, v] of upstream.headers.entries()) {
    if (!BLOCK.has(k.toLowerCase())) { try { res.setHeader(k, v); } catch (_) {} }
  }
  res.status(upstream.status);

  const ct = upstream.headers.get('content-type') || '';
  if ((ct.startsWith('video/') || upstream.status === 206) && upstream.body) {
    const reader = upstream.body.getReader();
    const pump = () => reader.read().then(({ done, value }) => {
      if (done) { res.end(); return; }
      res.write(value); return pump();
    }).catch(() => res.end());
    await pump();
  } else {
    res.end(Buffer.from(await upstream.arrayBuffer()));
  }
}

handler.config = {
  api: { bodyParser: false, responseLimit: false, externalResolver: true },
  maxDuration: 300,
};

module.exports = handler;
