-- 007_order_history.sql
-- Persistent memory layer: what each user actually ordered/booked, surviving
-- across ElevenLabs sessions (EL is stateless per conversation). Read at
-- /session/start to let the website assistant propose "il solito" to returning
-- users. MVP scope: items + restaurant + date — no raw transcripts.
--
-- payload examples:
--   asporto:      {"items": ["2x sushi misto", "1x ramen"], "pickup_time": "19:30"}
--   prenotazione: {"party_size": "2", "time": "21:30", "date": "venerdì"}

CREATE TABLE IF NOT EXISTS public.order_history (
  id                       BIGSERIAL PRIMARY KEY,
  user_id                  UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  -- one row per phone call; UNIQUE makes the double write-trigger
  -- (make_restaurant_call + check_call_status) an idempotent upsert
  caller_conversation_id   TEXT NOT NULL UNIQUE,
  restaurant_name          TEXT NOT NULL,
  booking_type             TEXT NOT NULL DEFAULT 'prenotazione',  -- 'prenotazione' | 'asporto'
  items                    JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS order_history_user_created_idx
  ON public.order_history (user_id, created_at DESC);

-- Same pattern as sessions/users: per-user data, users may read their own rows.
-- All writes go through the service-role backend (bypasses RLS by design).
ALTER TABLE public.order_history ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS order_history_select_own ON public.order_history;
CREATE POLICY order_history_select_own ON public.order_history
  FOR SELECT USING (auth.uid() = user_id);
