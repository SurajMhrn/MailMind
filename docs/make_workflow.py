"""Generates docs/workflow.svg: the detailed technical workflow (every step with its
tools, APIs and parameters). Render to PNG the same way as architecture.svg.

Run:  .venv\\Scripts\\python docs\\make_workflow.py
"""
from html import escape
from pathlib import Path

OUT = Path(__file__).parent
W, H = 1700, 1600
FONT = "Segoe UI, Arial, sans-serif"
COLORS = {  # fill, stroke, title, detail
    "llm": ("#EEEDFE", "#534AB7", "#3C3489", "#534AB7"),
    "data": ("#E1F5EE", "#0F6E56", "#085041", "#0F6E56"),
    "ext": ("#FAECE7", "#993C1D", "#712B13", "#993C1D"),
    "you": ("#F1EFE8", "#888780", "#2C2C2A", "#5F5E5A"),
}
late = []  # drawn after everything else
COLS = [90, 400, 710, 1020, 1330]  # 5 columns of 280px boxes
BW, BH = 280, 122

parts = [
    f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="{FONT}">',
    '<defs><marker id="a" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" '
    'orient="auto-start-reverse"><path d="M2 1L8 5L2 9" fill="none" stroke="context-stroke" '
    'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></marker></defs>',
    f'<rect width="{W}" height="{H}" fill="#FFFFFF"/>',
    '<text x="50" y="52" font-size="26" font-weight="600" fill="#2C2C2A">'
    'Cold Mail Agent — technical workflow</text>',
    '<text x="50" y="80" font-size="15" fill="#5F5E5A">Every step with its tools, APIs and parameters · '
    'Streamlit (app.py) · Qwen qwen3.8-27b on Groq · Apify Website Content Crawler · Gmail SMTP/IMAP</text>',
]


def box(col, y, kind, title, lines):
    x = COLS[col]
    fill, stroke, tc, dc = COLORS[kind]
    parts.append(f'<rect x="{x}" y="{y}" width="{BW}" height="{BH}" rx="8" fill="{fill}" stroke="{stroke}" stroke-width="1"/>')
    parts.append(f'<text x="{x + 14}" y="{y + 26}" font-size="15" font-weight="600" fill="{tc}">{escape(title)}</text>')
    for i, line in enumerate(lines):
        parts.append(f'<text x="{x + 14}" y="{y + 50 + i * 19}" font-size="13" fill="{dc}">{escape(line)}</text>')


