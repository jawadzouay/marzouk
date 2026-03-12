# Marzouk Academy — CRM System Spec
> Full build spec derived from planning session. Read this entire file before writing any code.

---

## 🏗️ Project Overview

A mobile-first Arabic RTL web application for **Marzouk Academy** that allows 25 sales agents to:
- Upload a photo of their daily handwritten leads sheet
- Have Claude AI extract the leads automatically
- Review and confirm the data
- Track RDVs, conversions, and performance

The admin gets a full dashboard to monitor all agents, manage the swap pool, view the RDV calendar, and manage the blacklist.

---

## 💻 Tech Stack

| Layer | Tool | Cost |
|---|---|---|
| Frontend | HTML / CSS / JS (Arabic RTL) | Free |
| Backend | Python FastAPI | Free |
| Database | Supabase (PostgreSQL) | Free |
| AI Extraction | Claude Haiku (claude-haiku-4-5-20251001) | ~$22/month |
| Sheets Export | Google Sheets API | Free |
| Hosting | Railway free tier | Free |
| Domain | None for now (Railway URL) | Free |

**Total monthly cost → ~$22/month (Claude API only)**

---

## 📁 Folder Structure

```
marzouk-academy/
├── CLAUDE.md
├── backend/
│   ├── main.py                  # FastAPI app entry point
│   ├── requirements.txt
│   ├── .env                     # API keys (never commit)
│   ├── routes/
│   │   ├── auth.py              # Login / PIN
│   │   ├── agents.py            # Agent management
│   │   ├── leads.py             # Lead submission + extraction
│   │   ├── rdv.py               # RDV tracking
│   │   ├── swap.py              # Swap pool logic
│   │   ├── admin.py             # Admin dashboard data
│   │   └── blacklist.py         # Blacklist management
│   ├── services/
│   │   ├── claude_service.py    # Claude Haiku image extraction
│   │   ├── sheets_service.py    # Google Sheets sync
│   │   ├── swap_service.py      # Swap pool logic
│   │   └── supabase_service.py  # DB helpers
│   └── models/
│       ├── lead.py
│       ├── agent.py
│       └── rdv.py
├── frontend/
│   ├── index.html               # Login page
│   ├── agent/
│   │   ├── dashboard.html       # Agent personal dashboard
│   │   ├── submit.html          # Photo upload + review
│   │   └── rdv.html             # Agent RDV list
│   └── admin/
│       ├── dashboard.html       # Admin main dashboard
│       ├── agents.html          # Add/fire agents
│       ├── calendar.html        # RDV calendar
│       ├── swap.html            # Swap pool view
│       ├── leaderboard.html     # Full leaderboard
│       └── blacklist.html       # Blacklist management
└── README.md
```

---

## 🗄️ Database Schema (Supabase)

### Table: `agents`
```sql
id              UUID PRIMARY KEY DEFAULT uuid_generate_v4()
name            TEXT NOT NULL
pin             TEXT NOT NULL  -- hashed 4-digit PIN
is_active       BOOLEAN DEFAULT true
created_at      TIMESTAMP DEFAULT now()
fired_at        TIMESTAMP NULL
```

### Table: `leads`
```sql
id              UUID PRIMARY KEY DEFAULT uuid_generate_v4()
phone           TEXT NOT NULL
name            TEXT
level           TEXT
city            TEXT
status          TEXT  -- RDV, B.V, N.R, P.I, Autre ville
original_agent  UUID REFERENCES agents(id)
current_agent   UUID REFERENCES agents(id)
swap_count      INT DEFAULT 0  -- max 3
submitted_at    TIMESTAMP DEFAULT now()
locked          BOOLEAN DEFAULT true
is_blacklisted  BOOLEAN DEFAULT false
swap_eligible_at TIMESTAMP NULL  -- when 4-day timer expires
source_date     DATE  -- date written on the paper
```

### Table: `lead_history`
```sql
id              UUID PRIMARY KEY DEFAULT uuid_generate_v4()
lead_id         UUID REFERENCES leads(id)
agent_id        UUID REFERENCES agents(id)
action          TEXT  -- 'submitted', 'swapped', 'rdv_booked', 'showed_up', 'no_show', 'archived'
status_before   TEXT
status_after    TEXT
note            TEXT
created_at      TIMESTAMP DEFAULT now()
```

### Table: `rdv`
```sql
id              UUID PRIMARY KEY DEFAULT uuid_generate_v4()
lead_id         UUID REFERENCES leads(id)
agent_id        UUID REFERENCES agents(id)
rdv_date        TIMESTAMP
status          TEXT  -- 'scheduled', 'showed_up', 'no_show'
confirmed_at    TIMESTAMP NULL
no_show_repool_at TIMESTAMP NULL  -- rdv_date + 10 days
created_at      TIMESTAMP DEFAULT now()
```

