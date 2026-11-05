"""Tests for worldwide job search, the Excel export/import and bulk sending."""
import io
from datetime import datetime

import pytest
from openpyxl import Workbook, load_workbook

from agent import bulk, jobsearch, rag
from agent.agent import EmailStyle
from agent.bulk import ALREADY, DUPLICATE, INVALID, LOOKUP, NO_EMAIL, READY, Opening
from agent.excel import read_openings, write_jobs, write_results
from agent.followup import SentEmail
from agent.jobsearch import Job, JobSearch, clean_url, normalize, parse_list
from agent.llm import LLM
from conftest import FakeLLMClient, json_reply

# Shaped like a real bebity/linkedin-jobs-scraper item.
ITEM = {
    "id": "4486588677", "publishedAt": "2026-11-05", "title": "DATA ANALYST INTERN",
    "jobUrl": "https://in.linkedin.com/jobs/view/data-analyst-intern-at-vortenza-4486588677?trk=public_jobs_topcard-title",
    "companyName": "Vortenza Systems", "location": "India", "postedTime": "5 hours ago", "applicationsCount": "59 applicants",
    "contractType": "Internship", "experienceLevel": "Internship", "workType": "Remote",
    "posterProfileUrl": "https://in.linkedin.com/in/sanket-sspatil?trk=x", "posterFullName": "Sanket Patil",
    "companyWebsite": "https://vortenza.example.org/", "companySize": "2-10 employees",
    "companyIndustries": "Technology, Information and Internet",
    "description": "Build Power BI dashboards. Send your CV to careers@vortenza.example.org or noreply@vortenza.example.org.",
}


# ---------------- job search ----------------
def test_parse_list_and_clean_url():
    assert parse_list(" Data Analyst, Python developer;\nData Analyst ") == ["Data Analyst", "Python developer"]
    assert clean_url(ITEM["jobUrl"]) == "https://in.linkedin.com/jobs/view/data-analyst-intern-at-vortenza-4486588677"
    assert clean_url("https://company.example.org/jobs?id=3") == "https://company.example.org/jobs?id=3"


def test_search_maps_filters_to_actor_input():
    s = JobSearch(["Data Analyst", "ML Engineer"], ["India", "Germany"], posted="Past 24 hours",
                  job_types=["Internship", "Full-time"], workplaces=["Remote"], experience=["Entry level"], per_search=20)
    inp = s.run_input()
    assert inp["titles"] == ["Data Analyst", "ML Engineer"] and inp["locations"] == ["India", "Germany"]
    assert (inp["publishedAt"], inp["contractTypes"], inp["workTypes"], inp["experienceLevels"]) == ("r86400", ["I", "F"], ["2"], ["2"])
    assert (s.searches, s.max_jobs, round(s.estimated_cost_usd, 2)) == (4, 80, 0.12)
    assert JobSearch([], [], per_search=10).validate() == ["Enter at least one job title or keyword.",
                                                           "Choose at least one location (or type your own)."]
    assert "keep it to" in JobSearch([str(i) for i in range(5)], ["a", "b", "c"]).validate()[0]


def test_normalize_takes_email_from_description():
    job = normalize(ITEM)
    assert (job.title, job.company, job.recruiter, job.job_type, job.workplace) == (
        "DATA ANALYST INTERN", "Vortenza Systems", "Sanket Patil", "Internship", "Remote")
    assert job.email == "careers@vortenza.example.org" and job.email_source == "job description"
    assert "noreply" not in job.other_emails and "?trk" not in job.recruiter_url


def test_website_lookup_fills_missing_emails(monkeypatch):
    monkeypatch.setattr(jobsearch, "company_emails", lambda site: ["hr@acme.example.org", "info@acme.example.org"])
    jobs = [Job("1", "A", "Acme", company_website="https://acme.example.org"),
            Job("2", "B", "Acme", company_website="https://acme.example.org"),
            Job("3", "C", "Solo"), Job("4", "D", "Done", email="jobs@done.example.org", company_website="https://done.example.org")]
    assert jobsearch.fill_from_websites(jobs) == 2
    assert [j.email for j in jobs] == ["hr@acme.example.org", "hr@acme.example.org", "", "jobs@done.example.org"]
    assert jobs[0].email_source == "company website"


def test_search_jobs_dedupes_sorts_and_caps_cost(monkeypatch, settings):
    calls = {}

    def fake_run(token, actor, run_input, **kw):
        calls.update(kw, actor=actor)
        older = dict(ITEM, id="1", publishedAt="2026-11-01", title="Older")
        return [dict(ITEM), dict(ITEM), older, dict(ITEM, id="9", title="")]  # duplicate and an empty title

    monkeypatch.setattr(jobsearch.apify, "run_actor", fake_run)
    jobs = jobsearch.search_jobs(settings, JobSearch(["Data Analyst"], ["India"], per_search=10, find_emails=False))
    assert [j.title for j in jobs] == ["DATA ANALYST INTERN", "Older"]
    assert calls["actor"] == jobsearch.ACTOR and calls["max_charge_usd"] == pytest.approx(10 * 0.0015 * 1.5 + 0.05)


