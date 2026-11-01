"""Thin wrapper over the hosted Qwen model (any OpenAI-compatible provider)."""
import json
import re

from openai import OpenAI

from .config import Settings


def strip_think(text: str) -> str:
    """Qwen3 may emit <think>...</think> reasoning before the answer."""
    return re.sub(r"<think>.*?(</think>|$)", "", text or "", flags=re.S).strip()


def parse_json(text: str):
    """Pull the first JSON object or array out of a model reply."""
    text = strip_think(text)
    match = re.search(r"[\[{].*[\]}]", text, re.S)
    if not match:
        raise ValueError(f"Model did not return JSON:\n{text[:500]}")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        # Raw newlines inside strings are a common small-model mistake.
        return json.loads(match.group(0).replace("\r", "").replace("\n", "\\n"))


class LLM:
    def __init__(self, settings: Settings):
        if not settings.api_key:
            raise RuntimeError(f"No API key set for provider '{settings.provider}'. Add it to .env.")
        self.model = settings.model
        # Free tiers have tight per-minute token limits; on a 429 the SDK waits
        # for the server's retry-after and tries again instead of failing.
        self.client = OpenAI(base_url=settings.base_url, api_key=settings.api_key, max_retries=6)
        self.tokens = 0

    def chat(self, messages: list[dict], tools: list | None = None, temperature: float = 0.4):
        resp = self.client.chat.completions.create(
            model=self.model, messages=messages, tools=tools, temperature=temperature,
        )
        if resp.usage:
            self.tokens += resp.usage.total_tokens
        return resp.choices[0].message

    def json(self, prompt: str, temperature: float = 0.4):
        return parse_json(self.chat([{"role": "user", "content": prompt}], temperature=temperature).content)
