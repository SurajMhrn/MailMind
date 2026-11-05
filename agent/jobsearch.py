"""Worldwide job search: LinkedIn job listings via Apify, filtered by date, place and job type.

Uses the LinkedIn Jobs Scraper Actor (bebity/linkedin-jobs-scraper): every keyword is
searched in every location, and each result carries the job link, the recruiter who
posted it and the company's website. HR emails are rarely in a listing, so they are
collected from the job description and, optionally, from the company's own website
(the same free email finder as Find openings).
"""
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from typing import Callable
from urllib.parse import urlparse, urlunparse

from . import apify
from .config import Settings
from .scout import CAREER_RE, CONTACT_RE, rank_emails, same_site
from .scraper import fetch_links, find_emails

ACTOR = "bebity~linkedin-jobs-scraper"
COST_PER_JOB_USD = 0.0015  # Actor's free-tier price per job result

POSTED = {"Any time": "", "Past 24 hours": "r86400", "Past week": "r604800", "Past month": "r2592000"}
JOB_TYPES = {"Full-time": "F", "Part-time": "P", "Contract": "C", "Temporary": "T", "Internship": "I", "Volunteer": "V"}
WORKPLACES = {"On-site": "1", "Remote": "2", "Hybrid": "3"}
EXPERIENCE = {"Internship": "1", "Entry level": "2", "Associate": "3", "Mid-Senior level": "4", "Director": "5"}
LOCATION_PRESETS = [
    "India", "Bengaluru", "Hyderabad", "Pune", "Mumbai", "Delhi NCR", "Chennai", "Kolkata", "Bhubaneswar",
    "Worldwide", "United States", "United Kingdom", "Canada", "Germany", "Netherlands", "Ireland",
    "Singapore", "United Arab Emirates", "Australia", "Japan",
]
MAX_SEARCHES = 12  # keyword × location pairs per run
MAX_JOBS = 600


@dataclass
class JobSearch:
    keywords: list[str]
    locations: list[str]
    posted: str = "Past week"
    job_types: list[str] = field(default_factory=list)
    workplaces: list[str] = field(default_factory=list)
    experience: list[str] = field(default_factory=list)
    per_search: int = 25
    find_emails: bool = True

    @property
    def searches(self) -> int:
        return len(self.keywords) * len(self.locations)

    @property
    def max_jobs(self) -> int:
        return self.searches * self.per_search

    @property
    def estimated_cost_usd(self) -> float:
        return self.max_jobs * COST_PER_JOB_USD

    def validate(self) -> list[str]:
        errors = []
        if not self.keywords:
            errors.append("Enter at least one job title or keyword.")
        if not self.locations:
            errors.append("Choose at least one location (or type your own).")
        if self.searches > MAX_SEARCHES:
            errors.append(f"That is {self.searches} searches (keywords × locations); keep it to {MAX_SEARCHES} or fewer.")
        if self.max_jobs > MAX_JOBS:
            errors.append(f"Up to {self.max_jobs} jobs requested; keep it to {MAX_JOBS} or fewer per run.")
        return errors

    def run_input(self) -> dict:
        return {
            "titles": self.keywords,
            "locations": self.locations,
            "rows": self.per_search,
            "publishedAt": POSTED[self.posted],
            "contractTypes": [JOB_TYPES[t] for t in self.job_types],
            "workTypes": [WORKPLACES[w] for w in self.workplaces],
            "experienceLevels": [EXPERIENCE[e] for e in self.experience],
            "companyProfile": True,  # adds the company website, used for the email lookup
            "enrichCompany": False,
        }

    def summary(self) -> dict:
        return {
            "Keywords": ", ".join(self.keywords), "Locations": ", ".join(self.locations), "Posted": self.posted,
            "Job type": ", ".join(self.job_types) or "Any", "Workplace": ", ".join(self.workplaces) or "Any",
            "Experience": ", ".join(self.experience) or "Any", "Results per search": self.per_search,
            "Searched on": date.today().isoformat(),
        }


@dataclass
class Job:
    job_id: str
    title: str
    company: str
    location: str = ""
    job_type: str = ""
    experience: str = ""
    workplace: str = ""
    posted_date: str = ""
    posted_ago: str = ""
    applicants: str = ""
    job_url: str = ""
    apply_url: str = ""
    recruiter: str = ""
    recruiter_url: str = ""
    email: str = ""
    email_source: str = ""
    other_emails: str = ""
    company_website: str = ""
    company_size: str = ""
    industry: str = ""
    salary: str = ""
    description: str = ""


