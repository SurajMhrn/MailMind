import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import followup  # noqa: E402
from agent.config import Settings  # noqa: E402


@pytest.fixture
def settings():
    return Settings(provider="groq", base_url="https://example.invalid", api_key="test-key", model="qwen-test",
                    embed_base_url="", embed_api_key="", embed_model="", smtp_host="smtp.test", smtp_port=587,
                    smtp_user="me@gmail.com", smtp_password="app-password", sender_name="Suraj Maharana",
                    imap_host="imap.test", apify_token="apify-test", followup_days=[4, 7])


@pytest.fixture
def sent_log(tmp_path, monkeypatch):
    """Every test gets its own empty sent log; the real data/sent.json is never touched."""
    monkeypatch.setattr(followup, "LOG_PATH", tmp_path / "sent.json")
    return followup.SentLog()


class FakeLLMClient:
    """Stands in for the OpenAI client: returns scripted messages in order and records the calls."""

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return NS(usage=NS(total_tokens=100), choices=[NS(message=self.replies.pop(0))])


def reply(content=None, tool_calls=None):
    return NS(content=content, tool_calls=tool_calls)


def json_reply(**data):
    return reply(json.dumps(data))
