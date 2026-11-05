"""Excel in and out: export job-search results, and read a sheet of openings for bulk sending.

The reader accepts this app's own export and any sheet that has an email column or a
job-link column; headers are matched loosely ("HR Email", "Contact email", "E-mail"...).
"""
import io
from dataclasses import asdict

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .jobsearch import Job
from .scraper import find_emails

NAVY = "1F3A7A"
# (header, Job attribute, column width, is a link)
COLUMNS = [
    ("Job title", "title", 34, False), ("Company", "company", 24, False), ("Location", "location", 22, False),
    ("Job type", "job_type", 13, False), ("Experience", "experience", 15, False), ("Workplace", "workplace", 11, False),
    ("Posted on", "posted_date", 12, False), ("Posted", "posted_ago", 14, False), ("Applicants", "applicants", 15, False),
    ("HR email", "email", 30, False), ("Email source", "email_source", 16, False), ("Other emails", "other_emails", 30, False),
    ("Recruiter", "recruiter", 22, False), ("Recruiter profile", "recruiter_url", 40, True),
    ("Job link", "job_url", 50, True), ("Apply link", "apply_url", 40, True), ("Company website", "company_website", 32, True),
    ("Company size", "company_size", 16, False), ("Industry", "industry", 28, False), ("Salary", "salary", 16, False),
    ("Description", "description", 60, False), ("Status", "", 14, False), ("Notes", "", 24, False),
]
DESCRIPTION_CHARS = 2000  # enough to personalise an email; keeps the sheet light

# What the reader looks for (lower-case header text).
ALIASES = {
    "email": ["hr email", "email", "e-mail", "email id", "contact email", "recruiter email", "hr mail", "mail", "email address"],
    "title": ["job title", "title", "role", "position", "designation", "job role"],
    "company": ["company", "company name", "organisation", "organization", "employer"],
    "job_url": ["job link", "job url", "link", "url", "job posting", "posting", "apply link", "apply url", "job"],
    "location": ["location", "city", "place", "country"],
    "recruiter": ["recruiter", "hr name", "contact name", "contact person", "hiring manager", "name"],
    "description": ["description", "job description", "jd", "details", "requirements"],
}


def write_jobs(jobs: list[Job], summary: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Jobs"
    header_font, header_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor=NAVY)
    for col, (header, _, width, _) in enumerate(COLUMNS, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font, cell.fill = header_font, header_fill
        cell.alignment = Alignment(vertical="center")
        ws.column_dimensions[get_column_letter(col)].width = width
    for r, job in enumerate(jobs, 2):
        data = asdict(job)
        for col, (_, attr, _, is_link) in enumerate(COLUMNS, 1):
            value = data.get(attr, "") if attr else ""
            if attr == "description":
                value = value[:DESCRIPTION_CHARS]
            cell = ws.cell(row=r, column=col, value=value or None)
            if is_link and value:
                cell.hyperlink, cell.style = value, "Hyperlink"
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{max(len(jobs), 1) + 1}"
    ws.row_dimensions[1].height = 22

    about = wb.create_sheet("Search")
    about.column_dimensions["A"].width, about.column_dimensions["B"].width = 22, 70
    about.append(["MailMind job search", ""])
    about["A1"].font = Font(bold=True, size=13, color=NAVY)
    for key, value in summary.items():
        about.append([key, str(value)])
    about.append(["Jobs", len(jobs)])
    about.append(["With an HR email", sum(bool(j.email) for j in jobs)])
    about.append([])
    about.append(["Note", "Emails come from the job description or the company's website. Check them before sending; "
                          "use the Status and Notes columns to track your applications."])
    for row in about.iter_rows(min_row=2, max_col=1):
        row[0].font = Font(bold=True)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _match_columns(headers: list[str]) -> dict[str, int]:
    """Map each field to the first header that matches one of its aliases (exact match first)."""
    norm = [str(h or "").strip().lower() for h in headers]
    found: dict[str, int] = {}
    for key, names in ALIASES.items():
        for name in names:  # aliases are in priority order
            if name in norm and norm.index(name) not in found.values():
                found[key] = norm.index(name)
                break
        if key not in found:  # e.g. "HR Email ID" or "Company Name (Legal)"
            for i, h in enumerate(norm):
                if i not in found.values() and any(name in h for name in names[:2]):
                    found[key] = i
                    break
    return found


def read_openings(data: bytes) -> tuple[list[dict], list[str]]:
    """Read openings from an .xlsx. Returns (rows, problems). Each row has: email, other_emails,
    title, company, job_url, location, recruiter, description, row (the sheet row number)."""
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        return [], [f"That file couldn't be opened as an Excel workbook ({type(exc).__name__}). Save it as .xlsx and try again."]
    ws = wb["Jobs"] if "Jobs" in wb.sheetnames else wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    # The header is the first of the top five rows with at least two filled cells.
    header_at = next((i for i, r in enumerate(rows[:5]) if sum(1 for c in r if c not in (None, "")) >= 2), None)
    if header_at is None:
        return [], ["No header row found. The first row should name the columns, e.g. HR email, Job title, Company, Job link."]
    cols = _match_columns(list(rows[header_at]))
    if "email" not in cols and "job_url" not in cols:
        return [], ["No email column or job-link column found. Name one column \"HR email\" (or \"Email\") "
                    "or \"Job link\"."]
    out = []
    for n, r in enumerate(rows[header_at + 1:], header_at + 2):
        get = lambda key: str(r[cols[key]]).strip() if key in cols and cols[key] < len(r) and r[cols[key]] is not None else ""
        if not any(get(k) for k in cols):
            continue
        emails = find_emails(get("email"))
        out.append({
            "row": n, "email": emails[0] if emails else "", "raw_email": get("email"),
            "other_emails": ", ".join(emails[1:]), "title": get("title"), "company": get("company"),
            "job_url": get("job_url"), "location": get("location"), "recruiter": get("recruiter"),
            "description": get("description"),
        })
    problems = [] if out else ["The sheet has a header but no rows of data."]
    return out, problems


def write_results(items: list[dict]) -> bytes:
    """A simple results sheet after a bulk send: who was emailed, with what, and how it went."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Bulk send"
    headers = ["Status", "To", "Job title", "Company", "Job link", "Subject", "Sent at", "Detail"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font, cell.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor=NAVY)
    for it in items:
        ws.append([it.get(k, "") for k in ("status", "to", "title", "company", "job_url", "subject", "sent_at", "detail")])
    for col, width in zip("ABCDEFGH", (14, 32, 32, 24, 46, 46, 20, 40)):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
