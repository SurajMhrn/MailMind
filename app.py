"""Streamlit UI for the cold-email agent.  Run:  streamlit run app.py"""
import hashlib
import hmac
import json
import os
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import streamlit as st

from agent.agent import CTAS, LENGTHS, TONES, ColdEmailAgent, EmailStyle, find_placeholders, refine_email
from agent.bulk import LOOKUP, READY, draft, find_email_on_page, plan, problems, sent_today
from agent.bulk import now as bulk_now
from agent.config import COLLECTION, load_settings
from agent.errors import friendly_error
from agent.excel import read_openings, write_jobs, write_results
from agent.followup import SentEmail, SentLog, generate_followup, reply_subject
from agent.jobsearch import (COST_PER_JOB_USD, EXPERIENCE, JOB_TYPES, LOCATION_PRESETS, POSTED, WORKPLACES, JobSearch,
                             parse_list, search_jobs)
from agent.llm import LLM
from agent.mailer import check_threads, days_since, send_email
from agent.rag import KnowledgeBase
from agent.scout import OpeningScout, email_kind
from agent.scraper import EMAIL_RE, is_valid_url, normalize_url

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
APP_NAME = "Cold Mail Agent"
DESCRIPTION = ("Agentic AI that finds job openings on company websites, writes resume-grounded cold emails "
               "with Qwen, and tracks replies and follow-ups.")

st.set_page_config(page_title=APP_NAME, page_icon=str(ASSETS / "favicon.png"), layout="wide",
                   menu_items={"About": f"**{APP_NAME}** — {DESCRIPTION}"})
settings = load_settings()
ss = st.session_state
sent_log = SentLog()

# When APP_PASSWORD is set (always, for a public deployment), nobody can use the app, and so the
# Gmail account and API keys behind it, without the password.
if (app_password := os.getenv("APP_PASSWORD", "")) and not ss.get("signed_in"):
    st.logo(str(ASSETS / "logo.png"), size="large")
    st.title("Sign in")
    with st.form("sign_in"):
        given = st.text_input("Password", type="password")
        if st.form_submit_button("Sign in", type="primary"):
            if hmac.compare_digest(given.encode(), app_password.encode()):
                ss["signed_in"] = True
                st.rerun()
            st.error("Wrong password.", icon=":material/error:")
    st.stop()

QUICK_EDITS = {
    "Shorter": "Make it about 30% shorter without losing the proof points.",
    "More formal": "Make the tone more formal.",
    "Friendlier": "Make the tone warmer and more conversational.",
    "Stronger opening": "Rewrite the first line so it is more specific and compelling.",
    "Better subject": "Write a more specific, curiosity-sparking subject line (under 9 words).",
}
HELP_LINKS = {
    "Get a Groq API key": "https://console.groq.com/keys",
    "Groq models": "https://console.groq.com/docs/models",
    "Apify API token": "https://console.apify.com/settings/integrations",
    "Gmail App Passwords": "https://myaccount.google.com/apppasswords",
}

# Widget values can only be changed before the widget is drawn, so actions that
# change them (refine, follow-up drafts, "draft email for this opening") park
# the new values in `pending` and rerun; they are applied here, first thing.
for key, value in ss.pop("pending", {}).items():
    ss[key] = value

# Streamlit forgets a widget's value when its page isn't shown; re-assigning keeps
# drafts and settings alive while you switch between pages.
PERSIST = ("target_", "style_", "draft_", "review_pass", "send_attach", "fu_body_", "fu_extra_", "scout_site", "scout_pref",
           "scout_pages", "js_", "bulko_", "bulkd_", "bulk_confirm")
for key in [k for k in ss.keys() if isinstance(k, str) and k.startswith(PERSIST)]:
    ss[key] = ss[key]
ss.setdefault("style_length", "Medium")
ss.setdefault("review_pass", True)
ss.setdefault("scout_pages", 25)
ss.setdefault("uploader_n", 0)
for key, value in {"js_locations": ["India"], "js_posted": "Past week", "js_per_search": 25, "js_find_emails": True,
                   "bulko_length": "Medium", "bulko_count": 10, "bulko_delay": settings.bulk_delay_secs,
                   "bulko_lookup": True}.items():
    ss.setdefault(key, value)


# ---------------- helpers ----------------
def flash(message: str, icon: str = ":material/check_circle:") -> None:
    """A toast that survives the rerun that follows most actions."""
    ss.setdefault("flash", []).append((message, icon))


def rerun(**values) -> None:
    ss["pending"] = values
    st.rerun()


def go_to(page_key: str, **values) -> None:
    ss["pending"] = values
    st.switch_page(PAGES[page_key])


def show_errors(errors: list[str]) -> bool:
    for e in errors:
        st.error(e, icon=":material/error:")
    return bool(errors)


def md_escape(text: str) -> str:
    """Scraped or model-written text shown as Markdown must not turn into links or formatting."""
    return re.sub(r"([\\`*_{}\[\]<>()#+\-.!|~])", r"\\\1", text or "")


def short_url(url: str, limit: int = 48) -> str:
    p = urlparse(url)
    text = (p.netloc.removeprefix("www.") + p.path).rstrip("/")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def safe_link(url: str) -> bool:
    return bool(re.match(r"https?://", url or "", re.I))


def head(title: str) -> None:
    """Browser tab title and meta description for the current page."""
    st.set_page_config(page_title=f"{title} · {APP_NAME}")
    meta = json.dumps({"description": DESCRIPTION, "og:title": f"{title} · {APP_NAME}",
                       "og:description": DESCRIPTION, "theme-color": "#534AB7"})
    st.html(f"""<script>
      for (const [name, content] of Object.entries({meta})) {{
        const attr = name.startsWith("og:") ? "property" : "name";
        let tag = document.head.querySelector(`meta[${{attr}}="${{name}}"]`);
        if (!tag) {{ tag = document.createElement("meta"); tag.setAttribute(attr, name); document.head.appendChild(tag); }}
        tag.setAttribute("content", content);
      }}
      // Streamlit opens the logo link in a new tab; the logo is "home", so stay in this tab.
      if (!window.__cmaLogo) {{
        window.__cmaLogo = true;
        const label = (sel, text) => document.querySelectorAll(sel).forEach(b => {{
          if (b.getAttribute("aria-label") !== text) {{ b.setAttribute("aria-label", text); b.title = text; }}
        }});
        const fix = () => {{
          document.querySelectorAll('[data-testid="stLogoLink"]').forEach(a => {{
            if (a.target !== "_self") {{ a.target = "_self"; a.removeAttribute("rel"); a.setAttribute("aria-label", "{APP_NAME} home"); }}
          }});
          // Streamlit's sidebar toggles have no accessible name; on phones the sidebar is the menu.
          label('[data-testid="stExpandSidebarButton"]', "Open menu");
          label('[data-testid="stSidebarCollapseButton"] button', "Close menu");
        }};
        fix();
        new MutationObserver(fix).observe(document.body, {{childList: true, subtree: true}});
      }}
    </script>""", unsafe_allow_javascript=True)


