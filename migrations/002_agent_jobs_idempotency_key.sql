-- Optional idempotency key, same rationale as
-- social-media-worker/migrations/002_publish_jobs_idempotency_key.sql:
-- lets MarketingApp/environments/agent_job_queue.py's insert_agent_job
-- (used by worker.py's MCP server for its audit-trail insert) retry safely
-- without creating a duplicate agent_jobs row.
--
-- Backward compatible: nullable column, partial unique index (only enforced
-- when non-null) -- existing rows and any external inserter that never sets
-- this column are unaffected.
alter table agent_jobs add column if not exists idempotency_key text;

create unique index if not exists agent_jobs_idempotency_key_uidx
  on agent_jobs (idempotency_key)
  where idempotency_key is not null;

-- ---------------------------------------------------------------------------
-- Least-privilege access for the worker process itself (see AGENT.md
-- "Supabase anahtarini daraltma"). Same idea as publish_jobs: today
-- worker.py authenticates with SUPABASE_SECRET_KEY (service-role, bypasses
-- RLS project-wide). This is a ready-to-run, narrower alternative -- create
-- the role, then generate an API key for it in the Supabase dashboard and
-- put it in SUPABASE_AGENT_KEY. Not applied automatically; uncomment when
-- you're ready to provision the actual key.
--
-- create role marketing_agent_writer nologin;
-- grant usage on schema public to marketing_agent_writer;
-- grant select, insert, update on agent_jobs to marketing_agent_writer;
--
-- create policy marketing_agent_writer_select on agent_jobs
--   for select to marketing_agent_writer using (true);
-- create policy marketing_agent_writer_insert on agent_jobs
--   for insert to marketing_agent_writer with check (true);
-- create policy marketing_agent_writer_update on agent_jobs
--   for update to marketing_agent_writer using (true) with check (true);
