"""UI tests: every page renders, buttons respond, and validation/success messages appear.

AppTest cannot switch between function pages, so each test runs app.py through a tiny
wrapper whose st.navigation returns the page under test.
"""
from datetime import datetime, timedelta

import pytest
from streamlit.testing.v1 import AppTest

from agent.agent import AgentResult, EmailDraft
from agent.followup import SentEmail
from conftest import ROOT

WRAPPER = """
import sys
import streamlit as st
sys.path.insert(0, {root!r})
_real = st.navigation
def _pick(pages, **kw):
    _real(pages, **kw)
    page = next(p for p in pages if p._url_path == {page!r})
    page._can_be_called = True  # what st.navigation does for the page it selects
    return page
st.navigation = _pick
try:
    exec(compile(open({app!r}, encoding="utf-8").read(), {app!r}, "exec"), {{"__name__": "__main__", "__file__": {app!r}}})
finally:
    st.navigation = _real
"""


def open_page(page: str) -> AppTest:
    return AppTest.from_string(WRAPPER.format(root=str(ROOT), app=str(ROOT / "app.py"), page=page), default_timeout=60)


def errors(at: AppTest) -> str:
    return " | ".join(e.value for e in at.error)


def button(at: AppTest, label: str):
    return next(b for b in at.button if b.label == label)


@pytest.mark.parametrize("page, title", [("write", "Write a cold email"), ("find-openings", "Find openings on a company website"),
                                         ("job-search", "Search jobs worldwide"), ("bulk-send", "Email many openings"),
                                         ("sent", "Sent emails & follow-ups"), ("not-found", "Page not found")])
def test_every_page_renders(page, title, sent_log):
    at = open_page(page).run()
    assert not at.exception, at.exception
    assert [t.value for t in at.title] == [title]
    footer_links = {p.label for p in at.get("page_link")}
    assert {"Write email", "Find openings", "Job search", "Bulk send", "Sent & follow-ups", "Get a Groq API key"} <= footer_links


def test_generate_validates_input_on_first_click(sent_log):
    at = open_page("write").run()
    button(at, "Generate email").click().run()
    assert "Add a job post URL" in errors(at)
    at.text_input(key="target_url").set_value("company careers page")
    button(at, "Generate email").click().run()
    assert "isn't a valid web address" in errors(at)
    at.text_input(key="target_url").set_value("")
    at.text_area(key="target_notes").set_value("short")
    button(at, "Generate email").click().run()
    assert "too short" in errors(at)


def _with_draft(at: AppTest, body="Hi team,\n\nThanks,\nSuraj Maharana", to="hr@acme.io") -> AppTest:
    at.session_state["result"] = AgentResult(EmailDraft(to=to, subject="Data Analyst", body=body, evidence=["SQL"]),
                                             [{"type": "tool", "name": "search_knowledge_base", "detail": "x"}], [], 1234)
    at.session_state["draft_to"], at.session_state["draft_subject"], at.session_state["draft_body"] = to, "Data Analyst", body
    return at.run()


def test_send_explains_what_is_missing(sent_log):
    at = _with_draft(open_page("write"), body="Hi [Hiring Manager],\n\nThanks", to="Hiring Team")
    assert any("placeholders" in w.value for w in at.warning)
    button(at, "Send email").click().run()
    msg = errors(at)
    assert "valid recipient" in msg and "placeholders" in msg and "I've reviewed" in msg
    assert sent_log.all() == []


def test_send_success_logs_and_confirms(sent_log, monkeypatch):
    import agent.mailer as mailer
    monkeypatch.setattr(mailer.smtplib, "SMTP", __import__("test_core").FakeSMTP)
    at = _with_draft(open_page("write"))
    at.checkbox(key="send_confirm").check().run()
    button(at, "Send email").click().run()
    assert not errors(at)
    assert [e.to for e in sent_log.all()] == ["hr@acme.io"]
    assert any("Email sent to hr@acme.io" in t.value for t in at.toast)
    assert any("Sent to hr@acme.io" in s.value for s in at.success)
    button(at, "Send email").click().run()  # a second click must not send the same email again
    assert "already sent" in errors(at) and len(sent_log.all()) == 1


def test_refine_requires_an_instruction(sent_log):
    at = _with_draft(open_page("write"))
    button(at, "Apply edit").click().run()
    assert "Pick a quick edit" in errors(at)


