"""Crawl websites with Apify's Website Content Crawler (apify/website-content-crawler).

Runs on Apify's servers, renders JavaScript (most careers pages need it) and
returns each page as Markdown with its links, so job links and mailto: addresses
survive. Uses the plain REST API so no extra package is needed.
"""
import time
from typing import Callable

import requests

API = "https://api.apify.com/v2"
ACTOR = "apify~website-content-crawler"
TERMINAL = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}


def crawl(token: str, start_urls: list[str], max_pages: int = 25, max_depth: int = 2,
          include_globs: list[str] = (), timeout_secs: int = 240,
          on_progress: Callable[[str], None] | None = None) -> list[dict]:
    """Crawl `start_urls`, their sub-pages and any links matching `include_globs`.

    Returns [{"url", "title", "markdown"}].
    """
    if not token:
        raise RuntimeError("APIFY_API_TOKEN is not set in .env.")
    headers = {"Authorization": f"Bearer {token}"}
    run_input = {
        "startUrls": [{"url": u} for u in start_urls],
        "crawlerType": "playwright:adaptive",
        "maxCrawlPages": max_pages,
        "maxResults": max_pages,
        "maxCrawlDepth": max_depth,
        "includeUrlGlobs": [{"glob": g} for g in include_globs],
        # Keep everything except scripts/styles: job lists aren't "articles", and
        # contact emails usually live in the footer.
        "htmlTransformer": "none",
        "removeElementsCssSelector": "script, style, noscript, svg, img[src^='data:'], [role=\"dialog\"]",
        "saveMarkdown": True,
        "blockMedia": True,
        "removeCookieWarnings": True,
        "proxyConfiguration": {"useApifyProxy": True},
    }
    resp = requests.post(f"{API}/acts/{ACTOR}/runs", headers=headers, json=run_input,
                         params={"timeout": timeout_secs, "memory": 4096}, timeout=60)
    if resp.status_code == 401:
        raise RuntimeError("Apify rejected the token. Check APIFY_API_TOKEN in .env.")
    resp.raise_for_status()
    run = resp.json()["data"]

    deadline = time.time() + timeout_secs + 60
    while run["status"] not in TERMINAL and time.time() < deadline:
        if on_progress:
            on_progress(run["status"])
        run = requests.get(f"{API}/actor-runs/{run['id']}", headers=headers,
                           params={"waitForFinish": 30}, timeout=60).json()["data"]
    if run["status"] not in TERMINAL:  # don't leave it running (and billing) in the background
        requests.post(f"{API}/actor-runs/{run['id']}/abort", headers=headers, timeout=30)
    if run["status"] in {"FAILED", "ABORTED"}:
        raise RuntimeError(f"Apify crawl {run['status'].lower()}: "
                           f"https://console.apify.com/actors/runs/{run['id']}")

    # A timed-out run still has whatever pages it crawled.
    items = requests.get(f"{API}/datasets/{run['defaultDatasetId']}/items", headers=headers,
                         params={"clean": "true", "format": "json"}, timeout=60).json()
    return [
        {
            "url": item.get("url") or item.get("crawl", {}).get("loadedUrl", ""),
            "title": (item.get("metadata") or {}).get("title") or "",
            "markdown": item.get("markdown") or item.get("text") or "",
        }
        for item in items
    ]
