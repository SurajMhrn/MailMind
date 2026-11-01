"""Fetch a web page (job post, company site) and reduce it to readable text."""
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Common false positives from image filenames and tracking pixels.
_JUNK_EMAIL = re.compile(r"\.(png|jpe?g|gif|svg|webp)$|example\.com$|sentry|wixpress", re.I)


URL_RE = re.compile(r"https?://[^\s/$.?#][^\s.]*\.[^\s]{2,}", re.I)


def normalize_url(url: str) -> str:
    url = url.strip()
    return url if re.match(r"https?://", url, re.I) else f"https://{url}"


def is_valid_url(url: str) -> bool:
    """A full http(s) address with a domain, e.g. https://company.com/careers."""
    return bool(URL_RE.fullmatch(normalize_url(url))) and " " not in url.strip()


def fetch_page(url: str, max_chars: int = 8000) -> dict:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    mailtos = [a["href"][7:].split("?")[0] for a in soup.select('a[href^="mailto:"]')]
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form"]):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    text = re.sub(r"\n\s*\n+", "\n\n", soup.get_text("\n", strip=True))
    return {
        "url": url,
        "title": title,
        "text": text[:max_chars],
        "emails": find_emails(resp.text + " " + " ".join(mailtos)),
    }


def fetch_links(url: str) -> dict:
    """Fetch one page and return its readable text, emails and absolute links [(href, text)]."""
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(resp.url, a["href"].strip()).split("#")[0]
        if href.startswith("http"):
            links.append((href, a.get_text(" ", strip=True)))
    emails = find_emails(resp.text, limit=30)
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = re.sub(r"\n\s*\n+", "\n\n", soup.get_text("\n", strip=True))
    return {"url": resp.url, "title": soup.title.get_text(strip=True) if soup.title else "",
            "text": text, "emails": emails, "links": links}


def find_emails(text: str, limit: int = 10) -> list[str]:
    seen = []
    for email in EMAIL_RE.findall(text):
        email = email.lower().rstrip(".")
        if email not in seen and not _JUNK_EMAIL.search(email):
            seen.append(email)
    return seen[:limit]
