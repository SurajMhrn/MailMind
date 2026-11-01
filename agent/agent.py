"""The cold-email agent: a tool-calling loop over a hosted Qwen model.

The model decides which tools to call (read the target page, search the
knowledge base, ...) and finishes by returning the email as JSON. A second
"reviewer" pass critiques and tightens the draft. Sending is never a tool:
the human approves every email in the UI.
"""
import json
import re
from dataclasses import dataclass, field
from typing import Callable

from openai import BadRequestError

from .config import Settings
from .llm import LLM, parse_json
from .rag import KnowledgeBase
from .scraper import fetch_page, find_emails

MAX_STEPS = 8

SYSTEM_PROMPT = """You write cold emails on behalf of a job seeker to a recruiter or hiring manager.
The target is usually a job post or company careers page. Match the role's requirements to the
candidate's most relevant skills, projects and experience from their resume (the knowledge base).

Work like this:
1. Call fetch_webpage on the target URL (if one is given) to learn about the company / role.
2. Call search_knowledge_base several times with SPECIFIC queries (e.g. a skill, technology or
   responsibility from the job post) to find matching evidence. Never invent facts that are not in
   the knowledge base or the target page, and never inflate them (e.g. "AWS basics" must not
   become "AWS deployment experience").
3. When you have enough, reply with ONLY a JSON object, no other text:
{"to": "<recipient EMAIL ADDRESS found on the page, or empty string — never a name>",
  "subject": "<short, specific subject line, under 9 words>",
  "body": "<the email>",
  "evidence": ["<knowledge-base fact you used>", "..."]}"""

REVIEW_PROMPT = """You are a strict cold-email editor. Critique the draft below against the style
rules and these checks: personalised first line, specific proof points, one clear CTA, no
buzzwords, no claims beyond the evidence (skill levels must not be inflated), subject under 9
words. Then return an improved version as ONLY a JSON object with the same keys: "to", "subject",
"body", "evidence", plus "critique" (one or two sentences on what you changed).

{rules}

Target context:
{context}

Draft:
{draft}"""

