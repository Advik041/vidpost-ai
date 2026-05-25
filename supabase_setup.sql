-- ============================================================
-- VidPost AI — Complete Supabase Database Setup
-- Run this entire script in Supabase SQL Editor
-- Dashboard → SQL Editor → New query → paste → Run
-- ============================================================


-- ── 1. platform_tokens ────────────────────────────────────────
-- Stores encrypted OAuth tokens for each connected platform
CREATE TABLE IF NOT EXISTS platform_tokens (
  id            UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id       TEXT NOT NULL,
  platform      TEXT NOT NULL,
  access_token  TEXT NOT NULL,
  refresh_token TEXT,
  expires_at    TIMESTAMPTZ,
  extra         JSONB DEFAULT '{}',
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  updated_at    TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(user_id, platform)
);

-- Index for fast token lookups
CREATE INDEX IF NOT EXISTS idx_platform_tokens_user_platform
  ON platform_tokens(user_id, platform);

-- Row Level Security
ALTER TABLE platform_tokens ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users manage own tokens" ON platform_tokens
  FOR ALL USING (auth.uid()::text = user_id);
-- Service role bypass (for backend)
CREATE POLICY "Service role full access tokens" ON platform_tokens
  FOR ALL USING (auth.role() = 'service_role');


-- ── 2. posts ─────────────────────────────────────────────────
-- Tracks every post published through VidPost AI
CREATE TABLE IF NOT EXISTS posts (
  id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id         TEXT NOT NULL,
  platform        TEXT NOT NULL,
  content         TEXT,
  platform_post_id TEXT,
  video_url       TEXT,
  status          TEXT DEFAULT 'posted',
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_posts_user_id ON posts(user_id);
CREATE INDEX IF NOT EXISTS idx_posts_created_at ON posts(created_at);

ALTER TABLE posts ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users see own posts" ON posts
  FOR ALL USING (auth.uid()::text = user_id);
CREATE POLICY "Service role full access posts" ON posts
  FOR ALL USING (auth.role() = 'service_role');


-- ── 3. clip_usage ─────────────────────────────────────────────
-- Tracks clip generation for usage limits per billing plan
CREATE TABLE IF NOT EXISTS clip_usage (
  id         UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id    TEXT NOT NULL,
  job_id     TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_clip_usage_user_id ON clip_usage(user_id);
CREATE INDEX IF NOT EXISTS idx_clip_usage_created_at ON clip_usage(created_at);

ALTER TABLE clip_usage ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users see own usage" ON clip_usage
  FOR ALL USING (auth.uid()::text = user_id);
CREATE POLICY "Service role full access usage" ON clip_usage
  FOR ALL USING (auth.role() = 'service_role');


-- ── 4. scheduled_posts ────────────────────────────────────────
-- Queue of posts scheduled for future publishing
CREATE TABLE IF NOT EXISTS scheduled_posts (
  id               UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id          TEXT NOT NULL,
  platform         TEXT NOT NULL,
  text             TEXT,
  video_url        TEXT,
  scheduled_at     TIMESTAMPTZ NOT NULL,
  status           TEXT DEFAULT 'pending',
  platform_post_id TEXT,
  error_msg        TEXT,
  posted_at        TIMESTAMPTZ,
  created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_scheduled_posts_user_id ON scheduled_posts(user_id);
CREATE INDEX IF NOT EXISTS idx_scheduled_posts_status ON scheduled_posts(status);
CREATE INDEX IF NOT EXISTS idx_scheduled_posts_scheduled_at ON scheduled_posts(scheduled_at);

ALTER TABLE scheduled_posts ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users manage own scheduled posts" ON scheduled_posts
  FOR ALL USING (auth.uid()::text = user_id);
CREATE POLICY "Service role full access scheduled" ON scheduled_posts
  FOR ALL USING (auth.role() = 'service_role');


-- ── 5. subscriptions ─────────────────────────────────────────
-- Billing plan per user, synced from Stripe webhooks
CREATE TABLE IF NOT EXISTS subscriptions (
  id                  UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id             TEXT NOT NULL UNIQUE,
  stripe_customer_id  TEXT,
  plan                TEXT DEFAULT 'free',
  status              TEXT DEFAULT 'active',
  current_period_end  TIMESTAMPTZ,
  created_at          TIMESTAMPTZ DEFAULT NOW(),
  updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_user_id ON subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_stripe_customer ON subscriptions(stripe_customer_id);

ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users see own subscription" ON subscriptions
  FOR ALL USING (auth.uid()::text = user_id);
CREATE POLICY "Service role full access subscriptions" ON subscriptions
  FOR ALL USING (auth.role() = 'service_role');


-- ── 6. Auto-update timestamps ────────────────────────────────
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trigger_platform_tokens_updated
  BEFORE UPDATE ON platform_tokens
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE OR REPLACE TRIGGER trigger_subscriptions_updated
  BEFORE UPDATE ON subscriptions
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();


-- ── 7. Verify all tables created ─────────────────────────────
SELECT table_name, 
       (SELECT COUNT(*) FROM information_schema.columns 
        WHERE table_name = t.table_name 
        AND table_schema = 'public') AS column_count
FROM information_schema.tables t
WHERE table_schema = 'public'
  AND table_name IN (
    'platform_tokens','posts','clip_usage',
    'scheduled_posts','subscriptions'
  )
ORDER BY table_name;

-- ✅ You should see 5 rows above, one per table.
-- If any are missing, run this script again — it uses
-- IF NOT EXISTS so it is safe to re-run anytime.
