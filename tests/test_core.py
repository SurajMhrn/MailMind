"""Unit tests for the agent package: validation, placeholders, errors, retrieval, scouting, mail, follow-ups."""
import json
import smtplib
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS

import openai
import pytest
import requests
from openai.types.chat import ChatCompletionMessageToolCall

from agent import agent as agent_mod, rag
from agent.agent import ColdEmailAgent, EmailDraft, EmailStyle, fill_sender_name, find_placeholders, refine_email
from agent.errors import friendly_error
from agent.followup import SentEmail, reply_subject
from agent.llm import LLM, parse_json, strip_think
from agent.mailer import send_email
from agent.scout import board_root, boilerplate_lines, career_score, email_kind, job_links, rank_emails, same_site
from agent.scraper import find_emails, is_valid_url, normalize_url
from conftest import FakeLLMClient, json_reply, reply


# ---------------- URL and email validation ----------------
@pytest.mark.parametrize("url, ok", [
    ("https://company.com/careers", True), ("company.com", True), ("www.sigmoid.com", True),
    ("http://x.io/a?b=1", True), ("company careers page", False), ("https://company", False), ("", False),
])
def test_is_valid_url(url, ok):
    assert is_valid_url(url) is ok


def test_normalize_url_adds_scheme():
    assert normalize_url("  sigmoid.com ") == "https://sigmoid.com"
    assert normalize_url("http://a.com") == "http://a.com"


def test_find_emails_drops_junk_and_duplicates():
    text = "Mail careers@acme.io or CAREERS@acme.io, logo@2x.png, test@example.com, sentry@sentry.io."
    assert find_emails(text) == ["careers@acme.io"]


# ---------------- placeholders ----------------
def test_find_placeholders_ignores_markdown_links():
    body = "Hi [Hiring Manager],\nSee [my portfolio](https://x.io). Thanks, {{sender_name}} <Your Name>"
    assert find_placeholders(body) == ["[Hiring Manager]", "{{sender_name}}", "<Your Name>"]


def test_fill_sender_name_replaces_signoff():
    draft = fill_sender_name(EmailDraft(body="Best regards,\n[Your Name]"), "Suraj Maharana")
    assert draft.body.endswith("Suraj Maharana") and not find_placeholders(draft.body)
    assert fill_sender_name(EmailDraft(body="[Your Name]"), "").body == "[Your Name]"  # no sender → left for the user


# ---------------- LLM helpers ----------------
def test_parse_json_strips_think_and_raw_newlines():
    assert parse_json('<think>plan</think>{"a": "x\ny"}') == {"a": "x\ny"}
    assert strip_think("<think>unterminated") == ""
    with pytest.raises(ValueError):
        parse_json("no json here")


# ---------------- friendly errors ----------------
def _openai_error(cls, status):
    # The SDK only reads these attributes from the HTTP response.
    response = NS(request=NS(method="POST", url="https://api.groq.com/openai/v1/chat/completions"),
                  status_code=status, headers={})
    return cls("boom", response=response, body=None)


def _http_error(status, url):
    resp = requests.Response()
    resp.status_code, resp.url = status, url
    return requests.HTTPError(response=resp)


@pytest.mark.parametrize("exc, expected", [
    (_openai_error(openai.AuthenticationError, 401), "GROQ_API_KEY"),
    (_openai_error(openai.RateLimitError, 429), "free-tier limit"),
    (_openai_error(openai.NotFoundError, 404), "LLM_MODEL"),
    (_http_error(402, "https://api.apify.com/v2/acts/x/runs"), "credit"),
    (_http_error(401, "https://api.apify.com/v2/acts/x/runs"), "APIFY_API_TOKEN"),
    (_http_error(403, "https://www.linkedin.com/jobs/1"), "Paste the job description"),
    (_http_error(404, "https://a.com/x"), "404"),
    (requests.ConnectionError(), "Couldn't connect"),
    (smtplib.SMTPRecipientsRefused({}), "refused the recipient"),
    (ValueError("Model did not return JSON"), "unexpected format"),
    (RuntimeError("SMTP_USER and SMTP_PASSWORD must be set"), "SMTP_USER"),
])
def test_friendly_error(exc, expected):
    assert expected in friendly_error(exc)