@st.cache_resource
def get_kb() -> KnowledgeBase:
    return KnowledgeBase(COLLECTION, settings)


def smtp_ready() -> bool:
    return bool(settings.smtp_user and settings.smtp_password)


def current_style() -> EmailStyle:
    return EmailStyle(
        tone=ss.get("style_tone", "Professional"), length=ss.get("style_length", "Medium"),
        cta=ss.get("style_cta", "Ask for a short call"), highlight=ss.get("style_highlight", "").strip(),
        extra=ss.get("style_extra", "").strip(), attach_resume=ss.get("style_attach", False),
    )


def home_url() -> str | None:
    url = getattr(st.context, "url", None)
    if not url:
        return None
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/"


# Long URLs and addresses wrap instead of pushing the page sideways on small screens.
st.html("""<style>
  .stMarkdown, .stCaption, .stAlert, [data-testid="stText"] { overflow-wrap: anywhere; word-break: break-word; }
  .stMarkdown pre, [data-testid="stText"] { white-space: pre-wrap; }
  [data-testid="stAppViewContainer"] { overflow-x: hidden; }
  img { max-width: 100%; }
  .cma-footer { font-size: 0.85rem; opacity: 0.75; }
</style>""")


# ---------------- sidebar: knowledge base, accounts ----------------
def sidebar(kb: KnowledgeBase) -> None:
    with st.sidebar:
        st.subheader("Your resume & projects")
        n_files = len(kb.files())
        st.caption(f"{n_files} file{'s' * (n_files != 1)} · {len(kb.chunks)} chunks · "
                   f"{'embeddings' if settings.use_embeddings else 'BM25'} retrieval")
        if not kb.files():
            st.warning("Upload your resume (PDF) so the agent has something to work from.")
        for f in kb.files():
            st.markdown(f":material/description: {md_escape(f.name)}")
            if st.button("Remove", icon=":material/delete:", type="tertiary", key=f"rm-{f.name}",
                         help=f"Remove {f.name} from the knowledge base"):
                kb.remove_file(f.name)
                flash(f"Removed {f.name}", ":material/delete:")
                st.rerun()
        for name, err in kb.errors.items():
            st.error(f"Couldn't read {name}: {err}. Re-export it as a normal PDF.", icon=":material/error:")
        uploads = st.file_uploader("Add resume / project docs", type=["pdf", "md", "txt"],
                                   accept_multiple_files=True, key=f"uploader_{ss.uploader_n}",
                                   help="PDF, Markdown or text, up to 10 MB each.")
        if uploads and st.button("Add to knowledge base", icon=":material/upload:"):
            empty = []
            for up in uploads:
                if kb.add_file(up.name, up.getvalue()) == 0:
                    empty.append(up.name)
            ok = len(uploads) - len(empty)
            if ok:
                flash(f"Added {ok} document{'s' * (ok != 1)} to your knowledge base")
            for name in empty:
                flash(f"{name} has no readable text (scanned image?). Upload a text-based PDF.", ":material/warning:")
            ss.uploader_n += 1  # clears the uploader
            st.rerun()
        with st.expander("Test retrieval"):
            q = st.text_input("Search your resume", help="Shows the chunks the agent would retrieve.")
            hits = kb.search(q) if q else []
            if q and not hits:
                st.caption("No matching chunks.")
            for hit in hits:
                st.markdown(f"**{md_escape(hit.source)}**")
                st.caption(hit.text[:400])

        st.subheader("Email account")
        if smtp_ready():
            st.caption(f"Sending from **{settings.smtp_user}** via {settings.smtp_host}")
            if st.button("Send test email to myself", icon=":material/outgoing_mail:"):
                with st.spinner("Sending…"):
                    try:
                        send_email(settings, settings.smtp_user, "Cold Mail Agent test",
                                   "Your email is connected. Emails from the agent will be sent from this account.")
                        st.success("Test email sent. Check your inbox.", icon=":material/check_circle:")
                    except Exception as exc:
                        st.error(friendly_error(exc), icon=":material/error:")
        else:
            st.warning("Not connected. Set SMTP_USER and SMTP_PASSWORD in .env, then restart the app.")

        st.subheader("Status")
        st.caption(f"{':material/check_circle:' if settings.api_key else ':material/cancel:'} "
                   f"Model `{settings.model}` via {settings.provider}")
        st.caption(f"{':material/check_circle:' if settings.apify_token else ':material/cancel:'} "
                   f"Apify {'connected' if settings.apify_token else 'not set (needed for Find openings)'}")
        if not settings.api_key:
            st.error("No Groq API key. Add GROQ_API_KEY to .env, then restart the app.", icon=":material/error:")


# ---------------- page: write email ----------------
def run_agent(kb: KnowledgeBase) -> None:
    icons = {"tool": ":material/build:", "info": ":material/psychology:"}
    with st.status("Agent working…", expanded=True) as status:
        def show(step):
            st.markdown(f"{icons[step['type']]} **{step['name']}** — {md_escape(step['detail'])}")
        try:
            result = ColdEmailAgent(settings, kb, on_step=show).run(
                normalize_url(ss.target_url) if ss.get("target_url", "").strip() else "",
                ss.get("target_notes", "").strip(), ss.get("target_recipient", "").strip(),
                ss.get("review_pass", True), current_style())
        except Exception as exc:
            status.update(label="Couldn't write the email", state="error")
            st.error(friendly_error(exc), icon=":material/error:")
            return
        status.update(label=f"Done · {result.tokens:,} tokens", state="complete", expanded=False)
    ss["result"] = result
    rerun(draft_to=result.draft.to, draft_subject=result.draft.subject, draft_body=result.draft.body,
          send_attach=current_style().attach_resume, send_confirm=False)


def validate_target() -> list[str]:
    url, notes = ss.get("target_url", "").strip(), ss.get("target_notes", "").strip()
    recipient = ss.get("target_recipient", "").strip()
    errors = []
    if not settings.api_key:
        errors.append("No Groq API key is set. Add GROQ_API_KEY to .env and restart the app.")
    if not url and not notes:
        errors.append("Add a job post URL or paste the job description.")
    if url and not is_valid_url(url):
        errors.append(f"“{url}” isn't a valid web address. Use a full URL like `https://company.com/careers/role`.")
    if not url and notes and len(notes) < 40:
        errors.append("The job description is too short. Paste the full text so the agent can match your resume.")
    if "@" in recipient and not EMAIL_RE.search(recipient):
        errors.append(f"“{recipient}” doesn't look like an email address.")
    return errors