# ---------------- Excel ----------------
def test_export_then_import_round_trip():
    data = write_jobs([normalize(ITEM), Job("2", "Analyst", "NoMail Ltd", job_url="https://x.example.org/job/2")],
                      {"Keywords": "Data Analyst"})
    wb = load_workbook(io.BytesIO(data))
    assert wb.sheetnames == ["Jobs", "Search"]
    assert wb["Jobs"]["O2"].hyperlink.target.startswith("https://in.linkedin.com/jobs/view/")
    rows, problems = read_openings(data)
    assert not problems and len(rows) == 2
    assert rows[0]["email"] == "careers@vortenza.example.org" and rows[0]["title"] == "DATA ANALYST INTERN"
    assert rows[1]["email"] == "" and rows[1]["job_url"] == "https://x.example.org/job/2"


def _sheet(rows) -> bytes:
    wb = Workbook()
    for r in rows:
        wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_reads_hand_made_sheets_with_other_headers():
    data = _sheet([["My applications", None, None], ["Role", "Organisation", "HR Email ID", "Link"],
                   ["SDE Intern", "Acme", "Priya <PRIYA@acme.example.org>; hr@acme.example.org", "https://acme.example.org/j/1"],
                   [None, None, None, None], ["Analyst", "Beta", "not an email", ""]])
    rows, problems = read_openings(data)
    assert not problems
    assert rows[0] == {"row": 3, "email": "priya@acme.example.org", "raw_email": "Priya <PRIYA@acme.example.org>; hr@acme.example.org",
                       "other_emails": "hr@acme.example.org", "title": "SDE Intern", "company": "Acme",
                       "job_url": "https://acme.example.org/j/1", "location": "", "recruiter": "", "description": ""}
    assert rows[1]["email"] == "" and rows[1]["raw_email"] == "not an email"


@pytest.mark.parametrize("data, message", [
    (b"not an excel file", "couldn't be opened"),
    (_sheet([["Name", "Phone"], ["A", "1"]]), "No email column or job-link column"),
    (_sheet([["Email", "Company"]]), "no rows of data"),
])
def test_read_openings_explains_problems(data, message):
    rows, problems = read_openings(data)
    assert rows == [] and message in problems[0]


def test_write_results_sheet():
    data = write_results([{"status": "sent", "to": "a@b.example.org", "title": "T", "sent_at": "2026-11-05T10:00:00"}])
    ws = load_workbook(io.BytesIO(data)).active
    assert [c.value for c in ws[1]][:2] == ["Status", "To"] and ws["A2"].value == "sent"


# ---------------- bulk send ----------------
def test_plan_marks_rows(sent_log):
    sent_log.add(SentEmail(to="old@acme.example.org", subject="s", body="b", message_id="<1@x>"))
    rows = [{"row": 2, "email": "hr@acme.example.org", "title": "SDE"},
            {"row": 3, "email": "HR@acme.example.org"},
            {"row": 4, "email": "old@acme.example.org"},
            {"row": 5, "email": "", "raw_email": "n/a"},
            {"row": 6, "email": "", "job_url": "https://acme.example.org/j/9"},
            {"row": 7, "email": ""}]
    assert [o.status for o in bulk.plan(rows, sent_log)] == [READY, DUPLICATE, ALREADY, INVALID, LOOKUP, NO_EMAIL]


def test_find_email_on_job_page(monkeypatch):
    monkeypatch.setattr(bulk, "fetch_page", lambda url, max_chars: {"text": "Apply: talent@acme.example.org", "emails": []})
    op = Opening(row=2, to="", title="SDE", company="Acme", job_url="https://acme.example.org/j/1", status=LOOKUP)
    assert bulk.find_email_on_page(op) and (op.to, op.status) == ("talent@acme.example.org", READY)


@pytest.fixture
def kb(tmp_path, monkeypatch, settings):
    monkeypatch.setattr(rag, "KNOWLEDGE_DIR", tmp_path)
    kb = rag.KnowledgeBase("resume", settings)
    kb.add_file("resume.md", b"# Skills\n\nPython, SQL, Power BI dashboards that cut reporting time by 40%.")
    return kb


def test_draft_is_grounded_and_signed(settings, kb):
    llm = LLM(settings)
    llm.client = FakeLLMClient([json_reply(subject="SDE Intern at Acme", body="Hi Acme team,\n\n...\n\n[Your Name]",
                                           evidence=["Power BI dashboards"])])
    op = Opening(row=2, to="hr@acme.example.org", title="SDE Intern", company="Acme", description="Python and SQL")
    bulk.draft(llm, kb, op, EmailStyle(), "Suraj Maharana")
    prompt = llm.client.calls[0]["messages"][0]["content"]
    assert "Power BI" in prompt and "Acme team" in prompt and "SDE Intern" in prompt
    assert op.body.endswith("Suraj Maharana") and op.evidence == ["Power BI dashboards"] and not bulk.problems(op)


def test_problems_and_daily_count(sent_log):
    op = Opening(row=2, to="not-an-email", title="T", company="C", subject="Hi", body="Dear [Hiring Manager]")
    assert bulk.problems(op) == ["no valid recipient address", "placeholders left: [Hiring Manager]"]
    now = datetime.now().isoformat(timespec="seconds")
    e = SentEmail(to="a@x.example.org", subject="s", body="b", message_id="<1@x>", sent_at=now)
    e.followups.append({"body": "f", "message_id": "<2@x>", "sent_at": now})
    sent_log.add(e)
    sent_log.add(SentEmail(to="b@x.example.org", subject="s", body="b", message_id="<3@x>", sent_at="2020-01-01T00:00:00"))
    assert bulk.sent_today(sent_log) == 2
