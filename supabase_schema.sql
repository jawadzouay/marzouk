-- Run this entire file in Supabase SQL Editor

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- AGENTS
CREATE TABLE agents (
  id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  name        TEXT NOT NULL UNIQUE,
  pin         TEXT NOT NULL,
  pin_plain   TEXT,  -- readable copy of the PIN shown to admin in agents list
  is_active   BOOLEAN DEFAULT true,
  created_at  TIMESTAMP DEFAULT now(),
  fired_at    TIMESTAMP NULL
);

-- Migration (run once if table already exists):
-- ALTER TABLE agents ADD COLUMN IF NOT EXISTS pin_plain TEXT;

-- LEADS
CREATE TABLE leads (
  id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  phone            TEXT NOT NULL,
  name             TEXT,
  level            TEXT,
  city             TEXT,
  status           TEXT,
  original_agent   UUID REFERENCES agents(id),
  current_agent    UUID REFERENCES agents(id),
  swap_count       INT DEFAULT 0,
  submitted_at     TIMESTAMP DEFAULT now(),
  locked           BOOLEAN DEFAULT true,
  is_blacklisted   BOOLEAN DEFAULT false,
  swap_eligible_at TIMESTAMP NULL,
  source_date      DATE,
  note             TEXT
);

-- Migration (run this if table already exists):
-- ALTER TABLE leads ADD COLUMN IF NOT EXISTS note TEXT;

-- LEAD HISTORY
CREATE TABLE lead_history (
  id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  lead_id       UUID REFERENCES leads(id),
  agent_id      UUID REFERENCES agents(id),
  action        TEXT,
  status_before TEXT,
  status_after  TEXT,
  note          TEXT,
  created_at    TIMESTAMP DEFAULT now()
);

-- RDV
CREATE TABLE rdv (
  id                 UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  lead_id            UUID REFERENCES leads(id),
  agent_id           UUID REFERENCES agents(id),
  rdv_date           TIMESTAMP,
  status             TEXT DEFAULT 'scheduled',
  confirmed_at       TIMESTAMP NULL,
  no_show_repool_at  TIMESTAMP NULL,
  created_at         TIMESTAMP DEFAULT now()
);

-- SUBMISSIONS
CREATE TABLE submissions (
  id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  agent_id         UUID REFERENCES agents(id),
  submission_date  DATE,
  image_url        TEXT,
  leads_count      INT,
  submitted_at     TIMESTAMP DEFAULT now()
);

-- BLACKLIST
CREATE TABLE blacklist (
  id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  phone       TEXT UNIQUE NOT NULL,
  reason      TEXT,
  added_by    TEXT,
  created_at  TIMESTAMP DEFAULT now()
);

-- Indexes for performance
CREATE INDEX idx_leads_phone ON leads(phone);
CREATE INDEX idx_leads_current_agent ON leads(current_agent);
CREATE INDEX idx_leads_original_agent ON leads(original_agent);
CREATE INDEX idx_leads_status ON leads(status);
CREATE INDEX idx_leads_swap_eligible ON leads(swap_eligible_at);
CREATE INDEX idx_rdv_agent ON rdv(agent_id);
CREATE INDEX idx_rdv_date ON rdv(rdv_date);
CREATE INDEX idx_submissions_agent_date ON submissions(agent_id, submission_date);

-- AD SPEND (one record per agent per upload)
CREATE TABLE IF NOT EXISTS ad_spend (
  id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  agent_id        UUID REFERENCES agents(id),
  spend           NUMERIC(10,2) NOT NULL DEFAULT 0,
  ad_results      INT DEFAULT 0,
  cost_per_result NUMERIC(10,2) DEFAULT 0,
  period_start    DATE NOT NULL,
  period_end      DATE NOT NULL,
  raw_name        TEXT,
  created_at      TIMESTAMP DEFAULT now()
);

-- AGENT AD NAMES (Facebook name aliases per agent, one row per alias)
CREATE TABLE IF NOT EXISTS agent_ad_names (
  id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  agent_id   UUID REFERENCES agents(id),
  ad_name    TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT now(),
  UNIQUE(agent_id, ad_name)
);

CREATE INDEX IF NOT EXISTS idx_ad_spend_agent ON ad_spend(agent_id);
CREATE INDEX IF NOT EXISTS idx_ad_spend_period ON ad_spend(period_start, period_end);

-- Profile & Goals persistence
-- Run these in Supabase SQL editor:
ALTER TABLE agents ADD COLUMN IF NOT EXISTS avatar_url TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS goals JSONB DEFAULT '[]'::jsonb;

-- CITIES (top-level, managed independently)
CREATE TABLE IF NOT EXISTS cities (
  id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  name       TEXT NOT NULL UNIQUE,
  created_at TIMESTAMP DEFAULT now()
);

