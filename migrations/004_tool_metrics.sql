-- 004_tool_metrics.sql
-- Latency/outcome telemetry for the backend round-trips we actually own.
-- ElevenLabs owns the audio/ASR/TTS/turn-taking pipeline, so we can't measure
-- those; what we CAN measure is each tool webhook round-trip, the scrape
-- (fetch + OpenAI extraction), the ElevenLabs token fetch, and the Twilio call
-- outcome. One row per instrumented operation; the dashboard reads this table.

CREATE TABLE IF NOT EXISTS public.tool_metrics (
  id              BIGSERIAL PRIMARY KEY,
  tool            TEXT NOT NULL,           -- e.g. 'tool:make_restaurant_call', 'scrape', 'elevenlabs_token'
  duration_ms     NUMERIC NOT NULL,
  outcome         TEXT,                    -- 'ok' | 'error' | a Twilio terminal status ('completed', 'no-answer', ...)
  call_sid        TEXT,
  conversation_id TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS tool_metrics_tool_created_idx
  ON public.tool_metrics (tool, created_at DESC);

-- RLS on, no policies: only the service-role backend reads/writes this table,
-- exactly like restaurants and call_statuses. The anon/authenticated roles get
-- no access by default once RLS is enabled.
ALTER TABLE public.tool_metrics ENABLE ROW LEVEL SECURITY;