def test_scout_validates_website(sent_log):
    at = open_page("find-openings").run()
    button(at, "Find openings").click().run()
    assert "Enter the company's website" in errors(at)
    at.text_input(key="scout_site").set_value("not a site")
    button(at, "Find openings").click().run()
    assert "isn't a valid web address" in errors(at)


def test_sent_page_actions_show_success_messages(sent_log):
    old = (datetime.now() - timedelta(days=5)).isoformat(timespec="seconds")
    sent_log.add(SentEmail(to="hr@acme.io", subject="Hi", body="b", message_id="<1@x>", sent_at=old, id="t1"))
    at = open_page("sent").run()
    assert [m.value for m in at.metric][:2] == ["1", "1"]
    assert any("Follow-up due" in m.value for m in at.markdown)
    button(at, "Mark replied").click().run()
    assert sent_log.all()[0].status == "replied"
    assert any("Marked hr@acme.io as replied" in t.value for t in at.toast)
    button(at, "Yes, delete").click().run()  # inside the Delete confirmation popover
    assert sent_log.all() == [] and any("Deleted" in t.value for t in at.toast)


def test_inbox_check_without_threads_says_so(sent_log):
    at = open_page("sent").run()
    button(at, "Check inbox for replies & bounces").click().run()
    assert any("No threads are awaiting" in i.value for i in at.info)


# ---------------- job search and bulk send ----------------
def test_job_search_validates_and_shows_results(sent_log):
    from agent.jobsearch import normalize
    from test_jobsearch_bulk import ITEM
    at = open_page("job-search").run()
    button(at, "Search jobs").click().run()
    assert "Enter at least one job title" in errors(at)
    at.session_state["jobs"] = [normalize(ITEM), normalize(dict(ITEM, id="2", title="BI Intern", description=""))]
    at.session_state["jobs_summary"] = {"Keywords": "Data Analyst"}
    at.run()
    assert not at.exception, at.exception
    assert [m.value for m in at.metric][:2] == ["2", "1"]  # jobs, with HR email
    assert any(getattr(b, "label", "") == "Download Excel" for b in at.get("download_button"))
    at.toggle(key="js_only_email").set_value(True).run()
    assert not at.exception


def test_bulk_send_drafts_reviews_and_sends(sent_log, monkeypatch):
    import agent.bulk as bulk_mod
    import agent.mailer as mailer
    from test_core import FakeSMTP

    def fake_draft(llm, kb, op, style, sender):
        op.subject, op.body, op.evidence = f"{op.title} application", f"Hi {op.company} team,\n\nThanks,\n{sender}", ["SQL"]

    monkeypatch.setattr(bulk_mod, "draft", fake_draft)
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr("time.sleep", lambda s: None)
    at = open_page("bulk-send")
    at.session_state["bulk_rows"] = [
        {"row": 2, "email": "hr@acme.example.org", "title": "SDE Intern", "company": "Acme", "job_url": "", "description": ""},
        {"row": 3, "email": "jobs@beta.example.org", "title": "Analyst", "company": "Beta", "job_url": "", "description": ""},
        {"row": 4, "email": "", "raw_email": "n/a", "title": "X", "company": "Gamma", "job_url": "", "description": ""}]
    at.session_state["bulk_source"] = "test.xlsx"
    at.run()
    assert not at.exception, at.exception
    assert [m.value for m in at.metric][:4] == ["3", "2", "0", "1"]
    button(at, "Draft emails").click().run()
    assert not at.exception, at.exception
    assert [t.value for t in at.text_input if t.key == "bulkd_to_0"] == ["hr@acme.example.org"]
    button(at, "Send 2 emails").click().run()
    assert "Tick the review box" in errors(at) and sent_log.all() == []
    at.checkbox(key="bulkd_inc_1").uncheck().run()  # send only the first draft
    at.checkbox(key="bulk_confirm").check().run()
    button(at, "Send 1 emails").click().run()
    assert not at.exception, at.exception
    assert [e.to for e in sent_log.all()] == ["hr@acme.example.org"]
    assert any("Sent 1 emails" in t.value for t in at.toast)
    assert any("Sent" in m.value for m in at.markdown)  # the sent draft is marked


def test_password_gate(sent_log, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "s3cret")
    at = open_page("write").run()
    assert [t.value for t in at.title] == ["Sign in"]
    at.text_input[0].set_value("wrong")
    at.button[0].click().run()
    assert "Wrong password" in errors(at)
    at.text_input[0].set_value("s3cret")
    at.button[0].click().run()
    assert [t.value for t in at.title] == ["Write a cold email"]