def page_write() -> None:
    head("Write email")
    kb = get_kb()
    st.title("Write a cold email")
    col_in, col_out = st.columns(2, gap="large")

    with col_in:
        st.subheader("1 · Target")
        if ss.get("target_label"):
            st.info(f"From Find openings: **{md_escape(ss['target_label'])}**", icon=":material/travel_explore:")
        with st.form("target_form", border=False):
            st.text_input("Job post / company URL", key="target_url",
                          help="For example `https://company.com/careers/data-analyst`. LinkedIn, Naukri and "
                               "Indeed block automated reading; paste their text below instead.")
            st.text_area("…or paste the job description / company info", key="target_notes", height=160)
            st.text_input("Recipient name or email (optional)", key="target_recipient",
                          help="A name is used in the greeting; an email address goes straight into To.")
            with st.expander("Customize the email", icon=":material/tune:"):
                c1, c2 = st.columns(2)
                c1.selectbox("Tone", list(TONES), key="style_tone")
                c2.selectbox("Length", list(LENGTHS), key="style_length",
                             format_func=lambda k: f"{k} ({LENGTHS[k][0]}-{LENGTHS[k][1]} words)")
                st.selectbox("Call to action", list(CTAS), key="style_cta")
                st.text_input("Skills / projects to highlight", key="style_highlight",
                              help="For example: Power BI dashboards, Django project.")
                st.text_area("Extra instructions", key="style_extra", height=70,
                             help="For example: mention I can join immediately.")
                st.checkbox("I'll attach my resume (the email will mention it)", key="style_attach")
            st.checkbox("Run reviewer pass (critique + rewrite)", key="review_pass")
            submitted = st.form_submit_button("Generate email", type="primary", icon=":material/auto_awesome:")
        if (submitted or ss.pop("autogen", False)) and not show_errors(validate_target()):
            run_agent(kb)

    with col_out:
        st.subheader("2 · Review & send")
        result = ss.get("result")
        if not result:
            st.info("Generated emails appear here. Nothing is sent until you click Send.", icon=":material/info:")
            return
        review_panel(result, kb)


def review_panel(result, kb: KnowledgeBase) -> None:
    to = st.text_input("To", key="draft_to").strip()
    if result.found_emails:
        st.caption("Emails found: " + ", ".join(result.found_emails))
    elif not to:
        st.warning("No email address was found. Type the recruiter's address above.", icon=":material/warning:")
    if to and not EMAIL_RE.fullmatch(to):
        st.error(f"“{to}” is not an email address. Enter one like name@company.com.", icon=":material/error:")
    subject = st.text_input("Subject", key="draft_subject")
    body = st.text_area("Body", key="draft_body", height=300)
    st.caption(f"{len(body.split())} words")
    placeholders = find_placeholders(subject + "\n" + body)
    if placeholders:
        st.warning("Replace these placeholders before sending: " + ", ".join(placeholders),
                   icon=":material/edit_note:")
    if result.draft.critique:
        st.caption(f"Last edit: {md_escape(result.draft.critique)}")
    if result.trace:
        with st.expander(f"How the agent wrote this ({len(result.trace)} steps, {result.tokens:,} tokens)"):
            icons = {"tool": ":material/build:", "info": ":material/psychology:"}
            for step in result.trace:
                st.markdown(f"{icons[step['type']]} **{step['name']}** — {md_escape(step['detail'])}")
    if result.draft.evidence:
        with st.expander("Evidence used from your resume"):
            for e in result.draft.evidence:
                st.markdown(f"- {md_escape(e)}")

    # ---- refine ----
    with st.form("refine_form"):
        st.markdown("**Refine with AI**")
        quick = st.pills("Quick edits", list(QUICK_EDITS), key="refine_quick", label_visibility="collapsed")
        st.text_input("Or describe the change", key="refine_text",
                      help="For example: mention my Django project, drop the last paragraph.")
        apply = st.form_submit_button("Apply edit", icon=":material/edit:")
    if apply:
        instruction = ss.get("refine_text", "").strip() or QUICK_EDITS.get(quick or "", "")
        if not instruction:
            st.error("Pick a quick edit or describe the change you want.", icon=":material/error:")
        else:
            with st.spinner("Rewriting…"):
                try:
                    draft = result.draft
                    draft.subject, draft.body = subject, body  # include the user's manual edits
                    result.draft = refine_email(LLM(settings), draft, instruction, current_style(),
                                                settings.sender_name)
                except Exception as exc:
                    st.error(friendly_error(exc), icon=":material/error:")
                else:
                    flash("Draft updated" + (f": {result.draft.critique}" if result.draft.critique else ""))
                    rerun(draft_subject=result.draft.subject, draft_body=result.draft.body,
                          refine_text="", refine_quick=None)

    # ---- send ----
    resumes = [f for f in kb.files() if f.suffix.lower() == ".pdf"]
    attachments = []
    if resumes and st.checkbox("Attach resume", key="send_attach"):
        chosen = st.selectbox("Resume file", resumes, format_func=lambda p: p.name) \
            if len(resumes) > 1 else resumes[0]
        attachments = [chosen]
    confirm = st.checkbox(f"I've reviewed this email and want to send it to {to or 'the recipient'}",
                          key="send_confirm")
    fingerprint = hashlib.sha256(f"{to}\n{subject}\n{body}".encode()).hexdigest()
    if ss.get("last_sent") == fingerprint:
        st.success(f"Sent to {to}. Track replies and follow-ups in Sent & follow-ups.", icon=":material/mark_email_read:")
    c1, c2 = st.columns(2)
    if c1.button("Send email", type="primary", icon=":material/send:", use_container_width=True):
        errors = []
        if not smtp_ready():
            errors.append("Email isn't connected. Set SMTP_USER and SMTP_PASSWORD in .env and restart the app.")
        if not EMAIL_RE.fullmatch(to):
            errors.append("Enter a valid recipient email address in To.")
        if not subject.strip() or not body.strip():
            errors.append("The subject and body can't be empty.")
        if placeholders:
            errors.append("Replace the placeholders first: " + ", ".join(placeholders))
        if not confirm:
            errors.append("Tick “I've reviewed this email” to confirm you want to send it.")
        if ss.get("last_sent") == fingerprint:
            errors.append(f"This exact email was already sent to {to}.")
        if not show_errors(errors):
            with st.spinner(f"Sending to {to}…"):
                try:
                    message_id = send_email(settings, to, subject, body, attachments=attachments)
                except Exception as exc:
                    st.error(friendly_error(exc), icon=":material/error:")
                else:
                    target = ss.get("target_label") or ss.get("target_url") or \
                        ss.get("target_notes", "").strip().split("\n")[0][:80]
                    sent_log.add(SentEmail(to=to, subject=subject, body=body, message_id=message_id,
                                           target=target, evidence=result.draft.evidence))
                    ss["last_sent"] = fingerprint
                    flash(f"Email sent to {to}", ":material/mark_email_read:")
                    rerun(send_confirm=False)
    c2.download_button("Download as .txt", f"To: {to}\nSubject: {subject}\n\n{body}", file_name="email.txt",
                       icon=":material/download:", use_container_width=True)


