-- Run this entire file in Supabase SQL Editor

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- AGENTS
CREATE TABLE agents (
  id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  name        TEXT NOT NULL UNIQUE,
  pin         TEXT NOT NULL,
  is_active   BOOLEAN DEFAULT true,
  created_at  TIMESTAMP DEFAULT now(),
  fired_at    TIMESTAMP NULL
);

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

-- AGENT REGISTRATION REQUESTS (self-signup flow)
CREATE TABLE IF NOT EXISTS agent_requests (
  id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  requested_name TEXT NOT NULL,
  password_plain TEXT NOT NULL,
  status         TEXT DEFAULT 'pending',  -- pending / approved / rejected
  final_name     TEXT,
  branch_id      UUID REFERENCES branches(id),
  created_at     TIMESTAMP DEFAULT now()
);
