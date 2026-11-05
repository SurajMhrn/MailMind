"""End-to-end browser check + screenshots, driving the real app in Microsoft Edge with Playwright.

Checks per page: browser title, meta description, no horizontal scroll (desktop and 375px phone),
mobile menu, custom 404, and the main flows (generate an email, find openings, draft a follow-up).
Screenshots land in docs/screenshots/.

Run against a running app:  python tools/e2e_screenshots.py [--url http://localhost:8501] [--steps write,mobile,404,scout,sent]
Needs: pip install playwright   (uses the installed Edge, no browser download).
The "write" and "sent" steps call Groq; "scout" runs an Apify crawl (a few cents of free credit).
"""
import argparse
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "docs" / "screenshots"
JOB = ("Acme Analytics (Bhubaneswar / Remote) is hiring a Data Analyst Intern for 6 months. You will build Power BI "
       "dashboards for retail clients, write SQL queries and clean data with Python (Pandas). Must have: SQL, Python, "
       "Power BI. Apply: careers@acme-analytics.example")
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def scroll_to(page: Page, locator, offset: int = 0) -> None:
    """Scroll Streamlit's inner scroll container so `locator` sits near the top (minus offset px)."""
    locator.evaluate("""(el, offset) => {
        el.scrollIntoView({block: 'start'});
        let box = el.parentElement;
        while (box && box.scrollHeight <= box.clientHeight) box = box.parentElement;
        (box || document.scrollingElement).scrollBy(0, -offset);
    }""", offset)
    page.wait_for_timeout(500)


def wait_ready(page: Page, heading: str) -> None:
    expect(page.get_by_role("heading", level=1, name=heading)).to_be_visible(timeout=30_000)
    page.wait_for_timeout(800)


def no_horizontal_scroll(page: Page, label: str) -> None:
    width = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    check(f"no horizontal scroll: {label}", width[0] <= width[1], f"scrollWidth={width[0]} viewport={width[1]}")


def head_tags(page: Page, title: str) -> None:
    check(f"page title: {title}", page.title() == f"{title} · Cold Mail Agent", page.title())
    try:  # the tag is added by a script that runs just after the page renders
        page.wait_for_function("(document.querySelector('meta[name=description]')?.content || '').length > 50",
                               timeout=5_000)
    except Exception:
        pass
    desc = page.evaluate("document.querySelector('meta[name=description]')?.content || ''")
    check(f"meta description: {title}", len(desc) > 50, desc[:60] + "…")


def step_write(page: Page, base: str) -> None:
    page.goto(base)
    wait_ready(page, "Write a cold email")
    head_tags(page, "Write email")
    page.get_by_label("…or paste the job description / company info", exact=True).fill(JOB)
    page.get_by_role("button", name="Generate email").click()  # first click, right after typing
    body = page.get_by_label("Body", exact=True)
    expect(body).to_be_visible(timeout=180_000)
    check("generate email produces a draft", len(body.input_value().split()) > 50,
          f"{len(body.input_value().split())} words, To={page.get_by_label('To', exact=True).input_value()}")
    no_horizontal_scroll(page, "write (desktop, with draft)")
    page.wait_for_timeout(1500)
    scroll_to(page, page.get_by_role("heading", level=1, name="Write a cold email"), offset=0)
    page.screenshot(path=SHOTS / "write.png")


def step_mobile(browser, base: str) -> None:
    ctx = browser.new_context(viewport={"width": 375, "height": 812}, device_scale_factor=2, is_mobile=True,
                              has_touch=True, color_scheme="light")
    page = ctx.new_page()
    for path, heading in (("", "Write a cold email"), ("find-openings", "Find openings on a company website"),
                          ("sent", "Sent emails & follow-ups")):
        page.goto(base + path)
        wait_ready(page, heading)
        no_horizontal_scroll(page, f"/{path} at 375px")
    page.goto(base)
    wait_ready(page, "Write a cold email")
    page.get_by_role("button", name="Open menu").click()
    nav = page.get_by_role("link", name="Find openings").first
    expect(nav).to_be_visible(timeout=10_000)
    page.wait_for_timeout(600)
    page.screenshot(path=SHOTS / "mobile_menu.png")
    nav.click()
    wait_ready(page, "Find openings on a company website")
    check("mobile menu navigates", True, page.url)
    ctx.close()


def step_404(page: Page, base: str) -> None:
    page.goto(base + "jobs")
    wait_ready(page, "Page not found")
    head_tags(page, "Page not found")
    page.wait_for_timeout(1800)  # Streamlit's own dialog is closed automatically
    dialogs = page.locator('[role="dialog"]:has-text("does not seem to exist")').count()
    check("404: Streamlit's duplicate dialog closed", dialogs == 0)
    page.screenshot(path=SHOTS / "not_found.png")
    page.get_by_role("link", name="Go to Write email").click()
    wait_ready(page, "Write a cold email")
    check("404: link back home works", True)
    logo = page.get_by_role("link", name="Cold Mail Agent home")
    check("logo is a same-tab home link", logo.get_attribute("target") == "_self", logo.get_attribute("href") or "")