# ---------------- page: find openings ----------------
def page_scout() -> None:
    head("Find openings")
    kb = get_kb()
    st.title("Find openings on a company website")
    st.caption("Apify crawls the site, finds the careers/jobs pages and contact emails, and Qwen ranks "
               "the openings against your resume. Pick one to draft a cold email.")
    if not settings.apify_token:
        st.warning("Find openings needs an Apify token. Add APIFY_API_TOKEN to .env, then restart the app.",
                   icon=":material/key:")
    with st.form("scout_form", border=False):
        c1, c2 = st.columns([3, 2])
        c1.text_input("Company website", key="scout_site", help="For example `https://company.com`")
        c2.text_input("What are you looking for? (optional)", key="scout_pref",
                      help="For example: data analyst / Python internship, remote.")
        st.slider("Pages to crawl", 10, 60, step=5, key="scout_pages",
                  help="More pages finds more, but takes longer and uses more Apify credit.")
        submitted = st.form_submit_button("Find openings", type="primary", icon=":material/travel_explore:")
    if submitted:
        site = ss.get("scout_site", "").strip()
        errors = []
        if not site:
            errors.append("Enter the company's website.")
        elif not is_valid_url(site):
            errors.append(f"“{site}” isn't a valid web address. Use one like `https://company.com`.")
        if not settings.apify_token:
            errors.append("APIFY_API_TOKEN is missing from .env.")
        if not settings.api_key:
            errors.append("GROQ_API_KEY is missing from .env.")
        if not show_errors(errors):
            with st.status("Scouting… this usually takes 1-3 minutes", expanded=True) as status:
                try:
                    ss["scout"] = OpeningScout(settings, kb, on_step=st.write).run(
                        site, ss.get("scout_pref", ""), ss.scout_pages)
                except Exception as exc:
                    status.update(label="Scan failed", state="error")
                    st.error(friendly_error(exc), icon=":material/error:")
                else:
                    ss.pop("scout_email", None)  # the old pick may not exist in the new results
                    status.update(label=f"Done · {ss['scout'].tokens:,} tokens", state="complete", expanded=False)
                    n = len(ss["scout"].openings)
                    st.toast(f"Found {n} opening{'s' * (n != 1)} at {ss['scout'].company}", icon=":material/check_circle:")

    res = ss.get("scout")
    if res:
        scout_results(res)


def scout_results(res) -> None:
    st.divider()
    st.subheader(res.company)
    if res.about:
        st.write(res.about)
    pages = ", ".join(f"[{short_url(u)}]({u})" for u in res.career_pages if safe_link(u)) or "none found"
    st.caption(f"{res.pages_crawled} pages crawled · careers pages: {pages}")
    if res.note:
        st.info(res.note, icon=":material/info:")

    st.markdown("**Who to email**")
    options = res.emails + ["Other…"]
    pick = st.radio("Recipient", options, key="scout_email", label_visibility="collapsed",
                    captions=[email_kind(e) for e in res.emails] + ["type an address"])
    recipient = st.text_input("Email address", key="scout_email_other").strip() if pick == "Other…" else pick
    if pick == "Other…" and recipient and not EMAIL_RE.fullmatch(recipient):
        st.error(f"“{recipient}” is not an email address.", icon=":material/error:")
    if not res.emails:
        st.warning("No email addresses were found on the site. Find the recruiter on LinkedIn or the "
                   "company's contact page and type it above.", icon=":material/warning:")
    elif not any(email_kind(e) == "hiring contact" for e in res.emails):
        st.warning("No hiring/HR address was found, only general ones. A recruiter's address from LinkedIn "
                   "(choose Other…) will usually get a better response.", icon=":material/warning:")

    st.markdown(f"**Openings ({len(res.openings)})**")
    if not res.openings:
        st.write("No openings are listed on the crawled pages. You can still send a general application email.")
    for i, o in enumerate(res.openings):
        color = "green" if o.score >= 70 else "orange" if o.score >= 40 else "red"
        with st.container(border=True):
            st.markdown(f"**{md_escape(o.title)}** :{color}-badge[{o.score}% match]"
                        + (f"  \n:material/location_on: {md_escape(o.location)}" if o.location else ""))
            if o.summary:
                st.caption(md_escape(o.summary))
            if o.reason:
                st.markdown(md_escape(o.reason))
            c1, c2 = st.columns([1, 2], vertical_alignment="center")
            if safe_link(o.url):
                c1.link_button("View posting", o.url, icon=":material/open_in_new:")
            if c2.button("Draft email for this opening", key=f"draft-{i}", icon=":material/arrow_forward:"):
                draft_for(res, recipient, o)
    if st.button("Draft a general application email", icon=":material/arrow_forward:"):
        draft_for(res, recipient, None)


def draft_for(res, recipient: str, opening) -> None:
    lines = [f"Company: {res.company} ({res.website})"]
    if res.about:
        lines.append(f"About: {res.about}")
    if opening:
        lines += [f"Role: {opening.title}" + (f" ({opening.location})" if opening.location else ""),
                  f"Requirements: {opening.summary}", f"Posting: {opening.url}",
                  f"Why it fits the candidate: {opening.reason}"]
    else:
        lines.append("No specific opening: write a general application asking about suitable roles.")
    recipient = recipient if EMAIL_RE.fullmatch(recipient or "") else ""
    if not recipient:
        flash("No recipient chosen yet. Add the address in To before sending.", ":material/info:")
    go_to("write", target_url="", target_notes="\n".join(lines), target_recipient=recipient,
          target_label=f"{opening.title} at {res.company}" if opening else f"General application, {res.company}",
          autogen=True)


