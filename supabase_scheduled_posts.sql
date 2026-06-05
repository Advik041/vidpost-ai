-- ─────────────────────────────────────────────────────────────────────────────
-- VidPost AI — scheduled_posts table
-- Run this in: Supabase Dashboard → SQL Editor → New query → Run
-- ─────────────────────────────────────────────────────────────────────────────

create table if not exists public.scheduled_posts (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references auth.users(id) on delete cascade,
  platform      text not null,
  text          text not null default '',
  video_url     text not null default '',
  scheduled_at  timestamptz not null,
  status        text not null default 'pending'
                  check (status in ('pending','posted','failed','cancelled')),
  platform_post_id text,
  error_msg     text,
  posted_at     timestamptz,
  created_at    timestamptz not null default now()
);

-- Index for the cron query: pending posts due now
create index if not exists idx_sched_pending
  on public.scheduled_posts (status, scheduled_at)
  where status = 'pending';

-- Index for per-user list
create index if not exists idx_sched_user
  on public.scheduled_posts (user_id, scheduled_at desc);

-- Row Level Security: users can only see their own posts
alter table public.scheduled_posts enable row level security;

create policy "Users see own scheduled posts"
  on public.scheduled_posts for select
  using (auth.uid() = user_id);

create policy "Users create own scheduled posts"
  on public.scheduled_posts for insert
  with check (auth.uid() = user_id);

create policy "Users delete own scheduled posts"
  on public.scheduled_posts for delete
  using (auth.uid() = user_id);

-- Service role can do everything (needed by the cron runner)
create policy "Service role full access"
  on public.scheduled_posts for all
  using (auth.role() = 'service_role');
