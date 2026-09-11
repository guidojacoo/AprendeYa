-- fantasybot · serverless state
--
-- Run this once against a fresh Supabase project (SQL Editor, or
-- `python scripts/setup-db.py --print` and paste). It is idempotent: re-running
-- it is safe and changes nothing.
--
-- Two ideas run through the whole schema:
--
--   scope   every row is namespaced, so one free Supabase project can run
--           several teams or leagues without them ever seeing each other.
--   RLS on, no policies
--           the anon key can read NOTHING. Only the service role (which lives
--           in Vercel's server-side env, never in a browser) touches this data.
--           The dashboard reads through our own API, on purpose.

create extension if not exists "pgcrypto";

-- ---------------------------------------------------------------------------
-- agent_state — the old .state/*.json blobs (snapshot, tasks, reminders, bids…)
-- ---------------------------------------------------------------------------
create table if not exists public.agent_state (
  scope       text        not null default 'default',
  key         text        not null,
  value       jsonb,
  updated_at  timestamptz not null default now(),
  primary key (scope, key)
);

-- ---------------------------------------------------------------------------
-- cache_entries — TTL'd scrape results (was .cache/)
-- A Vercel function starts with an empty disk, so without this every tick would
-- re-scrape futbolfantasy and get itself rate-limited.
-- ---------------------------------------------------------------------------
create table if not exists public.cache_entries (
  scope       text        not null default 'default',
  key         text        not null,
  value       jsonb,
  expires_at  timestamptz not null,
  primary key (scope, key)
);
create index if not exists cache_entries_expiry on public.cache_entries (expires_at);

-- ---------------------------------------------------------------------------
-- events — the action trace (was .state/events.jsonl)
-- ---------------------------------------------------------------------------
create table if not exists public.events (
  id      bigserial   primary key,
  scope   text        not null default 'default',
  ts      timestamptz not null default now(),
  run_id  text,
  kind    text,
  title   text,
  status  text        not null default 'ok',
  detail  jsonb
);
create index if not exists events_scope_ts on public.events (scope, ts desc, id desc);

-- ---------------------------------------------------------------------------
-- market_snapshots — one day of official LaLiga values (was .state/value_history/)
-- `player_values`, not `values`: VALUES is a reserved word and quoting it through
-- PostgREST is a papercut waiting to happen.
-- ---------------------------------------------------------------------------
create table if not exists public.market_snapshots (
  scope          text        not null default 'default',
  day            date        not null,
  player_values  jsonb       not null default '{}'::jsonb,
  created_at     timestamptz not null default now(),
  primary key (scope, day)
);

-- ---------------------------------------------------------------------------
-- scheduled_actions — work with a deadline. This table IS the bidding engine.
--
-- The UNIQUE on (scope, idempotency_key) is the first line of defence against a
-- duplicate bid: re-planning the same listing hits the constraint and updates
-- the existing row instead of queueing a second one.
-- ---------------------------------------------------------------------------
create table if not exists public.scheduled_actions (
  id               uuid        primary key default gen_random_uuid(),
  scope            text        not null default 'default',
  type             text        not null,
  payload          jsonb       not null default '{}'::jsonb,
  execute_at       timestamptz not null,
  expires_at       timestamptz,
  idempotency_key  text        not null,
  status           text        not null default 'pending'
                   check (status in ('pending','running','done','failed','cancelled','skipped')),
  attempts         integer     not null default 0,
  locked_until     timestamptz,
  result           jsonb,
  last_error       text,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  finished_at      timestamptz,
  unique (scope, idempotency_key)
);
-- The query every tick runs: "what is due, oldest first".
create index if not exists scheduled_actions_due
  on public.scheduled_actions (scope, status, execute_at);

-- ---------------------------------------------------------------------------
-- executions — one row per tick, so you can tell the bot is alive
-- ---------------------------------------------------------------------------
create table if not exists public.executions (
  id           bigserial   primary key,
  scope        text        not null default 'default',
  trigger      text,
  status       text        not null default 'running',
  started_at   timestamptz not null default now(),
  finished_at  timestamptz,
  summary      jsonb,
  error        text
);
create index if not exists executions_scope_started
  on public.executions (scope, started_at desc);

-- ---------------------------------------------------------------------------
-- locks — a mutex two concurrent functions can agree on
-- Leased, never held: a tick that dies does not wedge the bot, its lease simply
-- runs out and the next tick takes over.
-- ---------------------------------------------------------------------------
create table if not exists public.locks (
  scope       text        not null default 'default',
  name        text        not null,
  holder      text        not null,
  expires_at  timestamptz not null,
  primary key (scope, name)
);

-- ---------------------------------------------------------------------------
-- settings — runtime knobs, changeable without a redeploy
-- ---------------------------------------------------------------------------
create table if not exists public.settings (
  scope       text        not null default 'default',
  key         text        not null,
  value       jsonb,
  updated_at  timestamptz not null default now(),
  primary key (scope, key)
);

-- ---------------------------------------------------------------------------
-- agent_decisions — what the LLM decided, and what was actually applied
-- ---------------------------------------------------------------------------
create table if not exists public.agent_decisions (
  id            bigserial   primary key,
  scope         text        not null default 'default',
  created_at    timestamptz not null default now(),
  source        text,
  model         text,
  decision      jsonb,
  input_digest  text,
  applied       boolean     not null default false
);
create index if not exists agent_decisions_scope_created
  on public.agent_decisions (scope, created_at desc);

-- ---------------------------------------------------------------------------
-- Lock everything down.
--
-- RLS enabled with NO policies means: anon and authenticated keys can read and
-- write nothing at all. The service_role key bypasses RLS, and it is the only
-- key the bot uses — from the server side, never from the browser. If you ever
-- want a public read-only dashboard, add a policy here deliberately rather than
-- shipping the anon key to the client.
-- ---------------------------------------------------------------------------
alter table public.agent_state      enable row level security;
alter table public.cache_entries    enable row level security;
alter table public.events           enable row level security;
alter table public.market_snapshots enable row level security;
alter table public.scheduled_actions enable row level security;
alter table public.executions       enable row level security;
alter table public.locks            enable row level security;
alter table public.settings         enable row level security;
alter table public.agent_decisions  enable row level security;

-- ---------------------------------------------------------------------------
-- Housekeeping. The free tier gives you 500MB; events and executions are the
-- only tables that grow without bound, so keep them trimmed.
-- Call it from a tick, or schedule it with pg_cron if your project has it.
-- ---------------------------------------------------------------------------
create or replace function public.fantasybot_prune(keep_days integer default 30)
returns void language sql as $$
  delete from public.events      where ts         < now() - (keep_days || ' days')::interval;
  delete from public.executions  where started_at < now() - (keep_days || ' days')::interval;
  delete from public.cache_entries where expires_at < now() - interval '1 day';
  delete from public.scheduled_actions
    where status in ('done','failed','cancelled','skipped')
      and coalesce(finished_at, updated_at) < now() - (keep_days || ' days')::interval;
$$;