def arrow(d, dashed=False, color="#888780", both=False, on_top=False):
    extra = (' stroke-dasharray="6 5"' if dashed else "") + (' marker-start="url(#a)"' if both else "")
    path = f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.6"{extra} marker-end="url(#a)"/>'
    # Hand-offs cross into the next section, whose background would cover them.
    (late if on_top else parts).append(path)


def label(x, y, text, color="#534AB7", anchor="middle"):
    parts.append(f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-size="13" fill="{color}">{escape(text)}</text>')


def section(top, bottom, title, module):
    parts.append(f'<rect x="50" y="{top}" width="1600" height="{bottom - top}" rx="16" fill="#FBFBFA" '
                 'stroke="#B4B2A9" stroke-width="1" stroke-dasharray="6 4"/>')
    parts.append(f'<text x="90" y="{top + 34}" font-size="19" font-weight="600" fill="#2C2C2A">{escape(title)}</text>')
    parts.append(f'<text x="1610" y="{top + 34}" text-anchor="end" font-size="13" font-family="Consolas, monospace" '
                 f'fill="#5F5E5A">{escape(module)}</text>')


def chain(row_y, cols):
    """Left-to-right arrows between neighbouring boxes of one row."""
    for a, b in zip(cols, cols[1:]):
        arrow(f"M{COLS[a] + BW + 3} {row_y + BH / 2} L{COLS[b] - 4} {row_y + BH / 2}")


def cx(col):
    return COLS[col] + BW / 2


# ================= A. Find openings =================
A_TOP, A1, A2 = 110, 164, 346
section(A_TOP, A2 + BH + 30, "A · Find openings — company website → ranked openings + contact emails",
        "agent/scout.py · agent/apify.py · agent/scraper.py")
box(0, A1, "you", "Input", ["company website", "preference, e.g. “Python internship”",
                             "pages to crawl: 10–60 (default 25)"])
box(1, A1, "you", "Homepage reader", ["requests + BeautifulSoup (free, ~1 s)", "→ every link, email, page text",
                                       "homepage kept as page 0"])
box(2, A1, "you", "Link classification", ["ATS_RE → external job board", "CAREER_RE → careers links (top 3)",
                                           "contact → 1 contact page", "none → /, /careers, /jobs"])
box(3, A1, "ext", "Apify Website Content Crawler", ["POST /v2/acts/…/runs · 240 s", "playwright:adaptive, depth 2",
                                                    "includeUrlGlobs: *career* *job* …", "htmlTransformer none → Markdown"])
box(4, A1, "ext", "Poll run + read dataset", ["GET actor-runs?waitForFinish=30", "abort if past the deadline",
                                              "GET datasets/{id}/items", "dedupe pages by URL"])
chain(A1, [0, 1, 2, 3, 4])
arrow(f"M{cx(4)} {A1 + BH + 3} L{cx(4)} {A1 + BH + 30} L{cx(0)} {A1 + BH + 30} L{cx(0)} {A2 - 4}")
box(0, A2, "ext", "External job board crawl", ["first ATS link → board_root()", "e.g. greenhouse.io/acme (index)",
                                               "10 pages · depth 1"])
box(1, A2, "you", "Email extraction & ranking", ["regex over every crawled page", "drop noreply@ privacy@ legal@ …",
                                                 "careers@/hr@ › hello@ › other", "company's own domain first"])
box(2, A2, "you", "Careers page scoring", ["URL path ×3 · title ×2 · index +4", "job links ×2 · board index +10",
                                           "top 3 pages with score ≥ 3"])
box(3, A2, "llm", "Qwen: match openings (1 call)", ["resume ≤ 3.8k chars", "pages ≤ 6.5k chars, menus removed",
                                                    "→ title, url, fit score, reason", "temperature 0.2 · no invented jobs"])
box(4, A2, "you", "Post-process → UI", ["clamp score 0–100", "difflib fixes job URLs (≥ 0.75)",
                                         "sort by score · label emails", "you pick opening + recipient"])
chain(A2, [0, 1, 2, 3, 4])

# hand-off A → B
A_BOTTOM, B_TOP = A2 + BH + 30, 552
B1, B2 = B_TOP + 54, B_TOP + 54 + BH + 70
mid = (A_BOTTOM + B_TOP) / 2 - 4
arrow(f"M{cx(4)} {A2 + BH + 3} L{cx(4)} {mid} L70 {mid} L70 {B1 + BH / 2} L{COLS[0] - 4} {B1 + BH / 2}",
      dashed=True, color="#534AB7", on_top=True)
label(850, mid + 18, "“Draft email for this opening” → pre-fills the target and starts the agent automatically")

# ================= B. Write email =================
section(B_TOP, B2 + BH + 30, "B · Write email — agentic RAG over your resume", "agent/agent.py · agent/mailer.py")
box(0, B1, "you", "Target + EmailStyle", ["job URL / JD text / recipient", "tone · length · call to action",
                                         "skills to highlight · extra notes", "attach-resume flag"])
box(1, B1, "llm", "Agent loop (≤ 8 steps)", ["Qwen tool calling via Groq", "must search before writing",
                                             "unseen chunks only (saves tokens)", "→ JSON {to, subject, body, evidence}"])
box(2, B1, "llm", "Reviewer pass", ["critique vs style rules", "no inflated claims · one CTA",
                                    "rewrite + one-line critique", "on failure → keep first draft"])
box(3, B1, "you", "Recipient guardrails", ["keep only a valid email address", "typed address always wins",
                                           "else first email found on page"])
box(4, B1, "you", "You review & edit", ["edit To / Subject / Body", "Refine with AI (no new facts)",
                                        "attach resume PDF", "tick “reviewed” to enable Send"])
chain(B1, [0, 1, 2, 3, 4])
box(0, B2, "you", "Tool: fetch_webpage", ["requests + BS4, ≤ 5,000 chars", "strips nav, header, footer",
                                         "returns title, text, emails"])
box(1, B2, "data", "Tool: search_knowledge_base", ["BM25 over resume chunks", "top 4 · k1 = 1.5 · b = 0.75",
                                                   "returns only new chunks"])
box(2, B2, "you", "Fallback pipeline", ["on a malformed tool call (400)", "fetch → search → write JSON",
                                        "then the reviewer as usual"])
box(3, B2, "data", "Log sent email", ["data/sent.json", "Message-ID · evidence · target", "status = awaiting"])
box(4, B2, "ext", "Send via Gmail SMTP", ["smtp.gmail.com:587 + STARTTLS", "App Password login",
                                          "Message-ID via make_msgid", "attachments by MIME type"])
gap_y = B1 + BH + 35
arrow(f"M{cx(1)} {B1 + BH + 4} L{cx(1)} {B2 - 4}", both=True)  # loop ⇄ search
arrow(f"M{COLS[1] + 40} {B1 + BH + 4} L{COLS[1] + 40} {gap_y} L{cx(0)} {gap_y} L{cx(0)} {B2 - 4}", both=True)
arrow(f"M{COLS[1] + BW - 40} {B1 + BH + 4} L{COLS[1] + BW - 40} {gap_y} L{COLS[2] + 70} {gap_y} "
      f"L{COLS[2] + 70} {B2 - 4}", dashed=True)
arrow(f"M{COLS[2] + BW - 70} {B2 - 3} L{COLS[2] + BW - 70} {B1 + BH + 4}", dashed=True)
label(COLS[1] + 52, gap_y - 8, "tool calls", "#5F5E5A", "start")
label(COLS[2] + 80, gap_y - 8, "on error", "#5F5E5A", "start")
arrow(f"M{cx(4)} {B1 + BH + 4} L{cx(4)} {B2 - 4}")
arrow(f"M{COLS[4] - 3} {B2 + BH / 2} L{COLS[3] + BW + 4} {B2 + BH / 2}")

# hand-off B → C
B_BOTTOM, C_TOP = B2 + BH + 30, B2 + BH + 30 + 54
C1 = C_TOP + 54
mid = (B_BOTTOM + C_TOP) / 2 - 4
arrow(f"M{cx(3)} {B2 + BH + 3} L{cx(3)} {mid} L70 {mid} L70 {C1 + BH / 2} L{COLS[0] - 4} {C1 + BH / 2}",
      dashed=True, color="#534AB7", on_top=True)
label(850, mid + 18, "every sent email is tracked for replies, bounces and follow-ups")

# ================= C. Sent & follow-ups =================
section(C_TOP, C1 + BH + 30, "C · Sent & follow-ups — reply tracking and threaded follow-ups",
        "agent/followup.py · agent/mailer.py")
box(0, C1, "data", "Sent log", ["data/sent.json", "awaiting · replied · bounced · closed",
                                "followups[] with Message-IDs"])
box(1, C1, "ext", "Inbox check (IMAP)", ["imap.gmail.com · INBOX read-only", "bounce: FROM mailer-daemon",
                                         "reply: FROM recipient OR subject", "only mail since the send date"])
box(2, C1, "you", "Follow-up due?", ["schedule[n] − days since last email", "default: after 4, then 7 days",
                                     "max 2 follow-ups, then stop"])
box(3, C1, "llm", "Qwen: draft follow-up", ["#1: 40–80 words · #2: 30–60", "adds one new proof point",
                                            "no guilt-tripping · one question", "last one closes politely"])
box(4, C1, "ext", "Send “Re:” in same thread", ["after your review + tick", "In-Reply-To = last Message-ID",
                                                "References = earlier IDs", "appended to followups[]"])
chain(C1, [0, 1, 2, 3, 4])

# ================= Shared services =================
S_TOP = C1 + BH + 30 + 50
S1 = S_TOP + 76
parts.append(f'<rect x="50" y="{S_TOP}" width="1600" height="{S1 + BH + 30 - S_TOP}" rx="16" fill="#FBFBFA" '
             'stroke="#B4B2A9" stroke-width="1" stroke-dasharray="6 4"/>')
parts.append(f'<text x="90" y="{S_TOP + 34}" font-size="19" font-weight="600" fill="#2C2C2A">Shared services</text>')
label(90, S_TOP + 58, "Purple steps call llm.py · search_knowledge_base reads rag.py · orange steps use apify.py "
      "or mailer.py · all settings come from .env", "#5F5E5A", "start")
box(0, S1, "data", "rag.py — knowledge base", ["pypdf → ~800-char chunks", "150-char overlap per chunk",
                                               "BM25 default, embeddings optional", ".npy embedding cache in data/"])
box(1, S1, "llm", "llm.py — Qwen client", ["OpenAI SDK → Groq qwen3.8-27b", "max_retries=6, honours retry-after",
                                           "strips <think> · tolerant JSON", "counts tokens per run"])
box(2, S1, "ext", "apify.py — crawler API", ["REST v2: runs · actor-runs · datasets", "Bearer APIFY_API_TOKEN",
                                             "~$0.03–0.10 per company scan", "free plan: $5 credit / month"])
box(3, S1, "ext", "mailer.py — Gmail", ["SMTP send · IMAP search", "App Password (2-step verified)",
                                        "validates the address first", "readable login errors"])
box(4, S1, "you", "Config, state & storage", [".env (git-ignored)", "knowledge/resume/ · data/sent.json",
                                              "Streamlit session_state", "pending-update + rerun pattern"])

parts += late

# ================= legend =================
ly = S1 + BH + 30 + 46
lx = 50
for kind, text in [("llm", "Qwen LLM step"), ("data", "Data / retrieval"), ("ext", "External service (Apify, Gmail)"),
                   ("you", "Your input / app logic")]:
    fill, stroke, *_ = COLORS[kind]
    parts.append(f'<rect x="{lx}" y="{ly - 14}" width="18" height="18" rx="4" fill="{fill}" stroke="{stroke}"/>')
    parts.append(f'<text x="{lx + 28}" y="{ly}" font-size="14" fill="#444441">{escape(text)}</text>')
    lx += 28 + len(text) * 7.4 + 40
parts.append(f'<path d="M{lx + 10} {ly - 5} L{lx + 50} {ly - 5}" stroke="#534AB7" stroke-width="1.6" '
             'stroke-dasharray="6 5" fill="none" marker-end="url(#a)"/>')
label(lx + 60, ly, "hand-off between pipelines", "#444441", "start")
label(W - 50, ly, "Nothing is sent without your explicit review and click.", "#888780", "end")

height = ly + 30
parts[0] = parts[0].replace(f'height="{H}" viewBox="0 0 {W} {H}"', f'height="{height}" viewBox="0 0 {W} {height}"')
parts[3] = parts[3].replace(f'height="{H}"', f'height="{height}"')
parts.append("</svg>")
svg = "\n".join(parts)
(OUT / "workflow.svg").write_text(svg, encoding="utf-8")
(OUT / "_render.html").write_text(
    f'<!doctype html><html><body style="margin:0;background:#fff">{svg}</body></html>', encoding="utf-8")
print("wrote", OUT / "workflow.svg", "height", height)
