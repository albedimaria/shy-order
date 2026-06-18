-- 006_evals.sql
-- Reproducible offline eval of the /scrape restaurant-info extraction (the part we
-- fully own — input page -> expected {name, phone, address, hours}). One row in
-- eval_runs per suite execution; one row in eval_results per scenario. Mirrors the
-- dance-voice-agent eval schema, adapted to field-level scoring.

CREATE TABLE IF NOT EXISTS public.eval_runs (
  id            BIGSERIAL PRIMARY KEY,
  run_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  git_sha       TEXT,
  suite         TEXT NOT NULL,                 -- e.g. 'scrape_extraction'
  model         TEXT,                          -- e.g. 'gpt-4.1-mini'
  n_scenarios   INTEGER NOT NULL,
  n_passed      INTEGER NOT NULL,
  success_rate  NUMERIC(5,2) NOT NULL,         -- % scenarios passed (all required fields correct)
  avg_score     NUMERIC(5,2),                  -- mean per-field score, 0..100
  p50_ms        INTEGER,
  p95_ms        INTEGER,
  notes         TEXT
);

CREATE TABLE IF NOT EXISTS public.eval_results (
  id           BIGSERIAL PRIMARY KEY,
  run_id       BIGINT NOT NULL REFERENCES public.eval_runs(id) ON DELETE CASCADE,
  scenario_id  TEXT NOT NULL,
  name         TEXT,
  passed       BOOLEAN NOT NULL,
  score        NUMERIC(5,2),                   -- per-field score for this scenario, 0..100
  expected     JSONB,
  actual       JSONB,
  latency_ms   INTEGER,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS eval_results_run_idx ON public.eval_results (run_id);

ALTER TABLE public.eval_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.eval_results ENABLE ROW LEVEL SECURITY;
