"""Offline tests for the logic that does not need Lakebase, OpenAlex or Claude.

    .venv/bin/python -m pytest

The database paths are covered by test_deployment.py against a running app.
"""

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("LAKEBASE_URL", "postgresql://unused:unused@localhost:5432/unused")

import embeddings  # noqa: E402
import reading_plan  # noqa: E402
import retrieval  # noqa: E402
from openalex_client import (  # noqa: E402
    OpenAlexError,
    normalize_work,
    normalize_work_id,
    reconstruct_abstract,
    tei_to_text,
)

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "openalex_work_W4389984066.json")


# ---------------------------------------------------------------------------
# OpenAlex normalization
# ---------------------------------------------------------------------------


def test_reconstruct_abstract_orders_repeated_words():
    index = {"the": [0, 3], "cat": [1], "saw": [2], "dog": [4]}
    assert reconstruct_abstract(index) == "the cat saw the dog"
    assert reconstruct_abstract(None) is None
    assert reconstruct_abstract({}) is None


def test_normalize_live_work_fixture():
    raw = json.load(open(FIXTURE))
    work = normalize_work(raw)
    paper = work["paper"]
    assert paper["id"] == "W4389984066"
    assert paper["title"].startswith("Retrieval-Augmented Generation")
    assert paper["abstract"] and paper["abstract"].split()[0] == "Large"
    assert paper["has_content"] is True
    assert all(r.startswith("W") for r in paper["referenced_works"])
    assert "abstract_inverted_index" not in paper["payload"]
    # Every authorship points at an author row, positions are the byline order.
    author_ids = {a["id"] for a in work["authors"]}
    assert {l["author_id"] for l in work["authorships"]} == author_ids
    assert work["authorships"][0]["position_index"] == 0


def test_normalize_skips_duplicate_authors_on_one_byline():
    raw = {"id": "https://openalex.org/W1", "display_name": "T", "authorships": [
        {"author": {"id": "https://openalex.org/A1", "display_name": "X"}, "institutions": []},
        {"author": {"id": "https://openalex.org/A1", "display_name": "X"}, "institutions": []},
    ]}
    assert len(normalize_work(raw)["authorships"]) == 1


@pytest.mark.parametrize("value,expected", [
    ("W4389984066", "W4389984066"),
    ("https://openalex.org/W4389984066", "W4389984066"),
    ("w123", "W123"),
])
def test_normalize_work_id_accepts(value, expected):
    assert normalize_work_id(value) == expected


@pytest.mark.parametrize("value", ["A123", "10.1000/xyz", "", "W12x"])
def test_normalize_work_id_rejects(value):
    with pytest.raises(OpenAlexError):
        normalize_work_id(value)


def test_tei_to_text_keeps_heads_and_paragraphs_skips_bibliography():
    xml = """<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body>
      <div><head>1 Introduction</head><p>First <ref>[1]</ref> paragraph.</p></div>
      <div><head>2 Method</head><p>Second   paragraph.</p></div>
    </body><back><div><listBibl><biblStruct>Should not appear</biblStruct></listBibl></div></back>
    </text></TEI>"""
    text, truncated = tei_to_text(xml)
    assert text == "1 Introduction\nFirst [1] paragraph.\n2 Method\nSecond paragraph."
    assert not truncated
    short, truncated = tei_to_text(xml, max_chars=10)
    assert len(short) == 10 and truncated


# ---------------------------------------------------------------------------
# chunking
# ---------------------------------------------------------------------------


def test_chunks_cover_text_with_overlap_and_word_boundaries():
    text = " ".join(f"word{i}." for i in range(600))
    chunks = embeddings.chunk_text(text, size=300, overlap=50)
    assert len(chunks) > 1
    assert all(len(c) <= 300 for c in chunks)
    # No chunk starts or ends mid-token.
    assert all(c.split()[0].startswith("word") and c.endswith(".") for c in chunks)
    # Nothing lost: every token appears in some chunk.
    joined = " ".join(chunks)
    assert all(f"word{i}." in joined for i in range(600))


def test_chunk_text_edge_cases():
    assert embeddings.chunk_text("") == []
    assert embeddings.chunk_text(None) == []
    assert embeddings.chunk_text("short text") == ["short text"]
    with pytest.raises(ValueError):
        embeddings.chunk_text("abc", size=10, overlap=10)
    # An unbreakable run still makes progress instead of looping.
    assert len(embeddings.chunk_text("x" * 2500, size=1000, overlap=150)) == 3


def test_chunk_ids_are_stable_and_distinct():
    assert embeddings.chunk_id("abstract", "W1", 0) == embeddings.chunk_id("abstract", "W1", 0)
    assert embeddings.chunk_id("abstract", "W1", 0) != embeddings.chunk_id("content", "W1", 0)


def test_vector_literal():
    assert embeddings.to_vector_literal([0.5, 1, -0.25]) == "[0.5,1,-0.25]"


# ---------------------------------------------------------------------------
# reading plan
# ---------------------------------------------------------------------------


def _paper(pid, year, refs=(), cites=0, title=None, work_type="article"):
    return {"id": pid, "title": title or f"Paper {pid}", "publication_year": year,
            "cited_by_count": cites, "referenced_works": list(refs), "work_type": work_type}