### Table: `submissions`
```sql
id              UUID PRIMARY KEY DEFAULT uuid_generate_v4()
agent_id        UUID REFERENCES agents(id)
submission_date DATE
image_url       TEXT
leads_count     INT
submitted_at    TIMESTAMP DEFAULT now()
```

### Table: `blacklist`
```sql
id              UUID PRIMARY KEY DEFAULT uuid_generate_v4()
phone           TEXT UNIQUE NOT NULL
reason          TEXT
added_by        UUID REFERENCES agents(id)  -- always admin
created_at      TIMESTAMP DEFAULT now()
```

---

## 🔐 Authentication

- **Agents** → 4-digit PIN created by admin
- **Admin** → separate admin PIN (set in .env)
- **Sessions** → JWT token stored in localStorage
- **No self-registration** → admin creates all accounts
- **Fired agent** → `is_active = false` → immediate 401 on all requests

---

## 📸 Lead Extraction Flow

```
1. Agent opens submit.html
2. Agent takes photo or uploads image
3. Frontend sends image to POST /leads/extract
4. Backend sends image to Claude Haiku with this prompt:

   "Extract all rows from this handwritten Arabic leads table into JSON.
    Each row must have: phone, name, level, city, status.
    Rules:
    - Remove all dashes and spaces from phone numbers
    - If status contains RV, RDV, R.D.V → set status to 'RDV'
    - If status contains B.V, boite vocal → set status to 'B.V'  
    - If status contains N.R, NRP → set status to 'N.R'
    - If status contains P.I, pas intéressé → set status to 'P.I'
    - If status contains autre ville, autre city → set status to 'Autre ville'
    - Phone numbers must be 10 digits starting with 06 or 07
    - Flag any phone number that does not match this pattern
    - Return only valid JSON array, no extra text"

5. Claude returns JSON array
6. Frontend shows editable table to agent
   - Phone numbers highlighted in red if invalid format
   - Agent can edit any phone number
   - Agent cannot edit name/city/status (only phone)
7. Agent clicks confirm → POST /leads/submit
8. Backend validates:
   - Check each number against blacklist → block if found
   - Check for duplicate numbers in DB → warn agent, skip duplicate
   - First submission wins on duplicates
9. Leads saved to Supabase → locked
10. Leads synced to agent's Google Sheet tab
11. Submission logged in submissions table
```

---

## 📊 Lead Status Logic

```
RDV          → Goes to RDV tracker, removed from swap pool
B.V          → Enters swap pool after 4 days (weekends counted)
N.R          → Enters swap pool after 4 days (weekends counted)
P.I          → Archived immediately, never swapped
Autre ville  → Archived immediately, never swapped
```

---

## 🔄 Swap System

```
ELIGIBILITY:
- Only B.V and N.R leads enter swap pool
- Timer starts from submission date
- After 4 days with no status change → eligible for swap

SWAP RULES:
- Assignment is RANDOM from active agents
- Cannot swap to original agent
- Cannot swap to current agent
- Max 3 swaps total per lead
- After 3rd swap fails → lead goes to main archive sheet

SWAP COUNTER:
swap_count = 0 → original agent
swap_count = 1 → first swap
swap_count = 2 → second swap  
swap_count = 3 → third swap → archive

HISTORY VISIBILITY:
- New agent sees lead history (actions only, NO agent names)
- Admin sees full history including agent names

WHEN AGENT IS FIRED:
- All their active leads auto-redistributed randomly
- Their RDV leads reassigned to admin for manual handling
- Their swap pool leads enter pool immediately
```

---

## 📅 RDV System

```
BOOKING:
- Agent marks lead as RDV
- Agent enters RDV date and time (extracted from photo or manual)
- Lead removed from swap pool

CONFIRMATION:
- Agent confirms or cancels RDV (agent only, not admin)

SHOW UP:
- Agent marks showed_up ✅ or no_show ❌

NO SHOW RULE:
- If no_show → lead goes back to swap pool after 10 days
- Lead gets flagged with "no_show" in history
- 10 day timer starts from original RDV date

CALENDAR:
- Admin sees all RDVs for all agents
- Agent sees only their own RDVs
- Calendar view by day
```

---

## 📱 Call Button

```
Each lead row has ONE call button
Click → small popup appears with 2 options:
  📱 Normal Call  →  tel:{phone}
  💬 WhatsApp    →  https://wa.me/212{phone without leading 0}

No pre-written message on WhatsApp
Takes minimal space on mobile
```

---

## 🚫 Blacklist

```
- Admin only can add/remove numbers
- When agent submits → each number checked against blacklist
- Blacklisted number → blocked, warning shown to agent, row skipped
- Blacklisted leads never enter swap pool
```

---

## 📋 Duplicate Number Handling

```
- Same number submitted by 2 agents same day
- First submission wins
- Second agent gets warning: "هذا الرقم موجود مسبقاً" (number already exists)
- Duplicate row is NOT saved
- Agent can delete the duplicate row before final submit
```

