-- 003_restaurant_tracking.sql
-- Track how many times each restaurant has been called
-- and which restaurant is associated with each session

ALTER TABLE public.restaurants
  ADD COLUMN IF NOT EXISTS call_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE public.sessions
  ADD COLUMN IF NOT EXISTS restaurant_name TEXT;

ALTER TABLE public.sessions
  ADD COLUMN IF NOT EXISTS elevenlabs_conversation_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS sessions_conversation_id_idx
  ON public.sessions (elevenlabs_conversation_id)
  WHERE elevenlabs_conversation_id IS NOT NULL;

-- Atomic increment helper (avoids race conditions)
CREATE OR REPLACE FUNCTION public.increment_restaurant_call_count(p_name TEXT)
RETURNS VOID AS $$
  UPDATE public.restaurants
  SET call_count = call_count + 1
  WHERE lower(name) = lower(p_name);
$$ LANGUAGE sql;