def step_scout(page: Page, base: str, site: str) -> None:
    page.goto(base + "find-openings")
    wait_ready(page, "Find openings on a company website")
    head_tags(page, "Find openings")
    page.get_by_label("Company website", exact=True).fill(site)
    page.get_by_label("What are you looking for? (optional)", exact=True).fill("data analyst / Python internship or entry level")
    page.get_by_role("button", name="Find openings").click()
    expect(page.get_by_text("Who to email")).to_be_visible(timeout=420_000)
    count = page.locator("text=/Openings \\(\\d+\\)/").first.inner_text()
    check("find openings returns results", True, count)
    no_horizontal_scroll(page, "find openings (desktop, with results)")
    page.wait_for_timeout(4500)  # let the "found N openings" toast fade
    scroll_to(page, page.get_by_text("Who to email"), offset=220)
    page.screenshot(path=SHOTS / "find_openings.png")


def step_sent(page: Page, base: str) -> None:
    page.goto(base + "sent")
    wait_ready(page, "Sent emails & follow-ups")
    head_tags(page, "Sent & follow-ups")
    page.get_by_role("button", name="Draft follow-up #1").first.click()
    follow = page.get_by_label("Follow-up", exact=True)
    expect(follow).to_be_visible(timeout=120_000)
    check("follow-up drafted", len(follow.input_value().split()) > 20, f"{len(follow.input_value().split())} words")
    no_horizontal_scroll(page, "sent (desktop, with follow-up)")
    page.wait_for_timeout(4500)
    scroll_to(page, page.get_by_text("Sent emails & follow-ups").first, offset=0)
    scroll_to(page, follow, offset=420)
    page.screenshot(path=SHOTS / "sent.png")


def step_jobsearch(page: Page, base: str) -> None:
    page.goto(base + "job-search")
    wait_ready(page, "Search jobs worldwide")
    head_tags(page, "Job search")
    page.get_by_role("button", name="Search jobs").click()  # no keywords yet
    expect(page.get_by_text("Enter at least one job title or keyword.")).to_be_visible(timeout=15_000)
    check("job search: validation message", True)
    no_horizontal_scroll(page, "job search (desktop)")
    page.screenshot(path=SHOTS / "job_search.png")


def step_bulk(page: Page, base: str, sheet: str) -> None:
    page.goto(base + "bulk-send")
    wait_ready(page, "Email many openings")
    head_tags(page, "Bulk send")
    uploader = page.locator('[data-testid="stFileUploader"]').filter(has_text="Openings sheet")
    uploader.locator('input[type="file"]').set_input_files(sheet)
    expect(page.get_by_text("2 · Draft the emails")).to_be_visible(timeout=30_000)
    check("bulk send: sheet read and rows checked", True, page.locator('[data-testid="stMetric"]').first.inner_text().replace("\n", " "))
    no_horizontal_scroll(page, "bulk send (desktop, sheet loaded)")
    page.screenshot(path=SHOTS / "bulk_send.png")


def step_mobile_new(browser, base: str) -> None:
    ctx = browser.new_context(viewport={"width": 375, "height": 812}, device_scale_factor=2, is_mobile=True,
                              has_touch=True, color_scheme="light")
    page = ctx.new_page()
    for path, heading in (("job-search", "Search jobs worldwide"), ("bulk-send", "Email many openings")):
        page.goto(base + path)
        wait_ready(page, heading)
        no_horizontal_scroll(page, f"/{path} at 375px")
    ctx.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8501")
    ap.add_argument("--steps", default="write,mobile,404,scout,sent")
    ap.add_argument("--site", default="https://www.sigmoid.com")
    ap.add_argument("--sheet", default="", help="an .xlsx of openings for the bulk step (e.g. a Job search export)")
    args = ap.parse_args()
    base = args.url.rstrip("/") + "/"
    steps = args.steps.split(",")
    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 860}, device_scale_factor=1.5, color_scheme="light")
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        for name in steps:
            try:
                {"write": lambda: step_write(page, base), "mobile": lambda: step_mobile(browser, base),
                 "404": lambda: step_404(page, base), "scout": lambda: step_scout(page, base, args.site),
                 "sent": lambda: step_sent(page, base), "jobsearch": lambda: step_jobsearch(page, base),
                 "bulk": lambda: step_bulk(page, base, args.sheet), "mobile2": lambda: step_mobile_new(browser, base)}[name]()
            except Exception as exc:
                check(f"step {name}", False, f"{type(exc).__name__}: {str(exc)[:200]}")
        check("no JavaScript errors on the page", not errors, "; ".join(errors)[:200])
        browser.close()
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
