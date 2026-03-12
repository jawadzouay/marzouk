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
  source_date      DATE
);

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