# ---------------- page: job search ----------------
def page_jobsearch() -> None:
    head("Job search")
    st.title("Search jobs worldwide")
    st.caption("Searches LinkedIn job listings through Apify with your filters, then gives you an Excel sheet with "
               "each job's link, the recruiter who posted it and any HR email found. Use the sheet in Bulk send.")
    if not settings.apify_token:
        st.warning("Job search needs an Apify token. Add APIFY_API_TOKEN to .env, then restart the app.",
                   icon=":material/key:")
    chosen = ss.get("js_locations", [])
    location_options = LOCATION_PRESETS + [loc for loc in chosen if loc not in LOCATION_PRESETS]
    with st.form("jobsearch_form", border=False):
        c1, c2 = st.columns(2)
        c1.text_input("Job titles or keywords", key="js_keywords",
                      help="Separate several with commas, e.g. `Data Analyst, Python Developer`. Each is its own search.")
        c2.multiselect("Locations", location_options, key="js_locations", accept_new_options=True,
                       help="Pick from the list or type any city or country. Worldwide searches every country.")
        c3, c4, c5, c6 = st.columns(4)
        c3.selectbox("Posted", list(POSTED), key="js_posted")
        c4.multiselect("Job type", list(JOB_TYPES), key="js_types", placeholder="Any")
        c5.multiselect("Workplace", list(WORKPLACES), key="js_workplaces", placeholder="Any")
        c6.multiselect("Experience", list(EXPERIENCE), key="js_experience", placeholder="Any")
        c7, c8 = st.columns([2, 1], vertical_alignment="bottom")
        c7.slider("Results per search", 10, 100, step=5, key="js_per_search",
                  help="Each keyword × location pair is one search.")
        c8.checkbox("Find HR emails on company websites", key="js_find_emails",
                    help="Free. Visits each company's site for careers@ or hr@ addresses; adds about a minute.")
        st.caption(f"Apify charges about ${COST_PER_JOB_USD * 100:.2f} per 100 jobs from your credit; "
                   "a search usually takes 2 to 5 minutes.")
        submitted = st.form_submit_button("Search jobs", type="primary", icon=":material/search:")
    if submitted:
        search = JobSearch(keywords=parse_list(ss.get("js_keywords", "")), locations=list(ss.get("js_locations", [])),
                           posted=ss.js_posted, job_types=list(ss.get("js_types", [])),
                           workplaces=list(ss.get("js_workplaces", [])), experience=list(ss.get("js_experience", [])),
                           per_search=ss.js_per_search, find_emails=ss.js_find_emails)
        errors = search.validate()
        if not settings.apify_token:
            errors.append("APIFY_API_TOKEN is missing from .env.")
        if not show_errors(errors):
            with st.status("Searching jobs… this usually takes 2 to 5 minutes", expanded=True) as status:
                try:
                    jobs = search_jobs(settings, search, on_step=st.write)
                except Exception as exc:
                    status.update(label="Search failed", state="error")
                    st.error(friendly_error(exc), icon=":material/error:")
                else:
                    ss["jobs"], ss["jobs_summary"] = jobs, search.summary()
                    status.update(label=f"Done · {len(jobs)} jobs", state="complete", expanded=False)
                    st.toast(f"Found {len(jobs)} jobs, {sum(bool(j.email) for j in jobs)} with an HR email",
                             icon=":material/check_circle:")
    jobs = ss.get("jobs")
    if jobs is not None:
        jobsearch_results(jobs)


def job_to_row(n: int, j) -> dict:
    return {"row": n, "email": j.email, "raw_email": j.email, "other_emails": j.other_emails, "title": j.title,
            "company": j.company, "job_url": j.job_url, "location": j.location, "recruiter": j.recruiter,
            "description": j.description}


def jobsearch_results(jobs: list) -> None:
    st.divider()
    if not jobs:
        st.info("No jobs matched. Try a longer time period, fewer filters or another location.", icon=":material/info:")
        return
    with st.container(horizontal=True, wrap=True, gap="medium"):
        for label, value in (("Jobs", len(jobs)), ("With HR email", sum(bool(j.email) for j in jobs)),
                             ("With recruiter", sum(bool(j.recruiter) for j in jobs)),
                             ("Companies", len({j.company for j in jobs}))):
            st.metric(label, value, border=True, width=150)
    shown = [j for j in jobs if j.email] if st.toggle("Only jobs with an HR email", key="js_only_email") else jobs
    st.dataframe(
        [{"Posted": j.posted_date, "Job title": j.title, "Company": j.company, "Location": j.location,
          "Type": j.job_type, "HR email": j.email, "Recruiter": j.recruiter, "Job link": j.job_url,
          "Recruiter profile": j.recruiter_url} for j in shown],
        column_config={"Job link": st.column_config.LinkColumn(display_text="Open job"),
                       "Recruiter profile": st.column_config.LinkColumn(display_text="Profile")},
        hide_index=True, use_container_width=True)
    c1, c2 = st.columns(2)
    c1.download_button("Download Excel", write_jobs(shown, ss.get("jobs_summary", {})), type="primary",
                       file_name=f"mailmind_jobs_{datetime.now():%Y-%m-%d}.xlsx", icon=":material/download:",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)
    if c2.button("Email these openings in Bulk send", icon=":material/arrow_forward:", use_container_width=True):
        ss["bulk_rows"] = [job_to_row(n, j) for n, j in enumerate(shown, 2)]
        ss["bulk_source"] = f"Job search results ({len(shown)} jobs)"
        ss.pop("bulk_items", None)
        flash(f"Loaded {len(shown)} jobs into Bulk send")
        st.switch_page(PAGES["bulk"])