def test_plan_respects_prerequisites_and_stages():
    papers = [
        _paper("W3", 2023, refs=["W1", "W2"]),             # frontier: builds on both, cited by none
        _paper("W2", 2019, refs=["W1"], cites=500),          # core: builds on W1, cited by W3
        _paper("W1", 2017, cites=90000),                     # foundations
        _paper("W9", 2022, refs=["W1", "W2"], title="A Survey of Things", work_type="review"),
    ]
    plan = reading_plan.build_plan(papers, level="beginner")
    order = [i.paper_id for i in plan]
    assert order[0] == "W9"                                  # survey first for beginners
    assert order.index("W1") < order.index("W2") < order.index("W3")
    stages = [i.stage for i in plan]
    assert stages == sorted(stages, key=reading_plan.STAGES.index)  # stages never interleave
    by_id = {i.paper_id: i for i in plan}
    assert by_id["W1"].stage == "foundations"
    assert by_id["W3"].stage == "frontier"
    assert by_id["W3"].prerequisites == ["W1", "W2"]
    assert "#" in by_id["W3"].rationale


def test_advanced_level_treats_surveys_as_ordinary_papers():
    papers = [_paper("W1", 2017), _paper("W9", 2022, refs=["W1"], title="A survey", work_type="review")]
    plan = reading_plan.build_plan(papers, level="advanced")
    assert [i.paper_id for i in plan] == ["W1", "W9"]


def test_plan_breaks_citation_cycles():
    papers = [_paper("W1", 2020, refs=["W2"]), _paper("W2", 2020, refs=["W1"])]
    plan = reading_plan.build_plan(papers)
    assert len(plan) == 2
    assert any("cycle" in i.rationale for i in plan)


def test_plan_single_year_has_no_frontier_and_empty_input():
    papers = [_paper("W1", 2024), _paper("W2", 2024)]
    assert {i.stage for i in reading_plan.build_plan(papers)} == {"core"}
    assert reading_plan.build_plan([]) == []
    with pytest.raises(ValueError):
        reading_plan.build_plan(papers, level="expert")


def test_recommend_next_prefers_in_progress_then_unblocked():
    plan = [
        {"paper_id": "W1", "position": 1, "status": "completed", "prerequisites": []},
        {"paper_id": "W2", "position": 2, "status": "planned", "prerequisites": ["W1"]},
        {"paper_id": "W3", "position": 3, "status": "reading", "prerequisites": []},
    ]
    assert reading_plan.recommend_next(plan)["next"]["paper_id"] == "W3"
    plan[2]["status"] = "completed"
    rec = reading_plan.recommend_next(plan)
    assert rec["next"]["paper_id"] == "W2" and rec["completed"] == 2 and rec["total"] == 3
    plan[1]["status"] = "skipped"
    assert reading_plan.recommend_next(plan)["next"] is None


def test_recommend_next_reports_what_blocks():
    plan = [
        {"paper_id": "W2", "position": 1, "status": "planned", "prerequisites": ["W7"]},
    ]
    rec = reading_plan.recommend_next(plan)
    assert rec["next"]["paper_id"] == "W2" and rec["waiting_on"] == ["W7"]


# ---------------------------------------------------------------------------
# retrieval diversification + citations
# ---------------------------------------------------------------------------


def test_diversify_caps_chunks_per_paper_and_labels_keys():
    rows = [
        {"paper_id": "W1", "note_id": None, "source_type": "content", "similarity": 0.9},
        {"paper_id": "W1", "note_id": None, "source_type": "content", "similarity": 0.89},
        {"paper_id": "W1", "note_id": None, "source_type": "abstract", "similarity": 0.88},
        {"paper_id": "W2", "note_id": None, "source_type": "abstract", "similarity": 0.7},
        {"paper_id": None, "note_id": 12, "source_type": "note", "similarity": 0.6},
    ]
    kept = retrieval.diversify(rows, top_k=10, per_paper=2)
    assert [r["citation_key"] for r in kept] == ["W1", "W1", "W2", "N12"]
    assert len(retrieval.diversify(rows, top_k=2, per_paper=2)) == 2


def test_validate_citations_separates_unverified_keys():
    import agent

    citable = {"W1": {"key": "W1", "title": "One"}, "N4": {"key": "N4", "kind": "note"}}
    answer = "Transformers [W1] replaced RNNs [W1], per your note [N4] and [W999]."
    citations, unverified = agent.validate_citations(answer, citable)
    assert [c["key"] for c in citations] == ["W1", "N4"]
    assert unverified == ["W999"]


def test_history_is_text_only_and_starts_with_user():
    import agent

    history = [{"role": "assistant", "content": "hi"}, {"role": "user", "content": "q"},
               {"role": "assistant", "content": [{"type": "tool_use"}]}, {"role": "system", "content": "x"}]
    assert agent._history_messages(history) == [{"role": "user", "content": "q"}]


@pytest.mark.parametrize("title,expected", [
    ("Understand retrieval augmented generation", "retrieval augmented generation"),
    ("I want to learn the basics of graph neural networks", "graph neural networks"),
    ("Diffusion models", "diffusion models"),
    ("How to", "How to"),  # nothing but filler: fall back to the title rather than an empty query
])
def test_query_from_goal_strips_intent_words(title, expected):
    import library

    assert library.query_from_goal(title) == expected


def test_tool_schemas_match_handlers():
    import agent_tools

    names = [t["name"] for t in agent_tools.TOOL_DEFINITIONS]
    assert len(names) == len(set(names))
    for tool in agent_tools.TOOL_DEFINITIONS:
        assert tool["input_schema"]["type"] == "object"
        for required in tool["input_schema"].get("required", []):
            assert required in tool["input_schema"]["properties"]
