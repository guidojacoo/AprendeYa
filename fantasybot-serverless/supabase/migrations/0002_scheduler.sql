-- fantasybot · the database as the clock
--
-- Why this exists: GitHub Actions' `schedule` event is best-effort, and on this
-- deployment "best effort" meant zero runs in over an hour. It deprioritises
-- scheduled workflows hard, and a bot that is only woken when GitHub feels like
-- it is not a bot that plays a market close.
--
-- Supabase's free tier ships pg_cron and pg_net, so the database you are already
-- paying nothing for can wake the bot itself — every minute, on time, with no
-- third-party signup and no dependency on anybody's scheduler but your own.
--
-- Run this ONCE, in the SQL Editor, after editing the two values in the INSERT
-- below. Re-running is safe.

create extension if not exists pg_cron;
create extension if not exists pg_net;

-- ---------------------------------------------------------------------------
-- Where to call, and with what.
--
-- The secret lives in a table rather than inline in the job definition so that
-- rotating it is an UPDATE, not a re-scheduling — and so it is not sitting in
-- cron.job's command text where every listing of your jobs would print it.
-- RLS is on with no policies, so only the service role can read this.
-- ---------------------------------------------------------------------------
create table if not exists public.scheduler_config (
  id          integer     primary key default 1 check (id = 1),
  app_url     text        not null,
  bot_secret  text        not null,
  enabled     boolean     not null default true,
  updated_at  timestamptz not null default now()
);
alter table public.scheduler_config enable row level security;

-- >>> EDIT THESE TWO VALUES <<<
insert into public.scheduler_config (id, app_url, bot_secret)
values (1, 'https://TU-APP.vercel.app', 'TU_BOT_CRON_SECRET')
on conflict (id) do update
  set app_url = excluded.app_url,
      bot_secret = excluded.bot_secret,
      updated_at = now();

-- ---------------------------------------------------------------------------
-- One wake-up.
--
-- Fire-and-forget on purpose: pg_net queues the request and returns immediately,
-- so a slow tick can never hold a cron slot open or stack jobs on top of each
-- other. The function's own idempotency is what makes overlap harmless anyway.
-- ---------------------------------------------------------------------------
create or replace function public.fantasybot_wake(mode text default 'tick')
returns bigint
language plpgsql
security definer
set search_path = public, extensions, net
as $$
declare
  cfg public.scheduler_config;
  request_id bigint;
begin
  select * into cfg from public.scheduler_config where id = 1;
  if cfg is null or not cfg.enabled then
    return null;
  end if;

  select net.http_post(
    -- `src` is how the diagnosis can tell an automated wake-up from one you
    -- triggered by hand; they are otherwise identical in the log.
    url     := cfg.app_url || '/api/tick?mode=' || mode || '&src=db',
    headers := jsonb_build_object(
                 'Authorization', 'Bearer ' || cfg.bot_secret,
                 'Content-Type',  'application/json'),
    body    := '{}'::jsonb,
    -- Longer than the function's own 60s ceiling, so a slow tick is recorded as
    -- slow rather than as a timeout we caused ourselves.
    timeout_milliseconds := 70000
  ) into request_id;

  return request_id;
end;
$$;

-- ---------------------------------------------------------------------------
-- Every minute.
--
-- A minute is the finest granularity pg_cron offers, and it is the right one:
-- a queued bid becomes due 60s before its listing closes, so a one-minute clock
-- always catches it with time to spare, and the function then polls the market
-- into the final seconds itself.
--
-- Unscheduled first so re-running this file does not stack duplicate jobs.
-- ---------------------------------------------------------------------------
do $$
begin
  perform cron.unschedule('fantasybot-tick');
exception when others then
  null;   -- not scheduled yet; nothing to remove
end;
$$;

select cron.schedule('fantasybot-tick', '* * * * *',
                     $$select public.fantasybot_wake('tick')$$);

-- ---------------------------------------------------------------------------
-- Check it.
--
--   select * from cron.job;                        -- the schedule exists
--   select * from cron.job_run_details              -- it is actually firing
--     order by start_time desc limit 10;
--   select status, count(*) from net._http_response  -- Vercel is answering 200
--     group by status;
--
-- To pause without deleting:  update public.scheduler_config set enabled = false;
-- To rotate the secret:       update public.scheduler_config set bot_secret = '...';
-- ---------------------------------------------------------------------------