-- BRANCHES
CREATE TABLE IF NOT EXISTS branches (
  id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  name       TEXT NOT NULL UNIQUE,
  created_at TIMESTAMP DEFAULT now()
);

-- Add branch to agents
ALTER TABLE agents ADD COLUMN IF NOT EXISTS branch_id UUID REFERENCES branches(id);

-- BONUSES (agent-entered manual bonus amounts)
CREATE TABLE IF NOT EXISTS bonuses (
  id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  agent_id   UUID REFERENCES agents(id),
  amount     NUMERIC(10,2) NOT NULL,
  note       TEXT,
  created_at TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bonuses_agent ON bonuses(agent_id);

-- Add city to branches
ALTER TABLE branches ADD COLUMN IF NOT EXISTS city TEXT;

-- Swap level settings (single row)
CREATE TABLE IF NOT EXISTS swap_settings (
  id         INT PRIMARY KEY DEFAULT 1,
  swap_level INT DEFAULT 1
);
INSERT INTO swap_settings (id, swap_level) VALUES (1, 1) ON CONFLICT (id) DO NOTHING;

-- Add branch_id and adset_name to ad_spend
ALTER TABLE ad_spend ADD COLUMN IF NOT EXISTS branch_id UUID REFERENCES branches(id);
ALTER TABLE ad_spend ADD COLUMN IF NOT EXISTS adset_name TEXT;

-- APP SETTINGS (key-value store for admin-configurable values)
CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TIMESTAMP DEFAULT now()
);

-- AGENT REGISTRATION REQUESTS (self-signup flow)
CREATE TABLE IF NOT EXISTS agent_requests (
  id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  requested_name      TEXT NOT NULL,
  password_plain      TEXT NOT NULL,
  status              TEXT DEFAULT 'pending',  -- pending / approved / rejected
  final_name          TEXT,
  branch_id           UUID REFERENCES branches(id),
  requested_city      TEXT,
  requested_branch_id UUID REFERENCES branches(id),
  created_at          TIMESTAMP DEFAULT now()
);
ALTER TABLE agent_requests ADD COLUMN IF NOT EXISTS requested_city TEXT;
ALTER TABLE agent_requests ADD COLUMN IF NOT EXISTS requested_branch_id UUID REFERENCES branches(id);

-- ============================================================================
-- LEAD SOURCES — per-city or per-branch Google Sheet feeds with dynamic columns
--
-- Each config points to one sheet; the admin picks which columns to surface,
-- names them in Arabic, and marks their type (phone/name/date/etc). Leads are
-- synced every 5 minutes by the in-process scheduler and round-robin
-- distributed among active agents in the config's scope (city or branch).
-- ============================================================================

CREATE TABLE IF NOT EXISTS lead_sheet_configs (
  id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  name            TEXT NOT NULL,
  scope_type      TEXT NOT NULL,                 -- 'city' | 'branch'
  scope_id        UUID NOT NULL,
  enabled         BOOLEAN NOT NULL DEFAULT false,
  sheet_id        TEXT,
  sheet_tab       TEXT,                          -- null/empty => first visible tab
  last_synced_at  TIMESTAMPTZ,
  last_row_count  INT DEFAULT 0,
  last_error      TEXT,
  created_at      TIMESTAMPTZ DEFAULT now(),
  UNIQUE(scope_type, scope_id)
);

-- Column mapping: one row per column the admin chose to surface.
-- column_type drives behaviour:
--   key       -> unique row identifier (only one per config); dedup key
--   name      -> lead full name (only one per config); mirrored to full_name
--   date      -> lead created_time (only one per config); mirrored
--   phone     -> normalized as Moroccan phone; multiple allowed; first valid
--                goes to phone_primary, all valid ones to phones[]
--   ad_name   -> ad identifier for quality dashboard grouping
--   platform  -> source platform (fb/ig/...)
--   number    -> numeric display
--   text      -> plain text display
CREATE TABLE IF NOT EXISTS lead_sheet_columns (
  id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  config_id       UUID NOT NULL REFERENCES lead_sheet_configs(id) ON DELETE CASCADE,
  source_header   TEXT NOT NULL,
  display_name    TEXT NOT NULL,
  column_type     TEXT NOT NULL DEFAULT 'text',
  display_order   INT DEFAULT 0,
  visible         BOOLEAN DEFAULT true,
  UNIQUE(config_id, source_header)
);

-- Drop old Kenitra-only schema if it exists (feature never shipped).
DROP TABLE IF EXISTS lead_transfers   CASCADE;
DROP TABLE IF EXISTS ad_leads         CASCADE;
DROP TABLE IF EXISTS ad_leads_sync_state CASCADE;

