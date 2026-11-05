# Cold Mail Agent — agentic RAG cold emails for job hunting

A Streamlit app with five pages:

| Page | What it does |
|---|---|
| **Write email** | Give it a job post URL or description. A hosted **Qwen** agent (free, via Groq) reads it, searches your resume (RAG), drafts a personalised cold email, and reviews and rewrites it. You can customise the tone, length, call to action and skills to highlight, refine the draft with AI ("make it shorter"), attach your resume, and send it from Gmail. |
| **Find openings** | Give it a company website. It finds the careers pages (and external job boards such as Greenhouse or Lever) with **Apify**, collects contact emails, and ranks every listed opening against your resume. One click drafts the email for the opening you pick. |
| **Job search** | Search LinkedIn job listings worldwide through **Apify**, filtered by how recently they were posted (24 hours, week, month), location (India, any city or country, or worldwide), job type (internship, full-time, part-time, contract), workplace (on-site, remote, hybrid) and experience level. Results download as an **Excel sheet** with the job link, the recruiter who posted it and any HR email found (from the posting or the company's website). |
| **Bulk send** | Upload that sheet, or your own Excel with an HR email or job-link column. MailMind drafts one resume-grounded email per opening, you review and edit them, and they are sent one at a time with a pause between emails, a daily cap, and no repeats to addresses you've already emailed. Every email is logged for follow-ups. |
| **Sent & follow-ups** | Every sent email is logged. The app checks your inbox for replies and bounces, tells you when a follow-up is due (4 and 7 days by default), drafts a short follow-up that adds something new, and sends it in the same thread. |

Put your resume (PDF, MD or TXT) in `knowledge/resume/`, or upload it from the sidebar.

**About HR emails:** LinkedIn listings rarely include one, so the Excel sheet also gives the job link and the
recruiter's LinkedIn profile. Rows without an email can still be used: Bulk send can look for an address on
the job page, and the rest are skipped and listed. Job search costs about $0.15 of Apify credit per 100 jobs.

**Interface:** top navigation that becomes a menu on phones (no horizontal scrolling at 375 px), a
clickable logo that returns home, a favicon, per-page browser titles and a meta description, a custom
404 page, a footer with page and setup links, toast messages for every action, and plain-English
errors (bad URLs, missing keys, rate limits, blocked sites, Gmail login problems). Drafts that still
contain placeholders such as `[Company]` cannot be sent, and the same email cannot be sent twice.

## How it works

```
Write email                              Find openings
───────────                              ─────────────
job URL / description                    company website
      │                                        │
      ▼                                        ▼
Agent loop (Qwen, tool calling)          homepage → Careers/Contact links (direct HTTP)
  fetch_webpage(url)                     Apify Website Content Crawler (JS-rendered)
  search_knowledge_base(q) × N  ◄─RAG─┐    + external job board (Greenhouse, Lever, …)
  → draft {to, subject, body}         │  emails ranked: careers@/hr@ › hello@ › other
      │                               │  Qwen: openings on the page × resume → fit score
      ▼                               │        │
Reviewer pass (style rules)           └────────┤ "Draft email for this opening"
      │                                        ▼
      ▼                               (goes to Write email, pre-filled)
You edit / refine with AI → tick "reviewed" → Send (Gmail SMTP, optional resume attachment)
      │
      ▼
Sent & follow-ups: IMAP reply/bounce check → follow-up due → draft → send as "Re:" in thread
```

- **Grounding:** the agent must search your resume before writing, is told not to inflate skill levels, and refine/follow-up steps may only reuse facts already in the draft. The evidence used is shown under each draft.
- **You stay in control:** the model has no send tool. Every email and follow-up is sent only when you tick "reviewed" and click Send.
- **Rate limits:** Groq's free tier allows about 7k input tokens per minute; one email takes about 5–20k tokens. The client waits and retries automatically.

## Setup

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
```

Fill in `.env` (edit single lines; don't replace the whole file):

1. **Groq** (free): https://console.groq.com/keys → `GROQ_API_KEY`
2. **Gmail** (sending + reply checking):
   1. Turn on 2-Step Verification: https://myaccount.google.com/signinoptions/twosv
   2. Create an App Password: https://myaccount.google.com/apppasswords
   3. Set `SMTP_USER=you@gmail.com`, `SMTP_PASSWORD=<16 letters, no spaces>`, `SENDER_NAME=Your Name`
3. **Apify** (Find openings): https://console.apify.com/settings/integrations → copy your API token into `APIFY_API_TOKEN`. The free plan's monthly credit covers many company scans; one scan of 25 pages costs a few cents.
4. Optional: `FOLLOWUP_DAYS=4,7` sets when follow-ups are due.

Run:

```bash
.venv\Scripts\streamlit run app.py
```

## Deploying (Streamlit Community Cloud, free)

1. Sign in at https://share.streamlit.io with GitHub and choose **Create app → Deploy a public app from GitHub**.
2. Repository `SurajMhrn/MailMind`, branch `main`, main file `app.py`. Under **Advanced settings** pick Python 3.12 or newer.
3. In **Secrets**, paste your settings in TOML form (same names as `.env`), and always include `APP_PASSWORD`:
   ```toml
   APP_PASSWORD = "choose-a-strong-password"
   GROQ_API_KEY = "..."
   APIFY_API_TOKEN = "..."
   SMTP_USER = "you@gmail.com"
   SMTP_PASSWORD = "your-16-letter-app-password"
   SENDER_NAME = "Your Name"
   ```
4. Deploy, open the app, sign in, and upload your resume from the sidebar.

Hosted storage is temporary: the uploaded resume and the sent-email log reset when the app restarts or
sleeps, so re-upload the resume after a restart and keep the bulk-send reports you download.

## Testing and QA

```bash
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
.venv\Scripts\python tools\check_links.py
.venv\Scripts\python tools\e2e_screenshots.py --url http://localhost:8501
```

- `pytest`: 70 tests covering the agent loop (with a fake model), validation, placeholders, error messages, retrieval, scouting heuristics, email threading and follow-ups, job search and Excel import/export, bulk-send safeguards, plus UI tests for every page (errors, success messages, send safeguards).
- `tools/check_links.py`: requests every URL in the code and docs and reports broken ones.
- `tools/e2e_screenshots.py`: drives the running app in Microsoft Edge (Playwright) at desktop and phone sizes, checks titles, meta tags, horizontal scrolling, the mobile menu, the 404 page and the main flows, and saves screenshots to `docs/screenshots/`. It calls Groq and runs one Apify crawl.
- `tools/compress_images.py`: shrinks the PNG assets and diagrams (about 60% smaller).

Results of the last run are in [docs/QA_REPORT.md](docs/QA_REPORT.md).

## Project layout

```
app.py               Streamlit UI (three pages, 404 page, footer)
agent/config.py      .env settings, provider presets
agent/llm.py         Qwen client: retries on rate limits, token counting, JSON parsing
agent/agent.py       cold-email agent loop, tools, reviewer pass, style options, AI refine
agent/scout.py       Find openings: careers-page discovery, job boards, email ranking, resume matching
agent/apify.py       Apify Website Content Crawler (REST API)
agent/followup.py    sent-email log (data/sent.json), follow-up schedule and drafting
agent/mailer.py      SMTP send (threading headers, attachments), IMAP reply/bounce check
agent/rag.py         document loading, chunking, BM25 / embedding retrieval
agent/scraper.py     page fetching, link and email extraction, URL validation
agent/errors.py      turns API, network and Gmail errors into messages people can act on
agent/jobsearch.py   worldwide job search: LinkedIn listings via Apify, filters, HR email lookup
agent/excel.py       Excel export of jobs, reading uploaded sheets, bulk-send report
agent/bulk.py        bulk send: row checks, one-call grounded drafts, daily limit
assets/              logo and favicon (make_assets.py regenerates them)
.streamlit/          theme, upload limit, hidden developer menu
knowledge/resume/    your resume and project docs
tests/               pytest unit and UI tests
tools/               link checker, browser E2E/screenshots, image compression
docs/                diagrams, report, QA report, screenshots
```

## Limits worth knowing

- Many large companies don't publish recruiter emails, only `info@` or `marketing@`. The app labels each address and suggests finding the recruiter on LinkedIn when no hiring address exists. It never guesses addresses.
- LinkedIn, Naukri and Indeed pages can't be scraped. Paste the job description instead.

## Responsible use

Email recruiters one at a time, and send at most two follow-ups (the default). Gmail limits personal
accounts to about 500 emails a day and may suspend accounts that look like bulk senders.