# ---------------- RAG ----------------
@pytest.fixture
def kb(tmp_path, monkeypatch, settings):
    monkeypatch.setattr(rag, "KNOWLEDGE_DIR", tmp_path)
    kb = rag.KnowledgeBase("resume", settings)
    kb.add_file("resume.md", b"# Skills\n\nPython, SQL, Power BI\n\n# Projects\n\nForecasting model in Pandas, 18% better.")
    return kb


def test_chunking_splits_on_headings_and_bm25_ranks(kb):
    assert len(kb.chunks) == 2
    assert kb.search("forecasting pandas")[0].text.startswith("# Projects")


def test_add_file_reports_empty_and_corrupt_documents(kb):
    assert kb.add_file("empty.txt", b"   ") == 0
    kb.add_file("broken.pdf", b"not really a pdf")
    assert "broken.pdf" in kb.errors and len(kb.chunks) == 2  # the good file is still indexed
    kb.remove_file("broken.pdf")
    assert not kb.errors


# ---------------- agent loop ----------------
def _tool_call(name, args, id_="1"):
    return ChatCompletionMessageToolCall(id=id_, type="function", function={"name": name, "arguments": json.dumps(args)})


def _agent(settings, kb, replies):
    agent = ColdEmailAgent(settings, kb)
    agent.llm.client = FakeLLMClient(replies)
    return agent


def test_agent_searches_then_drafts_and_fills_signoff(settings, kb):
    agent = _agent(settings, kb, [
        reply(tool_calls=[_tool_call("search_knowledge_base", {"query": "python"})]),
        json_reply(to="Hiring Team", subject="Data Analyst", body="Hi,\n\n[Your Name]", evidence=["Python"]),
    ])
    result = agent.run("", "Acme needs a data analyst. careers@acme.io", review=False)
    assert result.draft.to == "careers@acme.io"  # a name in "to" is replaced by the address found
    assert result.draft.body.endswith("Suraj Maharana")
    assert any(s["name"] == "search_knowledge_base" for s in result.trace)


def test_agent_forces_a_search_before_writing(settings, kb):
    agent = _agent(settings, kb, [
        json_reply(to="", subject="s", body="b", evidence=[]),  # tries to write without grounding
        reply(tool_calls=[_tool_call("search_knowledge_base", {"query": "sql"})]),
        json_reply(to="", subject="s", body="b", evidence=["SQL"]),
    ])
    agent.run("", "A long enough job description for testing.", review=False)
    nudges = [m for m in agent.llm.client.calls[1]["messages"] if "not searched" in str(m.get("content"))]
    assert nudges


def test_typed_recipient_wins(settings, kb):
    agent = _agent(settings, kb, [
        reply(tool_calls=[_tool_call("search_knowledge_base", {"query": "x"})]),
        json_reply(to="hr@acme.io", subject="s", body="b", evidence=[]),
    ])
    assert agent.run("", "desc", "Priya <priya@acme.io>", review=False).draft.to == "priya@acme.io"


def test_empty_search_tells_the_model_to_stop(settings, kb):
    agent = ColdEmailAgent(settings, kb)
    agent._run_tool("search_knowledge_base", {"query": "python"})
    assert "Stop searching" in agent._run_tool("search_knowledge_base", {"query": "python"})


def test_refine_keeps_recipient_and_evidence(settings):
    llm = LLM(settings)
    llm.client = FakeLLMClient([json_reply(subject="New", body="Shorter.\n[Your Name]", critique="Cut 30%")])
    draft = EmailDraft(to="a@b.io", subject="Old", body="Long body", evidence=["Python"])
    new = refine_email(llm, draft, "shorter", EmailStyle(), "Suraj")
    assert (new.to, new.subject, new.evidence, new.body) == ("a@b.io", "New", ["Python"], "Shorter.\nSuraj")


def test_style_rules_reflect_options():
    rules = EmailStyle(tone="Formal", length="Short", highlight="Power BI", attach_resume=True).rules()
    assert "60-100 words" in rules and "formal" in rules and "Power BI" in rules and "attached" in rules


