# QA report — Cold Mail Agent

Date: 14 October 2026 · App version: final submission · Tester: automated suites + browser run in Microsoft Edge

**Result: all checks pass.** 51/51 automated tests, 22/22 end-to-end browser checks, 10 links with 0 broken.

## How to re-run

```bash
.venv\Scripts\python -m pytest
.venv\Scripts\python tools\check_links.py
.venv\Scripts\python tools\e2e_screenshots.py --url http://localhost:8501
```

## Checklist

| Item | What was done | Verified by |
|---|---|---|
| Remove horizontal scrolling | Long URLs and addresses wrap; inner scroll container clipped | E2E: scrollWidth = viewport on every page at 375 px and with content at 1280 px |
| Find broken links | `tools/check_links.py`; example URLs shown as code, not links to real sites | Link check: 10 links, 0 broken |
| Mobile menu | Top navigation (`st.navigation`, position top) collapses into a slide-out menu; "Open menu"/"Close menu" accessible labels | E2E: menu opens at 375 px and navigates |
| Favicon | `assets/favicon.png` (purple envelope) | E2E/browser: `<link rel=icon>` points at the PNG |
| Page titles | Per page: "Write email · Cold Mail Agent", etc. | E2E: title checked on all 4 pages |
| Meta description | description, og:title, og:description, theme-color | E2E: present on all 4 pages |
| Footer links | Footer with page links and setup links (Groq, Apify, Gmail) | UI tests: footer links on every page; link check |
| Custom 404 page | Unknown URLs show "Page not found" with links back; Streamlit's own dialog auto-closed | E2E: /jobs shows 404, dialog closed, link home works |
| Compress images | `tools/compress_images.py`: 1,225 KB → 478 KB (−61%), no visible change | Visual comparison of diagrams |
| Fix broken button | Inputs moved into forms; first click after typing now works | E2E: Generate email and Find openings clicked straight after typing |
| Success messages | Toasts that survive reloads for every action | UI tests (send, mark replied, delete); E2E screenshot |
| Error messages | Validation (empty, invalid URL, too short, bad recipient) + `agent/errors.py` for Groq/Apify/web/Gmail errors | UI tests for each validation; unit tests for 11 error types |
| Placeholder text | `[Your Name]` filled with SENDER_NAME; other placeholders block sending; in-field examples moved to help tooltips | Unit + UI tests |
| Unused navigation | Sidebar radio removed; Deploy button and developer menu hidden (`toolbarMode = "minimal"`) | Browser: no Deploy button |
| Clickable logo | `st.logo` linking home, opened in the same tab, accessible name | E2E: logo target `_self`, href = home |

## Bugs found during QA and fixed

1. **First click ignored**: Generate email / Find openings were disabled until the typed text was committed. Fixed with forms.
2. **Success messages lost**: messages shown just before a reload (inbox check, follow-up sent, status changes) vanished. Fixed with flash toasts.
3. **404 stuck**: after visiting an unknown URL, every page showed the 404 (Streamlit keeps the original URL per session). Fixed by checking only on the first run.
4. **Redirect loop**: an intermediate fix using `st.switch_page` looped; replaced by rendering the 404 in place.
5. **Logo opened a new tab**: Streamlit default; now same tab.
6. **Example URLs linked to a real website** (company.com) in error messages and tooltips. Now shown as code.
7. **Icon-only button without an accessible name** (remove file) and broken glyph; now a labelled "Remove" button.
8. **Agent repeated empty searches**: up to 8 searches and 16,408 tokens per email; the tool now tells the model to stop (9,173 tokens in the retest).
9. **Mobile metrics stacked into one tall column**; now a wrapping row.
10. **Corrupt PDF broke the whole knowledge base**; now skipped and reported.

## End-to-end run (Edge, Playwright)

```
PASS  page title / meta description: Write email, Find openings, Sent & follow-ups, Page not found
PASS  generate email produces a draft            130 words, To=careers@acme-analytics.example
PASS  no horizontal scroll                        /, /find-openings, /sent at 375 px; write, find openings, sent at 1280 px
PASS  mobile menu navigates
PASS  404: dialog closed, link back home works
PASS  logo is a same-tab home link
PASS  find openings returns results               sigmoid.com: Openings (10), careers@sigmoid.com ranked first
PASS  follow-up drafted                           64 words
PASS  no JavaScript errors on the page
22/22 checks passed
```

Screenshots: `docs/screenshots/` (write, find_openings, sent, mobile_menu, not_found).

## Known limitations

- The meta description is injected by JavaScript (Streamlit does not expose `<head>`), so crawlers that don't run scripts won't see it.
- After the 404 page renders, Streamlit rewrites the address bar to the home URL.
- First page load in a new session takes about 1 s (2 s server-side on a cold start while libraries load).