# ---------------- page: bulk send ----------------
def page_bulk() -> None:
    head("Bulk send")
    kb = get_kb()
    st.title("Email many openings")
    st.caption("Upload a sheet of openings: the Job search export, or your own Excel with an HR email or job-link "
               "column. MailMind drafts one email per opening from your resume, you review them, and they are sent "
               f"one at a time with a pause between emails, at most {settings.bulk_daily_limit} a day.")
    up = st.file_uploader("Openings sheet (.xlsx)", type=["xlsx"], key="bulk_file",
                          help="It looks for these columns: HR email (or Email), Job title, Company, Job link, Description.")
    if up is not None and ss.get("bulk_file_id") != up.file_id:
        ss["bulk_file_id"] = up.file_id
        rows, probs = read_openings(up.getvalue())
        if not show_errors(probs):
            ss["bulk_rows"], ss["bulk_source"] = rows, up.name
            ss.pop("bulk_items", None)
            st.toast(f"Read {len(rows)} rows from {up.name}", icon=":material/check_circle:")
    rows = ss.get("bulk_rows")
    if not rows:
        st.info("Upload a sheet, or run a Job search and choose “Email these openings in Bulk send”.",
                icon=":material/info:")
        return

    openings = plan(rows, sent_log)
    counts = Counter(o.status for o in openings)
    remaining = max(0, settings.bulk_daily_limit - sent_today(sent_log))
    st.subheader(f"1 · {ss.get('bulk_source', 'Your sheet')}")
    with st.container(horizontal=True, wrap=True, gap="medium"):
        for label, value in (("Rows", len(openings)), ("Ready", counts[READY]), ("Email to find", counts[LOOKUP]),
                             ("Skipped", len(openings) - counts[READY] - counts[LOOKUP]),
                             ("Left today", remaining)):
            st.metric(label, value, border=True, width=135)
    skipped = [o for o in openings if o.status not in (READY, LOOKUP)]
    if skipped:
        with st.expander(f"{len(skipped)} rows will be skipped"):
            st.dataframe([{"Row": o.row, "Opening": o.label, "Email": o.to, "Reason": o.status} for o in skipped],
                         hide_index=True, use_container_width=True)

    st.subheader("2 · Draft the emails")
    can_draft = counts[READY] + counts[LOOKUP]
    max_count = max(1, min(50, can_draft))
    ss["bulko_count"] = min(ss.get("bulko_count", 10), max_count)  # a smaller sheet than last time
    with st.form("bulk_form", border=False):
        c1, c2, c3 = st.columns(3)
        c1.selectbox("Tone", list(TONES), key="bulko_tone")
        c2.selectbox("Length", list(LENGTHS), key="bulko_length",
                     format_func=lambda k: f"{k} ({LENGTHS[k][0]}-{LENGTHS[k][1]} words)")
        c3.selectbox("Call to action", list(CTAS), key="bulko_cta")
        c4, c5 = st.columns(2)
        if max_count > 1:
            c4.slider("How many to draft", 1, max_count, key="bulko_count",
                      help="Each draft is one model call; about 3 a minute on Groq's free tier.")
        else:
            c4.caption("One opening to draft.")
        c5.slider("Pause between emails (seconds)", 10, 120, key="bulko_delay",
                  help="Spacing emails out keeps Gmail from flagging your account.")
        st.checkbox("Look for an email on the job page when a row only has a link", key="bulko_lookup",
                    disabled=not counts[LOOKUP])
        st.checkbox("I'll attach my resume (the emails will mention it)", key="bulko_attach")
        go = st.form_submit_button("Draft emails", type="primary", icon=":material/auto_awesome:")
    if go:
        errors = []
        if not settings.api_key:
            errors.append("GROQ_API_KEY is missing from .env.")
        if not can_draft:
            errors.append("No row has a usable email address or job link.")
        if not show_errors(errors):
            draft_bulk(openings, kb)

    items = ss.get("bulk_items")
    if items:
        review_bulk(items, kb, remaining)


def draft_bulk(openings: list, kb: KnowledgeBase) -> None:
    style = EmailStyle(tone=ss.bulko_tone, length=ss.bulko_length, cta=ss.bulko_cta, attach_resume=ss.bulko_attach)
    wanted = ss.bulko_count
    queue = [o for o in openings if o.status == READY] + ([o for o in openings if o.status == LOOKUP] if ss.bulko_lookup else [])
    llm, drafted, used, failed = LLM(settings), [], set(), []
    bar = st.progress(0.0, text=f"Drafting 0 of {wanted}…")
    for op in queue:
        if len(drafted) >= wanted:
            break
        if op.status == LOOKUP and not find_email_on_page(op):
            failed.append(f"{op.label}: {op.detail}")
            continue
        if op.to in used:
            continue
        try:
            draft(llm, kb, op, style, settings.sender_name)
        except Exception as exc:
            failed.append(f"{op.label}: {friendly_error(exc)}")
            continue
        used.add(op.to)
        drafted.append(op)
        bar.progress(len(drafted) / wanted, text=f"Drafting {len(drafted)} of {wanted}… ({op.label})")
    for f in failed[:10]:
        st.warning(f, icon=":material/warning:")
    if not drafted:
        st.error("No drafts could be written. See the messages above.", icon=":material/error:")
        return
    ss["bulk_items"] = drafted
    values = {"bulk_confirm": False}
    for i, op in enumerate(drafted):
        values |= {f"bulkd_inc_{i}": True, f"bulkd_to_{i}": op.to, f"bulkd_subj_{i}": op.subject, f"bulkd_body_{i}": op.body}
    flash(f"{len(drafted)} drafts ready for review" + (f"; {len(failed)} openings skipped" if failed else ""))
    rerun(**values)


