-- agent_jobs: durable queue + audit trail for MarketingApp/worker.py.
--
-- Two producers write rows here:
--   - anything external (the App, scheduler_worker, a human via psql/Studio)
--     inserts with status='queued'; the worker's poll loop claims and runs it.
--   - the worker's own MCP server inserts a row directly with status='processing'
--     for on-demand calls (see run_marketing_task), so both entry points leave
--     the same audit trail.
--
-- Modeled on social-media-worker/migrations/001_publish_jobs.sql (same
-- claim-with-skip-locked pattern, same Supabase project as publish_jobs and
-- MarketingApp/araclar/worker_yayinlama_araclari.py).
create table if not exists agent_jobs (
  id uuid primary key default gen_random_uuid(),
  owner_ref text,
  status text not null default 'queued' check (status in ('queued', 'processing', 'done', 'failed')),
  payload jsonb not null,
  results jsonb not null default '{}'::jsonb,
  error text,
  attempts integer not null default 0,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  completed_at timestamptz,
  updated_at timestamptz not null default now()
);

create index if not exists agent_jobs_status_created_idx on agent_jobs(status, created_at);
alter table agent_jobs enable row level security;

-- Safe with multiple worker replicas: locked rows are skipped instead of
-- allowing two workers to claim the same queued job.
create or replace function claim_next_agent_job()
returns agent_jobs
language plpgsql
as $$
declare claimed agent_jobs;
begin
  with candidate as (
    select id from agent_jobs where status = 'queued'
    order by created_at asc for update skip locked limit 1
  )
  update agent_jobs job set
    status = 'processing', attempts = job.attempts + 1,
    started_at = now(), updated_at = now(), error = null
  from candidate where job.id = candidate.id
  returning job.* into claimed;
  return claimed;
end;
$$;
