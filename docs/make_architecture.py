"""Generates docs/architecture.svg (and an HTML wrapper used to render the PNG).

Run:  .venv\\Scripts\\python docs\\make_architecture.py
"""
from html import escape
from pathlib import Path

OUT = Path(__file__).parent
W, H = 1400, 1060
COLORS = {  # fill, stroke, title, subtitle
    "llm": ("#EEEDFE", "#534AB7", "#3C3489", "#534AB7"),
    "data": ("#E1F5EE", "#0F6E56", "#085041", "#0F6E56"),
    "ext": ("#FAECE7", "#993C1D", "#712B13", "#993C1D"),
    "you": ("#F1EFE8", "#888780", "#2C2C2A", "#5F5E5A"),
}
COLUMNS = [
    ("1 · Find openings", "agent/scout.py", [
        ("you", "Company website + preference", "e.g. “data analyst internship”"),
        ("you", "Homepage reader", "requests + BeautifulSoup → careers links"),
        ("ext", "Apify Website Content Crawler", "JS-rendered pages, career-like links"),
        ("ext", "External job board crawl", "Greenhouse / Lever / Workable index"),
        ("you", "Email extraction & ranking", "careers@, hr@ first · noreply@ dropped"),
        ("llm", "Qwen: match openings to resume", "openings + fit score + reason"),
        ("you", "You pick an opening + recipient", "“Draft email for this opening”"),
    ]),
    ("2 · Write email", "agent/agent.py", [
        ("you", "Target + style options", "job URL / JD, recipient, tone, CTA"),
        ("llm", "Agent loop (Qwen tool calling)", "fetch_webpage · search_knowledge_base ×N"),
        ("llm", "Reviewer pass", "critique vs style rules → rewrite"),
        ("you", "Guardrails", "real email only · no inflated claims"),
        ("you", "You review & edit", "refine with AI · attach resume PDF"),
        ("ext", "Send via Gmail SMTP", "Message-ID kept for threading"),
        ("data", "Log to data/sent.json", "to, subject, body, evidence"),
    ]),
    ("3 · Sent & follow-ups", "agent/followup.py", [
        ("data", "Sent log", "awaiting · replied · bounced · closed"),
        ("ext", "Inbox check via Gmail IMAP", "replies and bounce notices"),
        ("you", "Follow-up schedule", "due after 4 and 7 days"),
        ("llm", "Qwen: draft follow-up", "40–80 words, one new proof point"),
        ("you", "You review", "edit, tick “reviewed”"),
        ("ext", "Send as “Re:” in same thread", "In-Reply-To + References headers"),
        ("you", "Stop", "on reply, bounce or 2 follow-ups"),
    ]),
]
SHARED = [
    ("data", "RAG knowledge base", "rag.py · resume PDF → chunks → BM25"),
    ("llm", "Qwen LLM on Groq", "llm.py · qwen3.8-27b · retry on 429"),
    ("ext", "Apify API", "apify.py · Website Content Crawler"),
    ("ext", "Gmail SMTP + IMAP", "mailer.py · App Password from .env"),
]
LEGEND = [("llm", "Qwen LLM step"), ("data", "Data / retrieval (RAG, sent log)"),
          ("ext", "External service (Apify, Gmail)"), ("you", "Your input / app logic")]

COL_X, COL_W, COL_TOP, COL_BOTTOM = [40, 490, 940], 420, 104, 780
BOX_W, BOX_H, STEP_Y0, STEP_DY = 360, 62, 168, 86

parts = [
    f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
    'font-family="Segoe UI, Arial, sans-serif">',
    '<defs><marker id="a" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" '
    'orient="auto-start-reverse"><path d="M2 1L8 5L2 9" fill="none" stroke="context-stroke" '
    'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></marker></defs>',
    f'<rect width="{W}" height="{H}" fill="#FFFFFF"/>',
    '<text x="40" y="52" font-size="26" font-weight="600" fill="#2C2C2A">Cold Mail Agent — pipeline architecture</text>',
    '<text x="40" y="80" font-size="15" fill="#5F5E5A">Agentic RAG cold emails for job hunting · '
    'Streamlit UI (app.py) · Qwen via Groq · Apify · Gmail</text>',
]


def box(x, y, w, h, kind, title, sub):
    fill, stroke, tc, sc = COLORS[kind]
    cx = x + w / 2
    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{fill}" stroke="{stroke}" stroke-width="1"/>')
    parts.append(f'<text x="{cx}" y="{y + h / 2 - 9}" text-anchor="middle" dominant-baseline="central" '
                 f'font-size="16" font-weight="600" fill="{tc}">{escape(title)}</text>')
    parts.append(f'<text x="{cx}" y="{y + h / 2 + 12}" text-anchor="middle" dominant-baseline="central" '
                 f'font-size="13.5" fill="{sc}">{escape(sub)}</text>')


