"""Opening scout: company website → relevant job openings + who to email.

1. The homepage is read directly to find its Careers / Contact links.
2. Apify crawls from those pages (JS-rendered, ~25 pages), or from the root if
   the homepage had no Careers link. Careers/jobs pages are picked by URL, title
   and content. If they link to an external job board (Greenhouse, Lever,
   Workable, ...), that board is crawled too.
3. Email addresses are pulled from every crawled page and ranked
   (careers@/hr@/talent@ first, noreply@/privacy@ dropped).
4. One Qwen call reads the careers pages + the resume and returns the openings
   actually listed, each scored for fit with the resume and the user's preference.
"""
import difflib
import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse

from . import apify
from .config import Settings
from .llm import LLM
from .rag import KnowledgeBase
from .scraper import fetch_links, find_emails, normalize_url

CAREER_RE = re.compile(r"careers?|jobs?\b|/job|openings?|positions?|vacanc|hiring|join[-_ ]?(us|our[-_ ]team)|"
                       r"work[-_ ]with[-_ ]us|life[-_ ]at|recruit|internships?|opportunit", re.I)
CONTACT_RE = re.compile(r"contact", re.I)
ATS_RE = re.compile(
    r"https?://[\w.-]*(?:greenhouse\.io|lever\.co|workable\.com|ashbyhq\.com|smartrecruiters\.com|"
    r"zohorecruit\.\w+|keka\.com|darwinbox\.\w+|freshteam\.com|recruitee\.com|breezy\.hr|bamboohr\.com|"
    r"myworkdayjobs\.com|teamtailor\.com|personio\.\w+|wellfound\.com|instahyre\.com)[^\s)\"'<>\]]*", re.I)
HIRING_EMAIL = re.compile(r"^(careers?|jobs?|hr|hiring|recruit\w*|talent\w*|people|internships?|join|work|apply)@", re.I)
GENERAL_EMAIL = re.compile(r"^(hello|hi|info|contact|team|founders?|ceo|cto|admin|office)@", re.I)
DROP_EMAIL = re.compile(r"^(no-?reply|donotreply|do-not-reply|unsubscribe|privacy|legal|abuse|security|gdpr|dpo|"
                        r"billing|invoices?|accounts?|press|media|bounce\w*|postmaster|webmaster)@", re.I)

SCOUT_PROMPT = """You help a job seeker find suitable openings at a company. Below are pages from
the company's website (Markdown with links) and the candidate's resume.

1. List the job openings ACTUALLY listed on these pages. Never invent openings. For each give:
   "title", "location" (or ""), "url" (that job's own link if present in the Markdown, else the
   page URL), "summary" (one line with the key requirements).
2. Score each opening 0-100 for fit with the resume{pref_note}. "reason": one sentence naming the
   resume skills/projects that match, and any important gap.
3. "company": the company name. "about": one sentence on what the company does.
4. "note": if no openings are listed, say what the site says about hiring (e.g. "send CV to
   careers@...", "hiring via LinkedIn"); otherwise "".

Return ONLY JSON:
{{"company": "", "about": "", "note": "", "openings": [{{"title": "", "location": "", "url": "", "summary": "", "score": 0, "reason": ""}}]}}

Candidate preference: {preference}

Resume:
{resume}

Website pages:
{pages}"""


@dataclass
class Opening:
    title: str
    location: str = ""
    url: str = ""
    summary: str = ""
    score: int = 0
    reason: str = ""


@dataclass
class ScoutResult:
    website: str
    company: str = ""
    about: str = ""
    note: str = ""
    openings: list[Opening] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    career_pages: list[str] = field(default_factory=list)
    pages_crawled: int = 0
    tokens: int = 0


def same_site(url: str, domain: str) -> bool:
    host, base = urlparse(url).netloc.lower(), domain.removeprefix("www.")
    return host == base or host.endswith("." + base)


MD_LINK = re.compile(r"\[([^\]\n]{3,120})\]\((https?://[^)\s]+)\)")
GENERIC_LINK = re.compile(r"^(apply( now)?|view( all)?( jobs| openings| roles)?|learn more|read more|see more|"
                          r"careers?|jobs?|open (roles|positions)|join (us|our team)|explore.*|find your role|"
                          r"work with us)$", re.I)


def board_root(url: str) -> str:
    """https://job-boards.greenhouse.io/acme/jobs/123 → https://job-boards.greenhouse.io/acme (lists every job)."""
    p = urlparse(url)
    segments = [s for s in p.path.split("/") if s]
    if segments and re.search(r"greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com|wellfound\.com", p.netloc):
        return f"{p.scheme}://{p.netloc}/{segments[0]}"
    return url


def clean_markdown(md: str, boilerplate: set[str] = frozenset()) -> str:
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)  # images
    md = re.sub(r"\[\s*\]\([^)]*\)", "", md)  # empty links
    lines = [ln for ln in md.splitlines() if ln.strip() and ln.strip() not in boilerplate]
    return "\n".join(lines).strip()