def review_bulk(items: list, kb: KnowledgeBase, remaining: int) -> None:
    st.subheader(f"3 · Review {len(items)} drafts")
    for i, op in enumerate(items):
        with st.container(border=True):
            if op.status == "sent":
                st.markdown(f":green-badge[Sent] **{md_escape(op.label)}** to {md_escape(op.to)}")
                continue
            head_col, link_col = st.columns([4, 1], vertical_alignment="center")
            head_col.checkbox(f"**{md_escape(op.label)}**", key=f"bulkd_inc_{i}")
            if safe_link(op.job_url):
                link_col.link_button("Job", op.job_url, icon=":material/open_in_new:")
            if op.status == "failed":
                st.error(f"Last attempt failed: {op.detail}", icon=":material/error:")
            st.text_input("To", key=f"bulkd_to_{i}")
            st.text_input("Subject", key=f"bulkd_subj_{i}")
            st.text_area("Body", key=f"bulkd_body_{i}", height=200)
            sync(op, i)
            for issue in problems(op):
                st.warning(issue.capitalize(), icon=":material/warning:")

    pending_items = [(i, op) for i, op in enumerate(items) if op.status != "sent" and ss.get(f"bulkd_inc_{i}")]
    resumes = [f for f in kb.files() if f.suffix.lower() == ".pdf"]
    attachments = [resumes[0]] if resumes and ss.get("bulko_attach") else []
    if attachments:
        st.caption(f"Attaching {resumes[0].name} to every email.")
    confirm = st.checkbox(f"I've reviewed the {len(pending_items)} selected emails and want to send them", key="bulk_confirm")
    if st.button(f"Send {len(pending_items)} emails", type="primary", icon=":material/send:"):
        errors = []
        if not smtp_ready():
            errors.append("Email isn't connected. Set SMTP_USER and SMTP_PASSWORD in .env and restart the app.")
        if not pending_items:
            errors.append("Select at least one draft.")
        if len(pending_items) > remaining:
            errors.append(f"Only {remaining} more emails are allowed today (BULK_DAILY_LIMIT). Deselect some drafts.")
        bad = [f"{op.label}: {', '.join(problems(op))}" for _, op in pending_items if problems(op)]
        if bad:
            errors.append("Fix these drafts first: " + "; ".join(bad[:5]))
        if not confirm:
            errors.append("Tick the review box to confirm you want to send them.")
        if not show_errors(errors):
            send_bulk(items, pending_items, attachments)
    done = [op for op in items if op.status in ("sent", "failed")]
    if done:
        st.download_button("Download send report", write_results([{
            "status": op.status, "to": op.to, "title": op.title, "company": op.company, "job_url": op.job_url,
            "subject": op.subject, "sent_at": op.detail if op.status == "sent" else "",
            "detail": op.detail if op.status == "failed" else ""} for op in items]),
            file_name=f"mailmind_bulk_send_{datetime.now():%Y-%m-%d}.xlsx", icon=":material/download:",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def sync(op, i: int) -> None:
    """Copy the reviewed (possibly edited) widget values back into the opening."""
    op.to = ss.get(f"bulkd_to_{i}", op.to).strip().lower()
    op.subject, op.body = ss.get(f"bulkd_subj_{i}", op.subject), ss.get(f"bulkd_body_{i}", op.body)


def send_bulk(items: list, pending_items: list, attachments: list) -> None:
    delay = ss.bulko_delay
    st.button("Stop sending", icon=":material/stop_circle:", help="Emails already sent stay sent; the rest wait.")
    sent = failed = 0
    with st.status(f"Sending {len(pending_items)} emails, {delay} s apart…", expanded=True) as status:
        for k, (i, op) in enumerate(pending_items):
            try:
                message_id = send_email(settings, op.to, op.subject, op.body, attachments=attachments)
            except Exception as exc:
                op.status, op.detail = "failed", friendly_error(exc)
                failed += 1
                st.write(f":material/error: {op.to}: {op.detail}")
            else:
                sent_log.add(SentEmail(to=op.to, subject=op.subject, body=op.body, message_id=message_id,
                                       target=op.label, evidence=op.evidence))
                op.status, op.detail = "sent", bulk_now()
                sent += 1
                st.write(f":material/check_circle: Sent to {op.to} ({op.label})")
            ss["bulk_items"] = items  # progress survives a stop or reload
            if k < len(pending_items) - 1:
                time.sleep(delay)
        status.update(label=f"Sent {sent}" + (f", {failed} failed" if failed else ""),
                      state="complete" if not failed else "error", expanded=bool(failed))
    flash(f"Sent {sent} emails" + (f"; {failed} failed" if failed else "") + ". Track them in Sent & follow-ups.",
          ":material/mark_email_read:")
    rerun(bulk_confirm=False)


# ---------------- page: sent & follow-ups ----------------
STATUS_BADGE = {"awaiting": ":blue-badge[Awaiting reply]", "replied": ":green-badge[Replied]",
                "bounced": ":red-badge[Bounced]", "closed": ":gray-badge[Closed]"}


def page_sent() -> None:
    head("Sent & follow-ups")
    st.title("Sent emails & follow-ups")
    emails = sorted(sent_log.all(), key=lambda e: e.sent_at, reverse=True)
    schedule = settings.followup_days
    counts = {s: sum(e.status == s for e in emails) for s in STATUS_BADGE}
    due = [e for e in emails if (d := e.followup_due_in(schedule)) is not None and d <= 0]
    # A wrapping row: five across on desktop, two or three per line on a phone instead of a tall stack.
    with st.container(horizontal=True, wrap=True, gap="medium"):
        for label, value in (("Sent", len(emails)), ("Awaiting", counts["awaiting"]), ("Replied", counts["replied"]),
                             ("Bounced", counts["bounced"]), ("Follow-ups due", len(due))):
            st.metric(label, value, border=True, width=135)
    st.caption(f"Follow-ups are suggested {' and '.join(map(str, schedule))} days after the previous "
               "email, then the thread is left alone. Change with FOLLOWUP_DAYS in .env.")

    if st.button("Check inbox for replies & bounces", icon=":material/refresh:"):
        awaiting = [e for e in emails if e.status == "awaiting"]
        if not smtp_ready():
            st.error("Email isn't connected. Set SMTP_USER and SMTP_PASSWORD in .env and restart.", icon=":material/error:")
        elif not awaiting:
            st.info("No threads are awaiting a reply.", icon=":material/info:")
        else:
            with st.spinner("Checking Gmail…"):
                try:
                    found = check_threads(settings, [{"id": e.id, "to": e.to, "subject": e.subject,
                                                      "since": datetime.fromisoformat(e.sent_at)} for e in awaiting])
                except Exception as exc:
                    st.error(friendly_error(exc), icon=":material/error:")
                else:
                    for e in awaiting:
                        if e.id in found:
                            e.status = found[e.id]
                            sent_log.update(e)
                    replied = sum(v == "replied" for v in found.values())
                    bounced = sum(v == "bounced" for v in found.values())
                    flash(f"Checked {len(awaiting)} thread{'s' * (len(awaiting) != 1)}: {replied} replied, "
                          f"{bounced} bounced", ":material/mark_email_read:")
                    st.rerun()

    if not emails:
        st.info("Emails you send from Write email show up here.", icon=":material/info:")
        st.page_link(PAGES["write"], label="Write your first email", icon=":material/edit_note:")
    for e in emails:
        render_thread(e, schedule)


def render_thread(e: SentEmail, schedule: list[int]) -> None:
    due_in = e.followup_due_in(schedule)
    with st.container(border=True):
        top = f"**{md_escape(e.to)}** — {md_escape(e.subject)}  {STATUS_BADGE[e.status]}"
        if due_in is not None:
            top += " :orange-badge[Follow-up due]" if due_in <= 0 else f" :gray-badge[Follow-up in {due_in}d]"
        st.markdown(top)
        st.caption(f"{md_escape(e.target) or '—'} · sent {days_since(e.sent_at)} days ago · "
                   f"{len(e.followups)}/{len(schedule)} follow-ups")
        with st.expander("Show thread"):
            st.markdown(f"**Original** · {e.sent_at[:10]}")
            st.text(e.body)
            for i, f in enumerate(e.followups, 1):
                st.markdown(f"**Follow-up {i}** · {f['sent_at'][:10]}")
                st.text(f["body"])

        key = f"fu_body_{e.id}"
        if due_in is not None:
            with st.form(f"fu_form_{e.id}", border=False):
                st.text_input("Instructions for the follow-up (optional)", key=f"fu_extra_{e.id}",
                              help="For example: mention I finished a new Power BI project.")
                draft_clicked = st.form_submit_button(f"Draft follow-up #{len(e.followups) + 1}",
                                                      icon=":material/auto_awesome:")
            if draft_clicked:
                with st.spinner("Writing follow-up…"):
                    try:
                        body = generate_followup(LLM(settings), e, settings.sender_name, schedule,
                                                 ss.get(f"fu_extra_{e.id}", ""))
                    except Exception as exc:
                        st.error(friendly_error(exc), icon=":material/error:")
                    else:
                        flash("Follow-up drafted. Review it before sending.")
                        rerun(**{key: body})
        if ss.get(key) and e.status == "awaiting":
            st.text_input("Subject", value=reply_subject(e.subject), disabled=True, key=f"fu_subj_{e.id}")
            body = st.text_area("Follow-up", key=key, height=160)
            if due_in is not None and due_in > 0:
                st.caption(f"Recommended to wait {due_in} more day(s) before sending.")
            ok = st.checkbox("I've reviewed this follow-up", key=f"fu_ok_{e.id}")
            if st.button("Send follow-up", key=f"fu_send_{e.id}", type="primary", icon=":material/send:"):
                placeholders = find_placeholders(body)
                errors = []
                if not smtp_ready():
                    errors.append("Email isn't connected. Set SMTP_USER and SMTP_PASSWORD in .env and restart.")
                if not body.strip():
                    errors.append("The follow-up is empty.")
                if placeholders:
                    errors.append("Replace the placeholders first: " + ", ".join(placeholders))
                if not ok:
                    errors.append("Tick “I've reviewed this follow-up” to confirm.")
                if not show_errors(errors):
                    try:
                        message_id = send_email(settings, e.to, reply_subject(e.subject), body,
                                                in_reply_to=e.last_message_id, references=e.references)
                    except Exception as exc:
                        st.error(friendly_error(exc), icon=":material/error:")
                    else:
                        e.followups.append({"body": body, "message_id": message_id,
                                            "sent_at": datetime.now().isoformat(timespec="seconds")})
                        sent_log.update(e)
                        flash(f"Follow-up sent to {e.to}", ":material/mark_email_read:")
                        rerun(**{key: "", f"fu_ok_{e.id}": False})

        b = st.columns(4)
        if e.status != "replied" and b[0].button("Mark replied", key=f"rep_{e.id}", icon=":material/done_all:"):
            update_status(e, "replied", f"Marked {e.to} as replied")
        if e.status == "awaiting" and b[1].button("Stop following up", key=f"close_{e.id}", icon=":material/block:"):
            update_status(e, "closed", f"Stopped following up with {e.to}")
        if e.status != "awaiting" and b[2].button("Reopen", key=f"open_{e.id}", icon=":material/undo:"):
            update_status(e, "awaiting", f"Reopened the thread with {e.to}")
        with b[3].popover("Delete", icon=":material/delete:"):
            st.write("Remove this thread from the log? This can't be undone.")
            if st.button("Yes, delete", key=f"del_{e.id}", type="primary"):
                sent_log.delete(e.id)
                flash(f"Deleted the thread with {e.to}", ":material/delete:")
                st.rerun()


def update_status(e: SentEmail, status: str, message: str) -> None:
    e.status = status
    sent_log.update(e)
    flash(message)
    st.rerun()


# ---------------- page: not found ----------------
def page_not_found() -> None:
    head("Page not found")
    st.title("Page not found")
    st.write("The page you asked for doesn't exist. It may have been moved, or the link is mistyped.")
    # Streamlit also opens its own "Running the app's main page" dialog; this page replaces it.
    st.html("""<script>
      const close = () => document.querySelectorAll('[role="dialog"]').forEach(d => {
        if (d.innerText.includes("does not seem to exist")) d.querySelector('button[aria-label="Close"]')?.click();
      });
      [0, 200, 600, 1500].forEach(ms => setTimeout(close, ms));
    </script>""", unsafe_allow_javascript=True)
    st.page_link(PAGES["write"], label="Go to Write email", icon=":material/edit_note:")
    st.page_link(PAGES["scout"], label="Go to Find openings", icon=":material/travel_explore:")
    st.page_link(PAGES["jobs"], label="Go to Job search", icon=":material/work:")
    st.page_link(PAGES["bulk"], label="Go to Bulk send", icon=":material/outgoing_mail:")
    st.page_link(PAGES["sent"], label="Go to Sent & follow-ups", icon=":material/forward_to_inbox:")


# ---------------- footer ----------------
def footer() -> None:
    st.divider()
    c1, c2, c3 = st.columns([2, 1, 1])
    c1.markdown(f"**{APP_NAME}**  \n<span class='cma-footer'>{DESCRIPTION}</span>", unsafe_allow_html=True)
    with c2:
        st.markdown("**Pages**")
        st.page_link(PAGES["write"], label="Write email")
        st.page_link(PAGES["scout"], label="Find openings")
        st.page_link(PAGES["jobs"], label="Job search")
        st.page_link(PAGES["bulk"], label="Bulk send")
        st.page_link(PAGES["sent"], label="Sent & follow-ups")
    with c3:
        st.markdown("**Setup help**")
        for label, url in HELP_LINKS.items():
            st.page_link(url, label=label, icon=":material/open_in_new:")


# ---------------- navigation ----------------
PAGES = {
    "write": st.Page(page_write, title="Write email", icon=":material/edit_note:", url_path="write", default=True),
    "scout": st.Page(page_scout, title="Find openings", icon=":material/travel_explore:", url_path="find-openings"),
    "jobs": st.Page(page_jobsearch, title="Job search", icon=":material/work:", url_path="job-search"),
    "bulk": st.Page(page_bulk, title="Bulk send", icon=":material/outgoing_mail:", url_path="bulk-send"),
    "sent": st.Page(page_sent, title="Sent & follow-ups", icon=":material/forward_to_inbox:", url_path="sent"),
    "404": st.Page(page_not_found, title="Page not found", url_path="not-found", visibility="hidden"),
}
nav = st.navigation(list(PAGES.values()), position="top")

if home := home_url():
    st.logo(str(ASSETS / "logo.png"), link=home, icon_image=str(ASSETS / "favicon.png"), size="large")
sidebar(get_kb())
for message, icon in ss.pop("flash", []):
    st.toast(message, icon=icon)

# An unknown address (e.g. /jobs) shows the custom 404 page instead of silently opening the home page.
# st.context.url keeps the address the session was opened with, so it is checked on the first run
# only; the next click (any link on the 404 page) navigates normally.
first_run = not ss.get("url_checked")
ss["url_checked"] = True
requested = urlparse(getattr(st.context, "url", "") or "").path.strip("/").split("/")[-1]
if first_run and nav is PAGES["write"] and requested not in {""} | {p.url_path for p in PAGES.values()}:
    page_not_found()
else:
    nav.run()
footer()
