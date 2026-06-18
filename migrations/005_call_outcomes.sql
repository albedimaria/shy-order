-- 005_call_outcomes.sql
-- Persist the outcome of each restaurant-caller conversation. Today it's ephemeral:
-- make_restaurant_call fetches it from the ElevenLabs conversation API and returns it
-- to the website agent, then it's lost. This table is the observability source for the
-- call leg, beyond the backend round-trips already in tool_metrics.
--
-- Fields mirror what the ElevenLabs conversation API actually returns (verified):
--   metadata.call_duration_secs, metadata.termination_reason,
--   analysis.call_successful ('success'|'failure'|'unknown'), analysis.transcript_summary,
--   analysis.call_summary_title, analysis.evaluation_criteria_results (the online eval).

CREATE TABLE IF NOT EXISTS public.call_outcomes (
  conversation_id          TEXT PRIMARY KEY,           -- ElevenLabs caller conversation id
  call_sid                 TEXT,
  website_conversation_id  TEXT,                       -- the browser session's EL conversation (link to sessions)
  status                   TEXT,                       -- 'done' | 'in_progress' | 'processing' | ...
  call_successful          TEXT,                       -- ElevenLabs enum: 'success' | 'failure' | 'unknown'
  transcript_summary       TEXT,
  summary_title            TEXT,
  duration_seconds         INTEGER,
  termination_reason       TEXT,
  restaurant_name          TEXT,
  evaluation_results       JSONB NOT NULL DEFAULT '{}'::jsonb,  -- analysis.evaluation_criteria_results (online eval)
  created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS call_outcomes_created_idx ON public.call_outcomes (created_at DESC);

-- RLS on, no policies: service-role backend only, like tool_metrics / restaurants.
ALTER TABLE public.call_outcomes ENABLE ROW LEVEL SECURITY;