def arrow(d, dashed=False, color="#888780"):
    dash = ' stroke-dasharray="6 5"' if dashed else ""
    parts.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.6"{dash} marker-end="url(#a)"/>')


# ---- three pipelines ----
for (title, module, steps), cx0 in zip(COLUMNS, COL_X):
    parts.append(f'<rect x="{cx0}" y="{COL_TOP}" width="{COL_W}" height="{COL_BOTTOM - COL_TOP}" rx="16" '
                 'fill="#FBFBFA" stroke="#B4B2A9" stroke-width="1" stroke-dasharray="6 4"/>')
    parts.append(f'<text x="{cx0 + 24}" y="{COL_TOP + 32}" font-size="18" font-weight="600" fill="#2C2C2A">{escape(title)}</text>')
    parts.append(f'<text x="{cx0 + COL_W - 24}" y="{COL_TOP + 32}" text-anchor="end" font-size="13" '
                 f'font-family="Consolas, monospace" fill="#5F5E5A">{escape(module)}</text>')
    bx = cx0 + (COL_W - BOX_W) / 2
    for i, (kind, t, s) in enumerate(steps):
        y = STEP_Y0 + i * STEP_DY
        box(bx, y, BOX_W, BOX_H, kind, t, s)
        if i < len(steps) - 1:
            arrow(f"M{bx + BOX_W / 2} {y + BOX_H + 3} L{bx + BOX_W / 2} {y + STEP_DY - 4}")

# ---- hand-offs between pipelines (routed through the gaps between columns) ----
last_y = STEP_Y0 + 6 * STEP_DY + BOX_H / 2
first_y = STEP_Y0 + BOX_H / 2
for src_col, gap_x, label in ((0, 475, "pre-fills the target"), (1, 925, "logged for follow-up")):
    sx = COL_X[src_col] + (COL_W + BOX_W) / 2
    tx = COL_X[src_col + 1] + (COL_W - BOX_W) / 2
    arrow(f"M{sx + 3} {last_y} L{gap_x} {last_y} L{gap_x} {first_y} L{tx - 4} {first_y}", dashed=True, color="#534AB7")
    parts.append(f'<text x="{gap_x}" y="{COL_TOP - 8}" text-anchor="middle" font-size="12.5" fill="#534AB7">{label}</text>')

# ---- shared services ----
SH_TOP, SH_H = 822, 150
parts.append(f'<rect x="40" y="{SH_TOP}" width="1320" height="{SH_H}" rx="16" fill="#FBFBFA" stroke="#B4B2A9" '
             'stroke-width="1" stroke-dasharray="6 4"/>')
parts.append(f'<text x="64" y="{SH_TOP + 32}" font-size="18" font-weight="600" fill="#2C2C2A">Shared services</text>')
parts.append(f'<text x="1336" y="{SH_TOP + 32}" text-anchor="end" font-size="13" fill="#5F5E5A">'
             'configured in .env · keys never leave your machine except to their own service</text>')
for i, (kind, t, s) in enumerate(SHARED):
    box(70 + i * 320, SH_TOP + 56, 300, 66, kind, t, s)
for cx0 in COL_X:
    x = cx0 + COL_W / 2
    arrow(f"M{x} {COL_BOTTOM + 4} L{x} {SH_TOP - 6}")
    parts.append(f'<text x="{x + 10}" y="{(COL_BOTTOM + SH_TOP) / 2 + 4}" font-size="12.5" fill="#5F5E5A">uses</text>')

# ---- legend ----
lx = 40
for kind, label in LEGEND:
    fill, stroke, *_ = COLORS[kind]
    parts.append(f'<rect x="{lx}" y="{H - 52}" width="18" height="18" rx="4" fill="{fill}" stroke="{stroke}"/>')
    parts.append(f'<text x="{lx + 28}" y="{H - 38}" font-size="14" fill="#444441">{escape(label)}</text>')
    lx += 28 + len(label) * 7.4 + 40
parts.append(f'<text x="{W - 40}" y="{H - 38}" text-anchor="end" font-size="13" fill="#888780">'
             'Nothing is sent without your explicit review and click.</text>')
parts.append("</svg>")

svg = "\n".join(parts)
(OUT / "architecture.svg").write_text(svg, encoding="utf-8")
(OUT / "_render.html").write_text(
    f'<!doctype html><html><body style="margin:0;background:#fff">{svg}</body></html>', encoding="utf-8")
print("wrote", OUT / "architecture.svg")
