# Omani Dialect Sentiment Collector

A small two-role Flask app for crowdsourcing dialect-tagged sentences:

- **Participants** (`/`): read a topic prompt, write a sentence in their own
  Omani dialect, self-tag its sentiment (positive/neutral/negative), submit.
  No login required, no personal data requested.
- **Admin** (`/admin`): password-protected dashboard with live counts,
  a breakdown by topic, a quality-review table of recent submissions
  (showing the exact question each answer responds to, with delete for
  spam/test entries), and a one-click CSV export.

**Storage: PostgreSQL, not local SQLite.** This matters if you deploy to
Render (or most free PaaS platforms) — their filesystem is ephemeral, so a
local SQLite file gets silently wiped on every restart, redeploy, or idle
spin-down. Using a real hosted Postgres database means your data survives
independently of whatever happens to the app's compute instance.

## 1. Get a free Postgres database (5 minutes, before anything else)

Sign up at either:
- **[neon.tech](https://neon.tech)** (recommended, no credit card required), or
- **[supabase.com](https://supabase.com)**

Create a project/database, then copy its **connection string** — it looks
like `postgresql://user:password@host/dbname`. You'll set this as
`DATABASE_URL` below. Keep it private — it's effectively a password.

## 2. Run it locally (for testing)

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

export DATABASE_URL="postgresql://user:pass@host/dbname"   # from step 1
export ADMIN_PASSWORD="choose-a-strong-password"
export FLASK_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"

python app.py
```
(Windows `cmd`: use `set VARNAME=value`, no quotes — see earlier notes on that.)

Then open:
- http://localhost:5000 — the participant form
- http://localhost:5000/admin — the admin login

The database table is created automatically on first run — you don't need
to create it manually in Neon/Supabase.

## 3. Deploying to Render

1. Push this folder to a GitHub repository.
2. On [render.com](https://render.com), click **New → Web Service**, connect
   your GitHub repo.
3. Confirm/set:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `gunicorn app:app`
4. Under **Environment**, add three variables:
   - `DATABASE_URL` — your Neon/Supabase connection string
   - `ADMIN_PASSWORD` — your own password
   - `FLASK_SECRET_KEY` — a generated random string (see step 2's command)
5. Click **Create Web Service**. You get a free `onrender.com` URL with
   HTTPS automatically.

Note: the free Render instance still sleeps after ~15 minutes of no
traffic — the first visitor after a quiet period waits ~30-60 seconds for
it to wake up. That's now just a compute delay, not a data-loss risk,
since your data lives in Postgres, not on Render's disk.

## 4. Editing the questions later

Everything lives in the `TOPICS` list near the top of `app.py` — each entry
is `(key, label, prompt)`. You can freely add, remove, or reword entries;
everything else (validation, the random-topic picker, admin stats) reads
from this list automatically. The one rule: **every key must be unique**,
even if two questions share a display label — the app will refuse to start
(with a clear error) if you accidentally duplicate one, so you can't
silently reintroduce the bug we fixed here.

## 5. Data management notes (for your methodology section)

- **No personal data is collected** — no names, emails, or IP addresses are
  stored in the `responses` table.
- **Every row stores the exact question text**, not just a topic key — so
  even if you edit `TOPICS` again later, historical rows stay
  self-documenting about which exact prompt they answered. The server
  derives this from its own copy of `TOPICS` at submission time (not
  whatever the client sends), so it can't be spoofed.
- **Back up your Postgres data regularly** — Neon/Supabase free tiers are
  durable but not infinite; export a CSV via the admin panel periodically
  as your own archival copy, independent of any platform.
- **Rate limiting** is basic and in-memory (resets when the app restarts)
  — enough to blunt casual spam, not a substitute for a CAPTCHA if you get
  hit by bots. Add `Flask-Limiter` and/or hCaptcha to `/submit` if needed.
- **Quality control**: the admin dashboard's "recent submissions" table
  shows each answer next to the exact question it responded to, and lets
  you delete garbage/test entries as they arrive.
- **CSV schema**: `id, sentence, question, sentiment, topic, region, style,
  created_at`.

## 6. Before real data collection

This app is functionally ready, but for a dissertation you'll still want
(as discussed separately): IRB/ethics approval, a proper informed-consent
text on the landing page, and an independent-annotator validation pass
on a sample of submissions to report inter-annotator agreement.