REFINE_PROMPT = """Rewrite this cold email following the instruction below. Keep every fact
accurate: do not add skills, projects or numbers that are not already in the draft or the
evidence list. Return ONLY a JSON object with keys "subject", "body" and "critique" (one sentence
on what you changed).

Instruction: {instruction}

{rules}

Evidence from the resume:
{evidence}

Draft subject: {subject}
Draft body:
{body}"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "fetch_webpage",
            "description": "Fetch a web page (job post, company site, about page) and return its text "
            "plus any email addresses found on it.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "Full http(s) URL"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": "Search the candidate's resume and project notes for facts relevant to a "
            "query. Call it several times with focused queries.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
]

# ---- customisation options shown in the UI ----
TONES = {
    "Professional": "professional and polite",
    "Friendly": "warm and conversational, still respectful",
    "Confident": "direct and confident, without arrogance",
    "Enthusiastic": "energetic and genuinely excited about the company",
    "Formal": "formal and courteous",
}
LENGTHS = {"Short": (60, 100), "Medium": (100, 160), "Long": (160, 220)}
CTAS = {
    "Ask for a short call": "ask for a 10-15 minute call this week or next",
    "Ask to be considered for the role": "ask them to consider the candidate for the role / review the resume",
    "Ask for a referral": "ask whether they could refer the candidate or point to the right person",
    "Ask about open roles": "ask whether there are suitable openings now or soon",
    "Ask for advice": "ask for brief advice on how to stand out for roles at the company",
}


@dataclass
class EmailStyle:
    tone: str = "Professional"
    length: str = "Medium"
    cta: str = "Ask for a short call"
    highlight: str = ""  # skills / projects the user wants mentioned
    extra: str = ""  # free-text instructions
    attach_resume: bool = False

    def rules(self) -> str:
        lo, hi = LENGTHS[self.length]
        lines = [
            "Style rules:",
            f"- Length: {lo}-{hi} words in the body.",
            f"- Tone: {TONES[self.tone]}.",
            f"- Call to action: {CTAS[self.cta]}. Only one call to action.",
            "- Open with something specific about THEM, not about you. Mention 2-3 concrete, relevant "
            "proof points from the resume.",
            '- No buzzwords ("synergy", "leverage", "cutting-edge"), no fake urgency, no placeholders '
            "like [Name]; if the recipient's name is unknown use a neutral greeting.",
            "- Sign off with the sender name if provided.",
        ]
        if self.highlight:
            lines.append(f"- Make sure to mention (if supported by the resume): {self.highlight}.")
        if self.attach_resume:
            lines.append("- Mention briefly that the resume is attached.")
        if self.extra:
            lines.append(f"- Additional instructions from the candidate: {self.extra}")
        return "\n".join(lines)


@dataclass
class EmailDraft:
    to: str = ""
    subject: str = ""
    body: str = ""
    evidence: list[str] = field(default_factory=list)
    critique: str = ""


@dataclass
class AgentResult:
    draft: EmailDraft
    trace: list[dict]  # [{"type": "tool"|"info", "name": ..., "detail": ...}]
    found_emails: list[str]
    tokens: int = 0
    context: str = ""  # target description, reused when refining


# "[Company]", "{{name}}", "<Your Name>" left in a draft (a "[text](url)" link is not one).
PLACEHOLDER_RE = re.compile(r"\[[^\[\]\n]{2,40}\](?!\()|\{\{[^{}\n]{1,40}\}\}|<(?:your|recipient|company|hiring)[^<>\n]{0,30}>",
                            re.I)
NAME_PLACEHOLDER_RE = re.compile(r"\[(?:your\s+)?(?:full\s+)?name\]|<your\s+name>|\{\{\s*(?:sender|your)_?name\s*\}\}", re.I)


def find_placeholders(text: str) -> list[str]:
    return list(dict.fromkeys(PLACEHOLDER_RE.findall(text or "")))


def fill_sender_name(draft: "EmailDraft", sender: str) -> "EmailDraft":
    """Replace a "[Your Name]" sign-off with the configured sender name."""
    if sender:
        draft.body = NAME_PLACEHOLDER_RE.sub(sender, draft.body)
    return draft


def to_draft(data: dict) -> EmailDraft:
    return EmailDraft(
        to=str(data.get("to") or ""),
        subject=str(data.get("subject") or ""),
        body=str(data.get("body") or "").replace("\\n", "\n"),
        evidence=[str(e) for e in data.get("evidence") or []],
        critique=str(data.get("critique") or ""),
    )


def refine_email(llm: LLM, draft: EmailDraft, instruction: str, style: EmailStyle, sender: str = "") -> EmailDraft:
    """Rewrite a draft according to a free-text instruction ("make it shorter", ...)."""
    data = llm.json(REFINE_PROMPT.format(
        instruction=instruction, rules=style.rules(), evidence="\n".join(f"- {e}" for e in draft.evidence)
        or "(none)", subject=draft.subject, body=draft.body,
    ))
    return fill_sender_name(EmailDraft(
        to=draft.to,
        subject=str(data.get("subject") or draft.subject),
        body=str(data.get("body") or draft.body).replace("\\n", "\n"),
        evidence=draft.evidence,
        critique=str(data.get("critique") or ""),
    ), sender)


class ColdEmailAgent:
    def __init__(self, settings: Settings, kb: KnowledgeBase, on_step: Callable[[dict], None] | None = None):
        self.settings = settings
        self.kb = kb
        self.llm = LLM(settings)
        self.on_step = on_step or (lambda step: None)
        self.trace: list[dict] = []
        self.found_emails: list[str] = []
        self.page_context = ""
        self.seen_chunks: set[str] = set()

    def _log(self, type_: str, name: str, detail: str) -> None:
        step = {"type": type_, "name": name, "detail": detail}
        self.trace.append(step)
        self.on_step(step)

    def _chat(self, messages: list[dict], tools: bool = True):
        return self.llm.chat(messages, tools=TOOLS if tools else None)

    # ---- tools ----
    def _run_tool(self, name: str, args: dict) -> str:
        try:
            if name == "fetch_webpage":
                page = fetch_page(args["url"], max_chars=5000)
                self.found_emails += [e for e in page["emails"] if e not in self.found_emails]
                self.page_context = f"{page['title']}\n{page['text'][:3000]}"
                self._log("tool", name, f"{args['url']} — {page['title']} ({len(page['text'])} chars, "
                          f"emails: {', '.join(page['emails']) or 'none'})")
                return json.dumps(page)
            if name == "search_knowledge_base":
                # Every tool result stays in the conversation and is re-sent on each
                # step, so only return chunks the model hasn't seen yet.
                hits = [h for h in self.kb.search(args["query"]) if h.text not in self.seen_chunks]
                self.seen_chunks.update(h.text for h in hits)
                self._log("tool", name, f"“{args['query']}” → {len(hits)} new chunks from "
                          f"{', '.join(sorted({h.source for h in hits})) or 'nothing'}")
                if not hits:
                    return ("No new results: everything relevant from the resume was already returned above. "
                            "Stop searching and write the email now.")
                return json.dumps([{"source": h.source, "text": h.text} for h in hits])
            return f"Unknown tool {name}"
        except Exception as exc:  # report tool failures back to the model
            self._log("info", name, f"failed: {exc}")
            return f"Error: {exc}"

    # ---- main loop ----
    def run(self, url: str, notes: str, recipient_hint: str = "", review: bool = True,
            style: EmailStyle | None = None) -> AgentResult:
        style = style or EmailStyle()
        sender = self.settings.sender_name
        user_msg = "\n".join(filter(None, [
            f"Target URL: {url}" if url else "",
            f"Pasted target description:\n{notes}" if notes else "",
            f"Recipient: {recipient_hint}" if recipient_hint else "",
            f"Sender name: {sender}" if sender else "",
        ]))
        if notes:
            self.found_emails += find_emails(notes)
            self.page_context = notes[:3000]
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT + "\n\n" + style.rules()},
            {"role": "user", "content": user_msg},
        ]

        try:
            draft = self._tool_loop(messages)
        except BadRequestError as exc:
            # Some hosted models occasionally emit malformed tool calls; fall back to a fixed pipeline.
            self._log("info", "fallback", f"Tool calling failed ({exc.message[:120]}); using fixed pipeline.")
            draft = self._fixed_pipeline(messages, url)

        if review:
            draft = self._review(draft, style)
        fill_sender_name(draft, sender)
        # The model sometimes puts a name ("Hiring Team") in "to"; keep only a real address.
        draft.to = next(iter(find_emails(draft.to)), "")
        typed = find_emails(recipient_hint)
        if typed:  # an address the user typed always wins over one the model picked
            draft.to = typed[0]
        elif not draft.to and self.found_emails:
            draft.to = self.found_emails[0]
        return AgentResult(draft, self.trace, self.found_emails, self.llm.tokens, self.page_context)

    def _tool_loop(self, messages: list[dict]) -> EmailDraft:
        searched = False
        for _ in range(MAX_STEPS):
            msg = self._chat(messages)
            if msg.tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
                })
                for tc in msg.tool_calls:
                    args = json.loads(tc.function.arguments or "{}")
                    searched |= tc.function.name == "search_knowledge_base"
                    messages.append({"role": "tool", "tool_call_id": tc.id,
                                     "content": self._run_tool(tc.function.name, args)})
                continue
            if not searched:  # don't let it write without grounding
                messages.append({"role": "assistant", "content": msg.content or ""})
                messages.append({"role": "user", "content": "You have not searched the knowledge base "
                                 "yet. Call search_knowledge_base before writing."})
                searched = True  # nudge only once
                continue
            self._log("info", "draft", "Agent wrote the first draft.")
            try:
                return to_draft(parse_json(msg.content))
            except ValueError:
                messages.append({"role": "assistant", "content": msg.content or ""})
                messages.append({"role": "user", "content": "Return ONLY the JSON object now."})
        messages.append({"role": "user", "content": "Stop calling tools. Return ONLY the JSON object now."})
        return to_draft(parse_json(self._chat(messages, tools=False).content))

    def _fixed_pipeline(self, messages: list[dict], url: str) -> EmailDraft:
        context = []
        if url:
            context.append(self._run_tool("fetch_webpage", {"url": url}))
        query = self.page_context[:500] or messages[1]["content"]
        context.append(self._run_tool("search_knowledge_base", {"query": query}))
        messages = messages + [{"role": "user", "content": "Tool results:\n" + "\n\n".join(context)
                                + "\n\nNow return ONLY the JSON object."}]
        return to_draft(parse_json(self._chat(messages, tools=False).content))

    def _review(self, draft: EmailDraft, style: EmailStyle) -> EmailDraft:
        prompt = REVIEW_PROMPT.format(rules=style.rules(), context=self.page_context or "(none)",
                                      draft=json.dumps(draft.__dict__, indent=2))
        try:
            reviewed = to_draft(self.llm.json(prompt))
        except Exception as exc:
            self._log("info", "review", f"Review skipped: {exc}")
            return draft
        reviewed.to = reviewed.to or draft.to
        self._log("info", "review", reviewed.critique or "Reviewer polished the draft.")
        return reviewed if reviewed.body else draft
