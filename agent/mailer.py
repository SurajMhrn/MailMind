"""Send email over SMTP and check for replies over IMAP (e.g. Gmail with an App Password)."""
import imaplib
import mimetypes
import smtplib
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

from .config import Settings
from .scraper import EMAIL_RE

_AUTH_HELP = ("Login rejected. For Gmail, SMTP_PASSWORD must be a 16-character App Password "
              "(not your normal password), and 2-Step Verification must be on.")


def send_email(settings: Settings, to: str, subject: str, body: str, *,
               in_reply_to: str = "", references: str = "", attachments: list[Path] = ()) -> str:
    """Send one email and return its Message-ID (used to thread follow-ups)."""
    if not (settings.smtp_user and settings.smtp_password):
        raise RuntimeError("SMTP_USER and SMTP_PASSWORD must be set in .env to send email.")
    if not EMAIL_RE.fullmatch(to.strip()):
        raise ValueError(f"'{to}' is not a valid email address.")
    msg = EmailMessage()
    msg["From"] = formataddr((settings.sender_name, settings.smtp_user)) if settings.sender_name else settings.smtp_user
    msg["To"] = to.strip()
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=settings.smtp_user.split("@")[-1])
    if in_reply_to:  # makes the follow-up appear in the same thread for the recipient
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = f"{references} {in_reply_to}".strip()
    msg.set_content(body)
    for path in attachments:
        ctype, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        smtp.starttls()
        try:
            smtp.login(settings.smtp_user, settings.smtp_password)
        except smtplib.SMTPAuthenticationError as exc:
            raise RuntimeError(_AUTH_HELP) from exc
        smtp.send_message(msg)
    return msg["Message-ID"]


def _imap_quote(text: str) -> str:
    return text.encode("ascii", "ignore").decode().replace('"', "").replace("\\", "")[:60]


def check_threads(settings: Settings, threads: list[dict]) -> dict[str, str]:
    """Look in the inbox for replies and bounces. Returns {thread id: "replied" | "bounced"}.

    Each thread is {"id", "to", "subject", "since": datetime}. A reply is any inbox
    message from the recipient, or with the same subject, received after we sent.
    A bounce is a mailer-daemon notice that mentions the recipient's address.
    """
    found: dict[str, str] = {}
    if not threads:
        return found
    imap = imaplib.IMAP4_SSL(settings.imap_host)
    try:
        try:
            imap.login(settings.smtp_user, settings.smtp_password)
        except imaplib.IMAP4.error as exc:
            raise RuntimeError(_AUTH_HELP) from exc
        imap.select("INBOX", readonly=True)

        def hits(criteria: str) -> bool:
            status, data = imap.search(None, criteria)
            return status == "OK" and bool(data and data[0].split())

        for t in threads:
            since = t["since"].strftime("%d-%b-%Y")
            to, subject = _imap_quote(t["to"]), _imap_quote(t["subject"])
            if hits(f'(SINCE {since} FROM "mailer-daemon" BODY "{to}")'):
                found[t["id"]] = "bounced"
            elif hits(f'(SINCE {since} OR FROM "{to}" SUBJECT "{subject}")'):
                found[t["id"]] = "replied"
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return found


def days_since(iso: str) -> int:
    return (datetime.now() - datetime.fromisoformat(iso)).days