# ---------------- scout helpers ----------------
def test_board_root_and_site_matching():
    assert board_root("https://job-boards.greenhouse.io/acme/jobs/123") == "https://job-boards.greenhouse.io/acme"
    assert board_root("https://acme.com/careers/x") == "https://acme.com/careers/x"
    assert same_site("https://careers.acme.com/jobs", "www.acme.com")
    assert not same_site("https://acme.com.evil.io", "acme.com")


def test_rank_emails_prefers_hiring_and_own_domain():
    ranked = rank_emails(["noreply@acme.com", "hello@acme.com", "careers@gmail.com", "careers@acme.com", "x@acme.com"],
                         "www.acme.com")
    assert ranked[:2] == ["careers@acme.com", "careers@gmail.com"] and "noreply@acme.com" not in ranked
    assert email_kind("hr@acme.com") == "hiring contact" and email_kind("info@acme.com") == "general inbox"


def test_career_scoring_prefers_listing_pages():
    listing = {"url": "https://acme.com/careers", "title": "Careers",
               "markdown": "\n".join(f"[Data Analyst {i}](https://acme.com/careers/da-{i})" for i in range(5))}
    single = {"url": "https://acme.com/careers/da-1", "title": "Data Analyst", "markdown": "Apply now. Requirements: SQL"}
    blog = {"url": "https://acme.com/blog/post", "title": "Our blog", "markdown": "Hello"}
    assert career_score(listing) > career_score(single) > career_score(blog)
    assert len(job_links(listing)) == 5


def test_boilerplate_lines_found_on_most_pages():
    pages = [{"markdown": f"Menu\nFooter\nUnique {i}"} for i in range(4)]
    assert boilerplate_lines(pages) == {"Menu", "Footer"}


# ---------------- mail ----------------
class FakeSMTP:
    sent = []

    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def starttls(self): pass
    def login(self, user, password): pass
    def send_message(self, msg): FakeSMTP.sent.append(msg)


def test_send_email_threads_and_attaches(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    message_id = send_email(settings, "hr@acme.io", "Re: Hi", "Body", in_reply_to="<b@x>", references="<a@x>",
                            attachments=[pdf])
    msg = FakeSMTP.sent[-1]
    assert msg["Message-ID"] == message_id and msg["In-Reply-To"] == "<b@x>" and msg["References"] == "<a@x> <b@x>"
    assert msg["From"] == "Suraj Maharana <me@gmail.com>"
    assert [p.get_filename() for p in msg.iter_attachments()] == ["resume.pdf"]


def test_send_email_rejects_names_and_missing_config(settings):
    with pytest.raises(ValueError, match="not a valid email"):
        send_email(settings, "Digitals AI Hiring Team", "s", "b")
    settings.smtp_password = ""
    with pytest.raises(RuntimeError, match="SMTP_USER"):
        send_email(settings, "hr@acme.io", "s", "b")


# ---------------- follow-ups ----------------
def test_followup_schedule_and_threading(sent_log):
    sent_at = (datetime.now() - timedelta(days=5)).isoformat(timespec="seconds")
    e = SentEmail(to="hr@acme.io", subject="Hi", body="b", message_id="<1@x>", sent_at=sent_at)
    sent_log.add(e)
    assert e.followup_due_in([4, 7]) == -1  # due a day ago
    e.followups.append({"body": "f", "message_id": "<2@x>", "sent_at": datetime.now().isoformat(timespec="seconds")})
    assert e.followup_due_in([4, 7]) == 7 and e.last_message_id == "<2@x>" and e.references == "<1@x>"
    e.followups.append({"body": "f2", "message_id": "<3@x>", "sent_at": datetime.now().isoformat()})
    assert e.followup_due_in([4, 7]) is None  # schedule used up
    e.status = "replied"
    sent_log.update(e)
    assert sent_log.all()[0].status == "replied"
    sent_log.delete(e.id)
    assert sent_log.all() == []


def test_reply_subject():
    assert reply_subject("Hi") == "Re: Hi" and reply_subject("RE: Hi") == "RE: Hi"


def test_sent_log_isolated_from_real_data(sent_log):
    assert sent_log.path.name == "sent.json" and "data" not in sent_log.path.parts[-2:-1]
    assert Path(sent_log.path).parent != Path(agent_mod.__file__).parent.parent / "data"
