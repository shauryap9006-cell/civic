-- CivicLens database schema (PostgreSQL 15+ with PostGIS)
-- Applied automatically by docker compose via /docker-entrypoint-initdb.d.
-- Source of truth for the data model. If code and this file disagree, this file wins.

CREATE EXTENSION IF NOT EXISTS postgis;

-- ---------------------------------------------------------------------------
-- Enumerations
-- ---------------------------------------------------------------------------
CREATE TYPE pothole_status AS ENUM (
  'open',          -- first event received, authority not yet notified
  'reported',      -- authority notified; waiting for a repair claim
  'claimed_fixed', -- authority says it is repaired (a claim, not proof)
  'unknown',       -- passes after the claim exist, but none was a valid clean pass
  'verified',      -- enough valid clean passes after the claim saw no pothole
  'reopened'       -- new evidence contradicts a fixed claim or a verification
);

CREATE TYPE severity_band AS ENUM ('low', 'medium', 'high');

CREATE TYPE report_kind AS ENUM ('initial', 'reminder', 'escalation', 'reopened');

CREATE TYPE report_delivery_status AS ENUM ('pending', 'sent', 'failed');

-- ---------------------------------------------------------------------------
-- Reference data
-- ---------------------------------------------------------------------------
CREATE TABLE authorities (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name             text NOT NULL,
  city             text NOT NULL,
  email            text NOT NULL,          -- real contact; in EMAIL_MODE=test mail still goes to TEST_INBOX
  escalation_email text,
  boundary         geography(MultiPolygon, 4326),  -- optional; used to pick the authority for a location
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE devices (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  public_key     text NOT NULL UNIQUE,     -- Ed25519 public key, base64
  platform       text NOT NULL CHECK (platform IN ('android', 'linux', 'replay', 'seed')),
  app_version    text,
  model_version  text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  last_seen_at   timestamptz
);

-- ---------------------------------------------------------------------------
-- Core entities
-- ---------------------------------------------------------------------------
CREATE TABLE potholes (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  location          geography(Point, 4326) NOT NULL,   -- canonical, recomputed on merge
  status            pothole_status NOT NULL DEFAULT 'open',
  severity          severity_band NOT NULL DEFAULT 'low',
  event_count       integer NOT NULL DEFAULT 0,
  authority_id      uuid REFERENCES authorities(id),
  before_event_id   uuid,                              -- best event before the latest claim (FK added below)
  first_seen_at     timestamptz NOT NULL,
  last_event_at     timestamptz NOT NULL,
  reported_at       timestamptz,                       -- first time the authority was notified
  last_reported_at  timestamptz,                       -- start of the current escalation cycle
  claimed_fixed_at  timestamptz,                       -- latest claim time (server clock)
  verified_at       timestamptz,
  reopened_at       timestamptz,
  fixed_token_epoch integer NOT NULL DEFAULT 0,        -- bumped on reopen; supersedes the previous Fixed link
  fixed_token_hash  text UNIQUE,                       -- sha256 hex of the current token (for lookup); the token itself is derived, never stored
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE events (
  id               uuid PRIMARY KEY,                   -- generated on the device => idempotent uploads
  device_id        uuid NOT NULL REFERENCES devices(id),
  pass_id          uuid,                               -- drive/pass this event came from
  pothole_id       uuid REFERENCES potholes(id),
  captured_at      timestamptz NOT NULL,               -- device time of the best frame
  received_at      timestamptz NOT NULL DEFAULT now(),
  location         geography(Point, 4326) NOT NULL,
  gps_accuracy_m   real,
  heading_deg      real,
  speed_mps        real,
  confidence       real NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  track_frames     integer NOT NULL CHECK (track_frames >= 1),
  area_ratio       real,                               -- box area / frame area
  severity         severity_band,                      -- recomputed by the server
  crop_key         text NOT NULL,                      -- object-storage key of the road-only crop
  crop_sha256      text NOT NULL,
  sharpness        real,
  mean_luma        real,
  model_version    text,
  app_version      text,
  payload          text NOT NULL,                      -- exact signed payload string, kept for audit
  signature        text NOT NULL
);

ALTER TABLE potholes
  ADD CONSTRAINT potholes_before_event_fk
  FOREIGN KEY (before_event_id) REFERENCES events(id);

CREATE TABLE passes (
  id             uuid PRIMARY KEY,                     -- generated on the device (one per drive session)
  device_id      uuid NOT NULL REFERENCES devices(id),
  started_at     timestamptz NOT NULL,
  ended_at       timestamptz NOT NULL,
  path           geography(LineString, 4326),          -- built from samples
  samples        jsonb NOT NULL,                       -- array of quality samples (see openapi PassSample)
  sample_count   integer NOT NULL,
  model_version  text,
  app_version    text,
  received_at    timestamptz NOT NULL DEFAULT now(),
  payload        text NOT NULL,
  signature      text NOT NULL
);

-- Derived cache: recomputable from passes + events + potholes. Never edited by hand.
CREATE TABLE pass_coverage (
  pass_id          uuid NOT NULL REFERENCES passes(id) ON DELETE CASCADE,
  pothole_id       uuid NOT NULL REFERENCES potholes(id) ON DELETE CASCADE,
  crossing_at      timestamptz NOT NULL,               -- time of closest approach
  clean            boolean NOT NULL,
  reason           text NOT NULL,                      -- 'ok' or the first failed check (see docs/STATE_MACHINE.md)
  window_samples   integer NOT NULL,
  ok_fraction      real,
  computed_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (pass_id, pothole_id)
);

CREATE TABLE reports (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  pothole_id           uuid NOT NULL REFERENCES potholes(id),
  authority_id         uuid NOT NULL REFERENCES authorities(id),
  kind                 report_kind NOT NULL,
  to_address           text NOT NULL,                  -- actual recipient used (TEST_INBOX in test mode)
  status               report_delivery_status NOT NULL DEFAULT 'pending',
  attempts             integer NOT NULL DEFAULT 0,
  provider_message_id  text,
  error                text,
  created_at           timestamptz NOT NULL DEFAULT now(),
  sent_at              timestamptz
);

CREATE TABLE status_history (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  pothole_id   uuid NOT NULL REFERENCES potholes(id) ON DELETE CASCADE,
  from_status  pothole_status,
  to_status    pothole_status NOT NULL,
  reason       text NOT NULL,                          -- machine-readable code, e.g. 'fixed_claim', 'clean_passes_met'
  evidence     jsonb NOT NULL DEFAULT '{}'::jsonb,     -- event ids, pass ids, thresholds used
  at           timestamptz NOT NULL                    -- logical (possibly simulated) time
);

-- Simple key/value state, e.g. the simulated clock offset used by the demo.
CREATE TABLE app_state (
  key    text PRIMARY KEY,
  value  text NOT NULL
);
INSERT INTO app_state (key, value) VALUES ('clock_offset_seconds', '0');

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------
CREATE INDEX potholes_location_gix ON potholes USING gist (location);
CREATE INDEX potholes_status_idx   ON potholes (status);
CREATE INDEX events_location_gix   ON events USING gist (location);
CREATE INDEX events_pothole_idx    ON events (pothole_id, captured_at);
CREATE INDEX events_pass_idx       ON events (pass_id);
CREATE INDEX events_device_idx     ON events (device_id);
CREATE INDEX passes_path_gix       ON passes USING gist (path);
CREATE INDEX passes_device_idx     ON passes (device_id, started_at);
CREATE INDEX coverage_pothole_idx  ON pass_coverage (pothole_id);
CREATE INDEX reports_pothole_idx   ON reports (pothole_id, created_at);
CREATE INDEX history_pothole_idx   ON status_history (pothole_id, at);