def parse_list(text: str) -> list[str]:
    """'Data Analyst, Python developer; ML' -> ['Data Analyst', 'Python developer', 'ML']."""
    return list(dict.fromkeys(p.strip() for p in re.split(r"[,;\n]", text or "") if p.strip()))


def clean_url(url: str) -> str:
    """Drop LinkedIn's tracking query (?trk=...) so links are short and stable."""
    if not url:
        return ""
    p = urlparse(url.strip())
    if "linkedin.com" in p.netloc:
        p = p._replace(query="", fragment="")
    return urlunparse(p)


def normalize(item: dict) -> Job:
    description = (item.get("description") or "").strip()
    website = (item.get("companyWebsite") or "").strip()
    job = Job(
        job_id=str(item.get("id") or ""),
        title=(item.get("title") or "").strip(),
        company=(item.get("companyName") or "").strip(),
        location=item.get("location") or "",
        job_type=item.get("contractType") or "",
        experience=item.get("experienceLevel") or "",
        workplace=item.get("workType") or "",
        posted_date=item.get("publishedAt") or "",
        posted_ago=item.get("postedTime") or "",
        applicants=item.get("applicationsCount") or "",
        job_url=clean_url(item.get("jobUrl") or ""),
        apply_url=clean_url(item.get("applyUrl") or ""),
        recruiter=item.get("posterFullName") or "",
        recruiter_url=clean_url(item.get("posterProfileUrl") or ""),
        company_website=website,
        company_size=item.get("companySize") or "",
        industry=item.get("companyIndustries") or item.get("sector") or "",
        salary=item.get("salary") or "",
        description=description,
    )
    # Some postings say "send your CV to hr@company.com".
    emails = rank_emails(find_emails(description, limit=10), urlparse(website).netloc or "")
    if emails:
        job.email, job.email_source, job.other_emails = emails[0], "job description", ", ".join(emails[1:])
    return job


def company_emails(website: str) -> list[str]:
    """Free lookup: the company's homepage plus its careers/contact page, ranked hiring-first."""
    try:
        home = fetch_links(website)
    except Exception:
        return []
    domain = urlparse(home["url"]).netloc
    emails = list(home["emails"])
    extra = [h for h, t in home["links"] if same_site(h, domain) and (CAREER_RE.search(f"{t} {h}") or CONTACT_RE.search(f"{t} {h}"))]
    for href in list(dict.fromkeys(extra))[:2]:
        try:
            emails += fetch_links(href)["emails"]
        except Exception:
            continue
    return rank_emails(list(dict.fromkeys(emails)), domain)


def fill_from_websites(jobs: list[Job], on_step: Callable[[str], None] = lambda m: None) -> int:
    """Look up an HR email for every job that has a company website but no email yet."""
    sites = list(dict.fromkeys(j.company_website for j in jobs if j.company_website and not j.email))
    if not sites:
        return 0
    on_step(f"✉️ Looking for HR emails on {len(sites)} company websites…")
    with ThreadPoolExecutor(max_workers=8) as pool:
        found = dict(zip(sites, pool.map(company_emails, sites)))
    filled = 0
    for j in jobs:
        emails = found.get(j.company_website) or []
        if not j.email and emails:
            j.email, j.email_source, j.other_emails = emails[0], "company website", ", ".join(emails[1:4])
            filled += 1
    return filled


def search_jobs(settings: Settings, search: JobSearch, on_step: Callable[[str], None] = lambda m: None) -> list[Job]:
    on_step(f"🔎 Searching LinkedIn: {search.searches} search(es), up to {search.max_jobs} jobs "
            f"(about ${search.estimated_cost_usd:.2f} of Apify credit)…")
    items = apify.run_actor(
        settings.apify_token, ACTOR, search.run_input(), memory_mb=1024,
        timeout_secs=min(900, 240 + 4 * search.max_jobs),
        max_charge_usd=search.estimated_cost_usd * 1.5 + 0.05,  # never bill more than the estimate allows
        on_progress=lambda s: on_step(f"⏳ Apify run status: {s}"),
    )
    jobs, seen = [], set()
    for item in items:
        job = normalize(item)
        key = job.job_id or (job.title, job.company, job.location)
        if job.title and key not in seen:
            seen.add(key)
            jobs.append(job)
    on_step(f"📄 {len(jobs)} unique jobs found.")
    if search.find_emails and jobs:
        filled = fill_from_websites(jobs, on_step)
        on_step(f"✉️ HR emails found for {sum(bool(j.email) for j in jobs)} of {len(jobs)} jobs "
                f"({filled} from company websites).")
    jobs.sort(key=lambda j: (j.posted_date or ""), reverse=True)
    return jobs
