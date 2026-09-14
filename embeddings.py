"""
The shared embedder and the text utilities around it.

Stored vectors and query vectors MUST come from the same model export, so the
ingest job, the inline embed-on-write path and the retriever all import
`embed_texts` from here - they cannot drift onto different models.

Model: BAAI/bge-small-en-v1.5 (384 dims) through fastembed/ONNX. Same width as
the MiniLM model the weather app used, so the VECTOR(384) column carries over,
but BGE is trained for retrieval and reads up to 512 tokens instead of 256 -
which matters here, where a chunk is a paragraph of a paper rather than a
two-sentence forecast.
"""

from __future__ import annotations

import hashlib
import logging
import os
from functools import lru_cache
from typing import Sequence

logger = logging.getLogger(__name__)

EMBED_MODEL = os.environ.get("COPILOT_EMBED_MODEL", "BAAI/bge-small-en-v1.5")

MODEL_DIMS = {
    "BAAI/bge-small-en-v1.5": 384,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "BAAI/bge-base-en-v1.5": 768,
}

# Databricks Apps only guarantee /tmp is writable.
os.environ.setdefault("FASTEMBED_CACHE_PATH", "/tmp/.cache/fastembed")

# Character-based chunking. ~1000 characters is ~220 tokens of English prose:
# inside BGE's 512-token window with room for the title prepended to every
# chunk. 150 characters of overlap keeps a sentence split across a boundary
# retrievable from either side. These are starting values - scripts and
# sql/03 report chunk counts so they can be re-tuned against the real corpus.
CHUNK_SIZE = int(os.environ.get("COPILOT_CHUNK_SIZE", "1000"))
CHUNK_OVERLAP = int(os.environ.get("COPILOT_CHUNK_OVERLAP", "150"))

# Look this far back from a window's end for a sentence or word boundary, so a
# chunk does not end mid-word.
_BOUNDARY_SLACK = 200


@lru_cache(maxsize=1)
def _embedder():
    """Load the ONNX model once, on first use rather than at import."""
    from fastembed import TextEmbedding

    logger.info("loading embedding model %s", EMBED_MODEL)
    return TextEmbedding(EMBED_MODEL)


def embed_texts(texts: Sequence[str]) -> list[list[float]]:
    """Embed a batch of passages."""
    if not texts:
        return []
    return [vector.tolist() for vector in _embedder().embed(list(texts))]


def embed_query(text: str) -> list[float]:
    """Embed a retrieval query into the same space as the stored passages."""
    return embed_texts([text])[0]


def expected_dims(model_name: str = EMBED_MODEL) -> int | None:
    return MODEL_DIMS.get(model_name)


def to_vector_literal(values: Sequence[float]) -> str:
    """pgvector's text form, '[0.1,0.2,...]', paired with an explicit ::vector cast."""
    return "[" + ",".join(f"{float(v):.7g}" for v in values) + "]"


def text_hash(text: str | None) -> str | None:
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_id(source_type: str, source_key: str | int, index: int) -> str:
    """Stable chunk primary key, derived from position so re-embeds collide."""
    return hashlib.sha256(f"{source_type}:{source_key}:{index}".encode()).hexdigest()[:32]


def chunk_text(text: str | None, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping windows that end on a boundary where possible.

    Paragraph breaks are preserved as single newlines (section heads from the
    full text stay on their own line); other whitespace is collapsed.
    """
    lines = [" ".join(line.split()) for line in (text or "").splitlines()]
    text = "\n".join(line for line in lines if line)
    if not text:
        return []
    if size <= overlap:
        raise ValueError(f"chunk size ({size}) must exceed overlap ({overlap})")

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            window = text[max(start, end - _BOUNDARY_SLACK):end]
            # Prefer a sentence end, then a line break, then any space.
            for marker in (". ", "\n", " "):
                cut = window.rfind(marker)
                if cut != -1:
                    end = end - len(window) + cut + len(marker)
                    break
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        next_start = end - overlap
        # Step the overlap start forward to a word boundary so the next chunk
        # does not open mid-word - but never past `end`, which would drop text.
        if next_start > 0 and not text[next_start - 1].isspace():
            space = min((i for i in (text.find(" ", next_start), text.find("\n", next_start))
                         if i != -1), default=-1)
            if space != -1 and space < end:
                next_start = space + 1
        # Guarantee forward progress even if the boundary search pulled `end`
        # back inside the overlap.
        start = max(next_start, start + 1)
    return chunks


def passage_for_embedding(title: str | None, chunk: str) -> str:
    """The string that actually becomes the vector for a paper chunk.

    Prepending the title makes a mid-paper paragraph ("we fine-tune on 12k
    examples...") findable by the topic a learner types, which the paragraph
    alone rarely names. chunk_text is stored without it.
    """
    return f"{title}\n{chunk}" if title else chunk