-- AD LEADS — generic. Free-form `data` JSONB holds everything the admin
-- mapped; the few hot columns are promoted for cheap filtering and grouping.
CREATE TABLE ad_leads (
  id                 UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  config_id          UUID NOT NULL REFERENCES lead_sheet_configs(id) ON DELETE CASCADE,
  scope_type         TEXT NOT NULL,                     -- copied from config
  scope_id           UUID NOT NULL,                     -- copied from config
  source_row_key     TEXT NOT NULL,                     -- unique per config
  data               JSONB NOT NULL DEFAULT '{}'::jsonb,
  phone_primary      TEXT,                              -- normalized 10-digit 06/07
  phones             JSONB NOT NULL DEFAULT '[]'::jsonb, -- [{label,number}]
  full_name          TEXT,
  ad_name            TEXT,
  platform           TEXT,
  created_time       TIMESTAMPTZ,
  assigned_agent_id  UUID REFERENCES agents(id),
  original_agent_id  UUID REFERENCES agents(id),
  assigned_at        TIMESTAMPTZ,
  status             TEXT DEFAULT 'new',
  contacted_at       TIMESTAMPTZ,
  last_note          TEXT,
  inserted_at        TIMESTAMPTZ DEFAULT now(),
  updated_at         TIMESTAMPTZ DEFAULT now(),
  UNIQUE(config_id, source_row_key)
);

CREATE INDEX IF NOT EXISTS idx_ad_leads_scope     ON ad_leads(scope_type, scope_id);
CREATE INDEX IF NOT EXISTS idx_ad_leads_assigned  ON ad_leads(assigned_agent_id, status);
CREATE INDEX IF NOT EXISTS idx_ad_leads_created   ON ad_leads(created_time DESC);
CREATE INDEX IF NOT EXISTS idx_ad_leads_phone     ON ad_leads(phone_primary);
CREATE INDEX IF NOT EXISTS idx_ad_leads_config    ON ad_leads(config_id);
CREATE INDEX IF NOT EXISTS idx_ad_leads_ad_name   ON ad_leads(ad_name);

-- ============================================================================
-- AD LEADS — additional optional columns (run once if upgrading)
--   custom_status: free-text label when status='custom' (set by agent)
--   rdv_date     : scheduled date when status='rdv'
--   adset_name   : ad-set name from the source sheet (live ads analytics)
-- The backend probes for each column at runtime and degrades gracefully if
-- the migration hasn't been applied yet, so deploying the new code without
-- running these is safe — the affected feature is just disabled.
-- ============================================================================
ALTER TABLE ad_leads ADD COLUMN IF NOT EXISTS custom_status TEXT;
ALTER TABLE ad_leads ADD COLUMN IF NOT EXISTS rdv_date      DATE;
ALTER TABLE ad_leads ADD COLUMN IF NOT EXISTS rdv_time      TIME;
ALTER TABLE ad_leads ADD COLUMN IF NOT EXISTS adset_name    TEXT;
CREATE INDEX IF NOT EXISTS idx_ad_leads_rdv_date   ON ad_leads(rdv_date) WHERE rdv_date IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ad_leads_adset_name ON ad_leads(adset_name);

-- status_changed_at powers the admin "today's activity" view: a lead
-- assigned yesterday but marked RDV/registered today should count toward
-- today in the admin dashboard. Backfill existing rows so every lead has
-- a meaningful value (first contact → assignment → insertion time, in
-- order of preference).
ALTER TABLE ad_leads ADD COLUMN IF NOT EXISTS status_changed_at TIMESTAMPTZ;
UPDATE ad_leads
   SET status_changed_at = COALESCE(contacted_at, assigned_at, inserted_at)
 WHERE status_changed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_ad_leads_status_changed_at
  ON ad_leads(status_changed_at DESC);

-- ============================================================================
-- AGENT OFF-DATES — per-agent list of future off days (unchanged)
-- ============================================================================

CREATE TABLE IF NOT EXISTS agent_off_dates (
  agent_id  UUID REFERENCES agents(id) ON DELETE CASCADE,
  off_date  DATE NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (agent_id, off_date)
);

CREATE INDEX IF NOT EXISTS idx_agent_off_dates_date ON agent_off_dates(off_date);

-- ============================================================================
-- LEAD TRANSFERS — audit log of agent-to-agent transfers (same-branch only)
-- ============================================================================

CREATE TABLE lead_transfers (
  id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  lead_id        UUID REFERENCES ad_leads(id) ON DELETE CASCADE,
  from_agent_id  UUID REFERENCES agents(id),
  to_agent_id    UUID REFERENCES agents(id),
  transferred_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_lead_transfers_lead ON lead_transfers(lead_id);
CREATE INDEX IF NOT EXISTS idx_lead_transfers_to   ON lead_transfers(to_agent_id);

-- Round-robin bookkeeping. Updated only on system auto-assign; transfers
-- between agents don't touch it so transferred leads stay additive.
ALTER TABLE agents ADD COLUMN IF NOT EXISTS last_distributed_at TIMESTAMPTZ;

-- Drop stale Kenitra single-sheet setting if it exists.
DELETE FROM settings WHERE key = 'kenitra_sheet_id';
