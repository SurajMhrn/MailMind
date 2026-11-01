"""Track sent cold emails and write follow-ups for the ones without a reply."""
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .config import ROOT
from .llm import LLM
from .mailer import days_since

LOG_PATH = ROOT / "data" / "sent.json"

FOLLOWUP_PROMPT = """Write follow-up email #{n} for a cold email a job seeker sent {days} days ago
that got no reply. Rules:
- {lo}-{hi} words. Polite, never guilt-tripping ("just bumping this", "did you see my email" are banned).
- Add ONE small piece of new value: a different relevant skill or project from the original
  evidence, or a short note of continued interest in the company. Do not invent facts.
- Restate the call to action in one easy-to-answer question.
- {final}
- Sign off with: {sender}
Return ONLY a JSON object: {{"body": "<the follow-up>"}}

Original subject: {subject}
Original email:
{body}

Evidence from the resume used originally:
{evidence}
{extra}"""


@dataclass
class SentEmail:
    to: str
    subject: str
    body: str
    message_id: str
    target: str = ""  # company / role, for display
    evidence: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    sent_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    status: str = "awaiting"  # awaiting | replied | bounced | closed
    followups: list[dict] = field(default_factory=list)  # {body, message_id, sent_at}

    @property
    def last_sent_at(self) -> str:
        return self.followups[-1]["sent_at"] if self.followups else self.sent_at

    @property
    def last_message_id(self) -> str:
        return self.followups[-1]["message_id"] if self.followups else self.message_id

    @property
    def references(self) -> str:
        """Earlier Message-IDs in the thread; send_email appends the one being replied to."""
        chain = [self.message_id] + [f["message_id"] for f in self.followups]
        return " ".join(chain[:-1])

    def followup_due_in(self, schedule: list[int]) -> int | None:
        """Days until the next follow-up is due (<= 0 means due now); None if no more are planned."""
        if self.status != "awaiting" or len(self.followups) >= len(schedule):
            return None
        return schedule[len(self.followups)] - days_since(self.last_sent_at)


class SentLog:
    """Sent emails, persisted as JSON in data/sent.json."""

    def __init__(self, path=None):
        self.path = path or LOG_PATH  # read at call time so tests can point it at a temp file

    def all(self) -> list[SentEmail]:
        if not self.path.exists():
            return []
        return [SentEmail(**row) for row in json.loads(self.path.read_text(encoding="utf-8"))]

    def _save(self, rows: list[SentEmail]) -> None:
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text(json.dumps([asdict(r) for r in rows], indent=2), encoding="utf-8")

    def add(self, email: SentEmail) -> None:
        self._save(self.all() + [email])

    def update(self, email: SentEmail) -> None:
        self._save([email if r.id == email.id else r for r in self.all()])

    def delete(self, email_id: str) -> None:
        self._save([r for r in self.all() if r.id != email_id])


def generate_followup(llm: LLM, email: SentEmail, sender: str, schedule: list[int], extra: str = "") -> str:
    n = len(email.followups) + 1
    final = ("This is the LAST follow-up: say you won't keep emailing and leave the door open."
             if n >= len(schedule) else "Keep it light.")
    lo, hi = (40, 80) if n == 1 else (30, 60)
    data = llm.json(FOLLOWUP_PROMPT.format(
        n=n, days=days_since(email.last_sent_at), lo=lo, hi=hi, final=final, sender=sender or "the candidate",
        subject=email.subject, body=email.body,
        evidence="\n".join(f"- {e}" for e in email.evidence) or "(none)",
        extra=f"\nExtra instructions from the candidate: {extra}" if extra else "",
    ))
    return str(data.get("body") or "").replace("\\n", "\n").strip()


def reply_subject(subject: str) -> str:
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"
