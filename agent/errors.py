"""Turn exceptions from Groq, Apify, Gmail and the web into messages a user can act on."""
import json
import smtplib
import socket

import openai
import requests


def friendly_error(exc: Exception) -> str:
    """One sentence saying what went wrong and what to do about it."""
    if isinstance(exc, openai.AuthenticationError):
        return "Groq rejected the API key. Check GROQ_API_KEY in .env, then restart the app."
    if isinstance(exc, openai.RateLimitError):
        return "Groq's free-tier limit was reached. Wait about a minute and try again."
    if isinstance(exc, openai.NotFoundError):
        return ("The configured model is no longer available on Groq. Set LLM_MODEL in .env to a current "
                "Qwen model (see console.groq.com/docs/models) and restart.")
    if isinstance(exc, openai.APIConnectionError):
        return "Couldn't reach Groq. Check your internet connection and try again."
    if isinstance(exc, openai.APIStatusError):
        return f"Groq returned an error ({exc.status_code}). Try again in a moment."

    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        code, url = exc.response.status_code, exc.response.url
        if "api.apify.com" in url:
            if code == 401:
                return "Apify rejected the token. Check APIFY_API_TOKEN in .env, then restart the app."
            if code == 402:
                return "Your Apify credit for this month is used up. Add credit or wait for the monthly reset."
            return f"Apify returned an error ({code}). Try again in a moment."
        if code in (401, 403, 429):
            return (f"That website blocks automated access ({code}). Paste the job description into the "
                    "text box instead.")
        if code == 404:
            return "That page doesn't exist (404). Check the URL."
        return f"That website returned an error ({code}). Check the URL or paste the text instead."
    if isinstance(exc, (requests.ConnectionError, socket.gaierror)):
        return "Couldn't connect to that website. Check the URL and your internet connection."
    if isinstance(exc, requests.Timeout):
        return "The website took too long to respond. Try again, or paste the text instead."
    if isinstance(exc, requests.exceptions.MissingSchema | requests.exceptions.InvalidURL):
        return "That isn't a valid web address. Use a full URL such as `https://company.com/careers`."

    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "Gmail refused the recipient address. Check it for typos."
    if isinstance(exc, smtplib.SMTPException | socket.timeout | ConnectionError):
        return f"Gmail couldn't send the email ({exc}). Check your connection and try again."

    if isinstance(exc, json.JSONDecodeError) or (isinstance(exc, ValueError) and "JSON" in str(exc)):
        return "The model returned an unexpected format. Please try again."
    # Our own errors (missing settings, invalid address, login help) are already written for people.
    if isinstance(exc, (RuntimeError, ValueError)):
        return str(exc)
    return f"Something went wrong ({type(exc).__name__}: {exc})."
