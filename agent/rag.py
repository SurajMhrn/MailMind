"""Knowledge base: load documents, chunk them, and retrieve relevant chunks.

Retrieval uses BM25 keyword scoring by default, so no embedding model is
needed. If EMBED_* settings are configured, it uses cosine similarity over
embeddings from that OpenAI-compatible endpoint instead (cached on disk).
"""
import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from openai import OpenAI
from pypdf import PdfReader

from .config import ROOT, Settings

KNOWLEDGE_DIR = ROOT / "knowledge"
CACHE_DIR = ROOT / "data"
SUPPORTED = {".md", ".txt", ".pdf"}

_TOKEN = re.compile(r"[a-z0-9+#.]+")
_STOPWORDS = set(
    "a an and are as at be by for from has have in is it its of on or that the this to was were "
    "will with we our you your i my me they their them".split()
)


@dataclass
class Chunk:
    source: str
    text: str


def read_document(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    return path.read_text(encoding="utf-8", errors="ignore")


def chunk_text(text: str, size: int = 800, overlap: int = 150) -> list[str]:
    """Split on paragraphs, packing them into chunks of roughly `size` characters.

    A Markdown heading always starts a new chunk, so each section (a service,
    a case study, a job) is retrieved on its own.
    """
    text = re.sub(r"\n(#{1,6} )", r"\n\n\1", text)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, current = [], ""
    for para in paragraphs:
        if current and para.startswith("#"):
            chunks.append(current)
            current = para
        elif current and len(current) + len(para) > size:
            chunks.append(current)
            current = current[-overlap:] + "\n\n" + para
        else:
            current = f"{current}\n\n{para}" if current else para
        while len(current) > size * 2:  # a single huge paragraph
            chunks.append(current[:size])
            current = current[size - overlap:]
    if current:
        chunks.append(current)
    return chunks


def tokenize(text: str) -> list[str]:
    return [t.strip(".") for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS]


class KnowledgeBase:
    def __init__(self, collection: str, settings: Settings):
        self.collection = collection
        self.settings = settings
        self.dir = KNOWLEDGE_DIR / collection
        self.dir.mkdir(parents=True, exist_ok=True)
        self.chunks: list[Chunk] = []
        self._embeddings: np.ndarray | None = None
        self._build()

    def files(self) -> list[Path]:
        return sorted(p for p in self.dir.iterdir() if p.suffix.lower() in SUPPORTED)

    def _build(self) -> None:
        # A corrupt or encrypted file is skipped (and reported) instead of breaking the whole index.
        self.chunks, self.errors = [], {}
        for path in self.files():
            try:
                self.chunks += [Chunk(path.name, piece) for piece in chunk_text(read_document(path))]
            except Exception as exc:
                self.errors[path.name] = f"{type(exc).__name__}: {exc}"
        self._tokens = [tokenize(c.text) for c in self.chunks]
        self._df = Counter(t for toks in self._tokens for t in set(toks))
        self._avg_len = sum(map(len, self._tokens)) / max(len(self._tokens), 1)
        if self.settings.use_embeddings and self.chunks:
            self._embeddings = self._embed_cached([c.text for c in self.chunks])

    # ---- embeddings (optional) ----
    def _embed(self, texts: list[str]) -> np.ndarray:
        client = OpenAI(base_url=self.settings.embed_base_url, api_key=self.settings.embed_api_key or "none")
        resp = client.embeddings.create(model=self.settings.embed_model, input=texts)
        vecs = np.array([d.embedding for d in resp.data], dtype=np.float32)
        return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)

    def _embed_cached(self, texts: list[str]) -> np.ndarray:
        key = hashlib.sha256(json.dumps([self.settings.embed_model, texts]).encode()).hexdigest()[:16]
        path = CACHE_DIR / f"{self.collection}-{key}.npy"
        if path.exists():
            return np.load(path)
        CACHE_DIR.mkdir(exist_ok=True)
        vecs = np.vstack([self._embed(texts[i:i + 64]) for i in range(0, len(texts), 64)])
        np.save(path, vecs)
        return vecs

    # ---- retrieval ----
    def _bm25_scores(self, query: str, k1: float = 1.5, b: float = 0.75) -> list[float]:
        n = len(self.chunks)
        q = tokenize(query)
        scores = []
        for toks in self._tokens:
            tf = Counter(toks)
            score = 0.0
            for term in q:
                if term not in tf:
                    continue
                idf = math.log(1 + (n - self._df[term] + 0.5) / (self._df[term] + 0.5))
                score += idf * tf[term] * (k1 + 1) / (tf[term] + k1 * (1 - b + b * len(toks) / self._avg_len))
            scores.append(score)
        return scores

    def search(self, query: str, k: int = 4) -> list[Chunk]:
        if not self.chunks:
            return []
        if self._embeddings is not None:
            scores = (self._embeddings @ self._embed([query])[0]).tolist()
        else:
            scores = self._bm25_scores(query)
        ranked = sorted(range(len(scores)), key=scores.__getitem__, reverse=True)
        return [self.chunks[i] for i in ranked[:k] if scores[i] > 0]

    def add_file(self, name: str, data: bytes) -> int:
        """Save a document and re-index. Returns how many chunks it produced (0 = no readable text)."""
        name = Path(name).name
        (self.dir / name).write_bytes(data)
        self._build()
        return sum(c.source == name for c in self.chunks)

    def remove_file(self, name: str) -> None:
        (self.dir / Path(name).name).unlink(missing_ok=True)
        self._build()
