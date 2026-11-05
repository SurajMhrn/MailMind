"""Bulk send: one personalised cold email per opening in a spreadsheet.

Each opening gets a single grounded model call (resume chunks + the job details)
instead of the full agent loop, so a batch of 20 fits Groq's free-tier limits.
Nothing is sent from here: the UI shows every draft for review first, and sends
with a delay between emails and a daily cap.
"""
from dataclasses import dataclass, field
from datetime import date, datetime

from .agent import EmailDraft, EmailStyle, fill_sender_name, find_placeholders
from .followup import SentLog
from .llm import LLM
from .rag import KnowledgeBase
from .scout import rank_emails
from .scraper import EMAIL_RE, fetch_page, find_emails

PROMPT = """Write a cold email from a job seeker to the hiring team for the opening below.
Use ONLY facts from the candidate's resume excerpts; never invent or inflate skills, numbers
or experience. Greet the "{company} team" (we don't know who reads this inbox). Mention the
role by name and connect 2-3 specific resume facts to what the job needs.

{rules}

Return ONLY a JSON object: {{"subject": "...", "body": "...", "evidence": ["resume fact used", "..."]}}

Opening:
- Role: {title}
- Company: {company}
- Location: {location}
- Posting: {job_url}
- Description: {description}

Candidate's resume excerpts:
{resume}

Sender name: {sender}"""

# Status of a row before drafting.
READY, NO_EMAIL, INVALID, DUPLICATE, ALREADY, LOOKUP = (
    "ready", "no email", "invalid email", "duplicate in sheet", "already emailed", "find email on job page")


@dataclass
class Opening:
    row: int
    to: str
    title: str
    company: str
    job_url: str = ""
    location: str = ""
    description: str = ""
    status: str = READY
    subject: str = ""
    body: str = ""
    evidence: list[str] = field(default_factory=list)
    detail: str = ""

    @property
    def label(self) -> str:
        return " at ".join(p for p in (self.title, self.company) if p) or self.to or f"row {self.row}"


def plan(rows: list[dict], sent_log: SentLog) -> list[Opening]:
    """Turn spreadsheet rows into openings, marking the ones that can't or shouldn't be emailed."""
    emailed = {e.to.lower() for e in sent_log.all()}
    seen: set[str] = set()
    out = []
    for r in rows:
        op = Opening(row=r["row"], to=r.get("email", "").lower(), title=r.get("title", ""), company=r.get("company", ""),
                     job_url=r.get("job_url", ""), location=r.get("location", ""), description=r.get("description", ""))
        if not op.to:
            op.status = INVALID if r.get("raw_email") else (LOOKUP if op.job_url else NO_EMAIL)
        elif op.to in emailed:
            op.status = ALREADY
        elif op.to in seen:
            op.status = DUPLICATE
        seen.add(op.to)
        out.append(op)
    return out


def find_email_on_page(op: Opening) -> bool:
    """For a row with only a job link: look for an address on the posting itself."""
    try:
        page = fetch_page(op.job_url, max_chars=6000)
    except Exception as exc:
        op.detail = f"couldn't open the job link ({type(exc).__name__})"
        return False
    emails = rank_emails(find_emails(page["text"] + " " + " ".join(page["emails"])), "")
    if not emails:
        op.detail = "no email on the job page"
        return False
    op.to, op.status, op.detail = emails[0], READY, "email found on the job page"
    if not op.description:
        op.description = page["text"][:2000]
    return True


def draft(llm: LLM, kb: KnowledgeBase, op: Opening, style: EmailStyle, sender: str) -> None:
    query = " ".join([op.title, op.description[:400]])
    chunks = kb.search(query, k=4) or kb.chunks[:4]
    data = llm.json(PROMPT.format(
        rules=style.rules(), title=op.title or "(not given)", company=op.company or "the company",
        location=op.location or "(not given)", job_url=op.job_url or "(not given)",
        description=(op.description or "(not given)")[:1500],
        resume="\n\n".join(c.text for c in chunks)[:3500], sender=sender or "the candidate",
    ), temperature=0.4)
    op.subject = str(data.get("subject") or "").strip()
    op.body = str(data.get("body") or "").replace("\\n", "\n").strip()
    op.evidence = [str(e) for e in data.get("evidence") or []]
    op.body = fill_sender_name(EmailDraft(body=op.body), sender).body


def problems(op: Opening) -> list[str]:
    """What stops this draft from being sent."""
    issues = []
    if not EMAIL_RE.fullmatch(op.to or ""):
        issues.append("no valid recipient address")
    if not op.subject.strip() or not op.body.strip():
        issues.append("empty subject or body")
    placeholders = find_placeholders(op.subject + "\n" + op.body)
    if placeholders:
        issues.append("placeholders left: " + ", ".join(placeholders))
    return issues


def sent_today(sent_log: SentLog) -> int:
    today = date.today().isoformat()
    total = 0
    for e in sent_log.all():
        total += e.sent_at.startswith(today)
        total += sum(f["sent_at"].startswith(today) for f in e.followups)
    return total


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")
