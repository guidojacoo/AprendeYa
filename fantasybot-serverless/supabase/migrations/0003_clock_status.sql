-- fantasybot · the clock, and a way to see it
--
-- Migration 0002 created the config table and the job. This one is idempotent and
-- safe to run on its own, and it adds the piece 0002 was missing: a way to ASK,
-- from outside the database, whether the cron job actually exists.
--
-- That gap cost a night. `scheduler_config` existed, so every check said the
-- clock was fine — while `cron.schedule` had never been run and nothing was
-- waking the bot at all. A check that can only see half the mechanism will
-- confidently report the working half.

create extension if not exists pg_cron;
create extension if not exists pg_net;

-- ---------------------------------------------------------------------------
-- The wake-up itself (re-stated here so this file stands alone).
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
    url     := cfg.app_url || '/api/tick?mode=' || mode || '&src=db',
    headers := jsonb_build_object(
                 'Authorization', 'Bearer ' || cfg.bot_secret,
                 'Content-Type',  'application/json'),
    body    := '{}'::jsonb,
    timeout_milliseconds := 70000
  ) into request_id;

  return request_id;
end;
$$;

-- ---------------------------------------------------------------------------
-- Every minute. Unscheduled first, so re-running never stacks duplicates.
-- ---------------------------------------------------------------------------
do $$
begin
  perform cron.unschedule('fantasybot-tick');
exception when others then
  null;
end;
$$;

select cron.schedule('fantasybot-tick', '* * * * *',
                     $$select public.fantasybot_wake('tick')$$);

-- ---------------------------------------------------------------------------
-- Let the dashboard see the clock.
--
-- cron.job lives in a schema PostgREST does not expose, so without this the
-- diagnosis can see the CONFIG and not the JOB — which is exactly how a missing
-- schedule hid behind a green check. SECURITY DEFINER so the service role can
-- read it; it returns counts and timestamps only, never the secret.
-- ---------------------------------------------------------------------------
create or replace function public.fantasybot_clock_status()
returns jsonb
language plpgsql
security definer
set search_path = public, cron, net
as $$
declare
  job_count integer := 0;
  job_active boolean := false;
  last_run timestamptz;
  last_status text;
  runs_last_hour integer := 0;
  ok_responses integer := 0;
  bad_responses integer := 0;
begin
  begin
    select count(*), bool_or(active) into job_count, job_active
      from cron.job where jobname = 'fantasybot-tick';

    select start_time, status into last_run, last_status
      from cron.job_run_details d
      join cron.job j on j.jobid = d.jobid
     where j.jobname = 'fantasybot-tick'
     order by start_time desc limit 1;

    select count(*) into runs_last_hour
      from cron.job_run_details d
      join cron.job j on j.jobid = d.jobid
     where j.jobname = 'fantasybot-tick'
       and d.start_time > now() - interval '1 hour';
  exception when others then
    null;   -- pg_cron not installed, or no permission to read its tables
  end;

  -- pg_net calls this column `status_code`; older builds used `status`. Probing
  -- both, dynamically, because a diagnosis that fails on a column name tells you
  -- nothing about the thing you were actually trying to check.
  begin
    execute $q$
      select count(*) filter (where status_code between 200 and 299),
             count(*) filter (where status_code is null or status_code >= 300)
        from net._http_response
       where created > now() - interval '1 hour'
    $q$ into ok_responses, bad_responses;
  exception when others then
    begin
      execute $q$
        select count(*) filter (where status between 200 and 299),
               count(*) filter (where status is null or status >= 300)
          from net._http_response
         where created > now() - interval '1 hour'
      $q$ into ok_responses, bad_responses;
    exception when others then
      null;   -- no response table we can read; the cron counts still stand
    end;
  end;

  return jsonb_build_object(
    'scheduled',      job_count > 0,
    'active',         coalesce(job_active, false),
    'last_run',       last_run,
    'last_status',    last_status,
    'runs_last_hour', runs_last_hour,
    'http_ok',        ok_responses,
    'http_failed',    bad_responses);
end;
$$;

-- Check it right now:
--   select public.fantasybot_clock_status();
--
-- `scheduled: true` and `runs_last_hour` near 60 means the clock is yours.