---

## 📈 Agent Dashboard (Agent sees own data only)

```
- Total leads submitted today / this week / this month
- RDVs booked (count + %)
- Shows up (count + %)
- B.V count + %
- N.R count + %
- Autre ville count + %
- P.I count + %
- Their own leaderboard position (position number only, no other agent names)
- Their scheduled RDVs (calendar)
- Their swap pool leads (leads they received from swaps)
```

---

## 📊 Admin Dashboard (Admin sees everything)

```
- All agents stats in one view
- Filter by: agent / date range / status
- Full leaderboard with all agent names
- Daily submission calendar (✅ submitted / ❌ not yet)
- RDV calendar (all agents)
- Swap pool status (how many leads in pool, how long)
- Archive sheet
- Add / fire agents
- Blacklist management
- Google Sheets sync status
```

---

## 📊 Google Sheets Structure

```
One Google Spreadsheet with multiple tabs:
- Tab per agent (agent name)
- Tab: "Archive" (Autre ville + P.I + 3x swapped)
- Tab: "Blacklist"
- Tab: "All Leads" (master sheet, all agents combined)

Columns per tab:
Date | Agent | # | Phone | Name | Level | City | Status | Swap Count | Submitted At
```

---

## 🌐 Frontend Guidelines

```
- Language: Arabic
- Direction: RTL (right to left)
- Mobile first (agents use phones)
- Simple, clean UI
- Large buttons (finger-friendly)
- Color coding:
  RDV         → Green  #E2EFDA
  B.V         → Yellow #FFF2CC
  N.R         → Red    #FCE4D6
  P.I         → Purple #F2CEEF
  Autre ville → Blue   #DDEBF7
- Font: Cairo or Tajawal (Arabic Google Fonts)
- App name: Marzouk Academy (مرزوق أكاديمي)
```

---

## 🔔 Notifications (In-App Only)

```
- Agent receives swapped lead → in-app notification badge
- Admin sees daily who submitted ✅ and who hasn't ❌
- Submission deadline reminder at 10:30pm if agent hasn't submitted
- Lead eligible for swap → admin notified
```

---

## ⚙️ Environment Variables (.env)

```
SUPABASE_URL=your_supabase_url
SUPABASE_ANON_KEY=your_supabase_anon_key
SUPABASE_SERVICE_KEY=your_supabase_service_key
ANTHROPIC_API_KEY=your_claude_api_key
GOOGLE_SHEET_ID=your_sheet_id
GOOGLE_CREDENTIALS_JSON=path_to_credentials.json
ADMIN_PIN=your_admin_pin
JWT_SECRET=your_jwt_secret
```

---

## 🏗️ Build Order

### Day 1 — Core Flow
```
1. Set up FastAPI project structure
2. Connect Supabase (create all tables)
3. Agent login with PIN (JWT)
4. Photo upload endpoint
5. Claude Haiku extraction service
6. Agent review page (editable phone numbers)
7. Lead submission + validation (blacklist + duplicates)
8. Lock leads after submission
9. Google Sheets sync
10. Basic agent dashboard (stats)
```

### Day 2 — Advanced Features
```
11. Admin login + dashboard
12. Swap system (4 day timer + random assignment)
13. RDV tracker + calendar
14. No-show → back to pool (10 day timer)
15. Add/fire agents (auto-redistribute leads)
16. Blacklist management
17. Full leaderboard
18. In-app notifications
19. Submission calendar (who submitted today)
20. Deploy to Railway
```

---

## ⚠️ Important Business Rules Summary

```
1. Agent submits ONCE per day before 11pm
2. Duplicate submission → error message
3. Duplicate phone number → first wins, second warned
4. Data locked immediately after submission
5. Only admin can unlock to correct
6. B.V + N.R → swap pool after 4 days (weekends count)
7. Autre ville + P.I → archived immediately
8. Max 3 swaps per lead → then main archive
9. Swap assignment is RANDOM (not same agent twice)
10. RDV no-show → back to pool after 10 days with flag
11. Fired agent → immediate access loss
12. Fired agent leads → auto-redistributed randomly
13. New agent sees lead history but NOT agent names
14. Admin sees everything including agent names in history
15. Blacklisted numbers blocked on submission silently
16. Phone validation: 10 digits, starts with 06 or 07
17. International numbers (+ prefix) allowed but flagged
18. Agent sees only their own RDVs
19. Agent sees own leaderboard position only (no other names)
20. Admin sees full leaderboard with names
```

---

## 🚀 First Command When Starting

```
Read this entire CLAUDE.md file first.
Then start with Day 1 Step 1:
Set up the FastAPI project structure and install all requirements.
Ask me for the .env values before connecting to any external services.
Build the frontend in Arabic RTL from the start, not as an afterthought.
```
