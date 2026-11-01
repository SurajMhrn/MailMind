"""Find broken links in the app, its docs and its config.

Collects every http(s) URL from the source and docs, then requests each one.
Example addresses (example.com, company.com, ...) are skipped; API base URLs only
need to answer at all (they reply 401/404 to a plain GET by design).

Run:  python tools/check_links.py     (exit code 1 if any link is broken)
"""
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
FILES = ["app.py", "README.md", ".env.example", *[str(p.relative_to(ROOT)) for p in (ROOT / "agent").glob("*.py")],
         *[str(p.relative_to(ROOT)) for p in (ROOT / "docs").glob("*.py")]]
URL_RE = re.compile(r"https?://[^\s\"'`<>)\]}]+")
SKIP = re.compile(r"example\.(com|org)|company\.com|\.example\b|localhost|127\.0\.0\.1|\{|\$|acme|mailer-daemon", re.I)
API_BASES = ("api.groq.com", "openrouter.ai/api", "api.apify.com")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"}


def collect() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for rel in FILES:
        path = ROOT / rel
        if not path.exists():
            continue
        for url in URL_RE.findall(path.read_text(encoding="utf-8", errors="ignore")):
            url = url.rstrip(".,;:")
            if not SKIP.search(url):
                found.setdefault(url, set()).add(rel)
    return found


def check(url: str) -> tuple[str, str, bool]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
    except requests.RequestException as exc:
        return url, f"ERROR {type(exc).__name__}", False
    if any(base in url for base in API_BASES):
        return url, f"{resp.status_code} (API endpoint reachable)", True
    ok = resp.status_code < 400 or resp.status_code in (401, 403, 429)  # login walls and bot checks are live pages
    note = f" -> {resp.url}" if resp.url.rstrip("/") != url.rstrip("/") else ""
    return url, f"{resp.status_code}{note}", ok


def main() -> int:
    links = collect()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(check, sorted(links)))
    broken = 0
    for url, status, ok in results:
        broken += not ok
        print(f"{'OK    ' if ok else 'BROKEN'} {status:<40} {url}  [{', '.join(sorted(links[url]))}]")
    print(f"\n{len(results)} links checked, {broken} broken")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
