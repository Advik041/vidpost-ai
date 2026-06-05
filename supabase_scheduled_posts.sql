-- ─────────────────────────────────────────────────────────────────────────────
-- VidPost AI — scheduled_posts table
-- FIX: Cast auth.uid() and user_id to text for RLS policy compatibility
-- Run in: Supabase Dashboard → SQL Editor → New query → Run
-- ─────────────────────────────────────────────────────────────────────────────

create table if not exists public.scheduled_posts (
  id               uuid primary key default gen_random_uuid(),
  user_id          text not null,
  platform         text not null,
  text             text not null default '',
  video_url        text not null default '',
  scheduled_at     timestamptz not null,
  status           text not null default 'pending'
                     check (status in ('pending','posted','failed','cancelled')),
  platform_post_id text,
  error_msg        text,
  posted_at        timestamptz,
  created_at       timestamptz not null default now()
);

-- Index for the cron query: pending posts due now
create index if not exists idx_sched_pending
  on public.scheduled_posts (status, scheduled_at)
  where status = 'pending';

-- Index for per-user list
create index if not exists idx_sched_user
  on public.scheduled_posts (user_id, scheduled_at desc);

-- Row Level Security
alter table public.scheduled_posts enable row level security;

create policy "Users see own scheduled posts"
  on public.scheduled_posts for select
  using (auth.uid()::text = user_id);

create policy "Users create own scheduled posts"
  on public.scheduled_posts for insert
  with check (auth.uid()::text = user_id);

create policy "Users delete own scheduled posts"
  on public.scheduled_posts for delete
  using (auth.uid()::text = user_id);

create policy "Service role full access"
  on public.scheduled_posts for all
  using (auth.role() = 'service_role');