def boilerplate_lines(pages: list[dict]) -> set[str]:
    """Lines repeated on most pages: menus, footers, cookie notices."""
    if len(pages) < 3:
        return set()
    counts: dict[str, int] = {}
    for p in pages:
        for ln in {ln.strip() for ln in p["markdown"].splitlines() if ln.strip()}:
            counts[ln] = counts.get(ln, 0) + 1
    return {ln for ln, n in counts.items() if n >= max(3, len(pages) // 2)}


def job_links(page: dict) -> list[str]:
    """Links on a careers page that look like individual job postings."""
    out = []
    for text, href in MD_LINK.findall(page["markdown"]):
        text = text.strip()
        if href.rstrip("/") == page["url"].rstrip("/") or GENERIC_LINK.match(text):
            continue
        if ATS_RE.match(href) or CAREER_RE.search(urlparse(href).path):
            line = f"- [{text}]({href})"
            if line not in out:
                out.append(line)
    return out[:40]


def career_score(page: dict) -> int:
    path = urlparse(page["url"]).path
    score = 3 * len(CAREER_RE.findall(path)) + 2 * len(CAREER_RE.findall(page["title"]))
    if CAREER_RE.fullmatch(path.rstrip("/").rsplit("/", 1)[-1]):
        score += 4  # an index page like /careers or /jobs lists every opening
    score += min(len(re.findall(r"\bapply\b|\bjob description\b|\bresponsibilities\b|\brequirements\b",
                                page["markdown"], re.I)), 5)
    # A listing page links to many postings; it beats any single posting.
    score += 2 * min(len(job_links(page)), 10)
    if ATS_RE.match(page["url"]) and page["url"].rstrip("/") == board_root(page["url"]):
        score += 10  # the job board's index
    return score


def email_kind(email: str) -> str:
    if HIRING_EMAIL.match(email):
        return "hiring contact"
    if GENERAL_EMAIL.match(email):
        return "general inbox"
    return "other — may not reach recruiters"


def rank_emails(emails: list[str], domain: str) -> list[str]:
    base = domain.removeprefix("www.").split(".")[0]

    def key(email: str):
        tier = 0 if HIRING_EMAIL.match(email) else 1 if GENERAL_EMAIL.match(email) else 2
        return tier, base not in email.split("@")[1]  # prefer the company's own domain

    return sorted((e for e in emails if not DROP_EMAIL.match(e)), key=key)


class OpeningScout:
    def __init__(self, settings: Settings, kb: KnowledgeBase, on_step: Callable[[str], None] | None = None):
        self.settings = settings
        self.kb = kb
        self.llm = LLM(settings)
        self.on_step = on_step or (lambda msg: None)

    def run(self, website: str, preference: str = "", max_pages: int = 25) -> ScoutResult:
        website = normalize_url(website)
        domain = urlparse(website).netloc.lower()
        result = ScoutResult(website=website)
        pages, home_ats = [], []

        # Read the homepage ourselves (free, instant) to aim the crawl straight at the
        # Careers / Contact pages; a crawl from the root can spend its whole page budget
        # on product and blog pages first.
        self.on_step(f"🏠 Reading the {domain} homepage for Careers / Contact links…")
        careers, contact = [], []
        try:
            home = fetch_links(website)
            pages.append({"url": home["url"], "title": home["title"],
                          "markdown": home["text"] + "\n" + " ".join(home["emails"])})
            for href, text in home["links"]:
                href = href.rstrip("/")
                if urlparse(href).path in ("", "/"):
                    continue  # the homepage itself: crawling it widens the scope to the whole site
                label = f"{text} {urlparse(href).path}"
                if ATS_RE.match(href):
                    home_ats.append(href)
                elif same_site(href, domain) and CAREER_RE.search(label):
                    careers.append(href)
                elif same_site(href, domain) and CONTACT_RE.search(label):
                    contact.append(href)
        except Exception as exc:
            self.on_step(f"⚠️ Couldn't read the homepage directly ({exc}); Apify will crawl from the root.")
        # Prefer links whose URL (not just link text) looks like careers, e.g. /careers over /about.
        careers = sorted(dict.fromkeys(careers), key=lambda h: not CAREER_RE.search(urlparse(h).path))[:3]
        if careers:
            self.on_step("🎯 Careers links on the homepage: " + ", ".join(careers))
            start_urls = careers + list(dict.fromkeys(contact))[:1]
        else:
            self.on_step("No Careers link on the homepage; crawling the site from the root.")
            root = f"{urlparse(website).scheme}://{domain}"
            start_urls = [website, f"{root}/careers", f"{root}/jobs"]

        root = f"{urlparse(website).scheme}://{domain}"
        # Besides sub-pages of the start URLs, follow any careers-looking link on the
        # site (e.g. /careers → /early-careers/ or /open-roles/).
        globs = [g for w in ("career", "job", "opening", "position", "vacanc", "role", "intern", "hiring")
                 for g in (f"{root}/**/*{w}*", f"{root}/**/*{w}*/**")]
        self.on_step(f"🕸️ Crawling with Apify (up to {max_pages} pages, usually 1-3 min)…")
        crawled = apify.crawl(self.settings.apify_token, start_urls, max_pages=max_pages, include_globs=globs,
                              on_progress=lambda s: self.on_step(f"⏳ Apify run status: {s}"))
        seen = {p["url"].rstrip("/") for p in pages}
        for p in crawled:
            if p["markdown"] and p["url"].rstrip("/") not in seen:
                seen.add(p["url"].rstrip("/"))
                pages.append(p)
        self.on_step(f"📄 Crawled {len(pages)} pages.")

        # External job boards linked from the site hold the actual openings.
        ats_links = list(dict.fromkeys(
            home_ats + [m.rstrip(".,") for p in pages for m in ATS_RE.findall(p["markdown"])]))
        if ats_links:
            board = board_root(ats_links[0])
            self.on_step(f"🔗 Found external job board {urlparse(board).netloc}; crawling it…")
            try:
                pages += apify.crawl(self.settings.apify_token, [board], max_pages=10, max_depth=1)
            except Exception as exc:
                self.on_step(f"⚠️ Job board crawl failed: {exc}")

        result.pages_crawled = len(pages)
        all_text = "\n".join(p["markdown"] for p in pages)
        result.emails = rank_emails(find_emails(all_text, limit=50), domain)
        self.on_step(f"✉️ Emails found: {', '.join(result.emails[:6]) or 'none'}")

        scored = sorted(((career_score(p), p) for p in pages), key=lambda sp: sp[0], reverse=True)
        career = [p for s, p in scored if s >= 3][:3]
        result.career_pages = [p["url"] for p in career]
        self.on_step("🎯 Careers pages: " + (", ".join(result.career_pages) or "none found"))

        self.on_step("🤖 Matching openings to your resume…")
        data = self.llm.json(SCOUT_PROMPT.format(
            pref_note=" and the candidate's preference" if preference else "",
            preference=preference or "(none given)",
            resume=self._resume_text(preference),
            pages=self._pages_text(pages, career, website),
        ), temperature=0.2)

        result.company = str(data.get("company") or domain)
        result.about = str(data.get("about") or "")
        result.note = str(data.get("note") or "")
        for o in data.get("openings") or []:
            try:
                score = int(float(o.get("score") or 0))
            except (TypeError, ValueError):
                score = 0
            result.openings.append(Opening(
                title=str(o.get("title") or "Untitled role"), location=str(o.get("location") or ""),
                url=str(o.get("url") or ""), summary=str(o.get("summary") or ""),
                score=max(0, min(score, 100)), reason=str(o.get("reason") or ""),
            ))
        # The model sometimes pairs a title with a neighbouring job's link; trust the page's own links.
        links = {text.strip().lower(): href for p in career for text, href in MD_LINK.findall(p["markdown"])}
        for o in result.openings:
            best = difflib.get_close_matches(o.title.lower(), list(links), n=1, cutoff=0.75)
            if best:
                o.url = links[best[0]]
        result.openings.sort(key=lambda o: o.score, reverse=True)
        result.tokens = self.llm.tokens
        self.on_step(f"✅ {len(result.openings)} openings found.")
        return result

    def _resume_text(self, preference: str, budget: int = 3800) -> str:
        chunks = self.kb.chunks
        if sum(len(c.text) for c in chunks) > budget:
            chunks = self.kb.search(preference or "skills experience projects education", k=6)
        return "\n\n".join(c.text for c in chunks)[:budget]

    @staticmethod
    def _pages_text(pages: list[dict], career: list[dict], website: str, budget: int = 6500) -> str:
        # Home page (for "about") first, then careers pages with most of the budget.
        # Menus/footers repeated on every page are dropped, and each careers page
        # starts with its job links so they survive truncation.
        boiler = boilerplate_lines(pages)
        home = next((p for p in pages if p["url"].rstrip("/") == website.rstrip("/")), pages[0] if pages else None)
        parts = []
        if home and home not in career:
            parts.append(f"### PAGE: {home['url']}\n{clean_markdown(home['markdown'], boiler)[:1200]}")
        per_page = (budget - 1200) // max(len(career), 1)
        for p in career:
            links = job_links(p)
            head = ("Job links on this page:\n" + "\n".join(links) + "\n\nPage text:\n") if links else ""
            parts.append(f"### PAGE: {p['url']}\n{(head + clean_markdown(p['markdown'], boiler))[:per_page]}")
        return "\n\n".join(parts) or "(no pages could be crawled)"
