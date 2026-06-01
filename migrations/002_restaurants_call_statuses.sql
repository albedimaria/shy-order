-- restaurants: replaces restaurants.json (ephemeral on Render)
CREATE TABLE IF NOT EXISTS public.restaurants (
  id           SERIAL      PRIMARY KEY,
  name         TEXT        NOT NULL,
  phone_number TEXT        NOT NULL,
  address      TEXT        NOT NULL DEFAULT '',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Case-insensitive unique index mirrors the ilike lookup in code
CREATE UNIQUE INDEX IF NOT EXISTS restaurants_name_lower_idx
  ON public.restaurants (lower(name));

-- call_statuses: replaces in-memory dict so multi-worker deploys stay consistent
CREATE TABLE IF NOT EXISTS public.call_statuses (
  call_sid   TEXT        PRIMARY KEY,
  status     TEXT        NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed data from restaurants.json
INSERT INTO public.restaurants (name, phone_number, address)
VALUES ('PepeVerde', '0373123456', 'Piazza Benvenuti 6, Ombriano di Crema')
ON CONFLICT DO NOTHING;
