import json

import pytest

from placex.ai.llm import LLMService
from placex.config import Config
from placex.matching import (
    EmbeddingSemanticMatcher,
    SemanticMatchingError,
    build_matching_report,
    validate_match_weights,
)


def _gap():
    return [
        {
            "skill_id": 1,
            "skill_name": "Python",
            "importance": 5,
            "required": True,
            "matched": True,
        },
        {
            "skill_id": 2,
            "skill_name": "Docker",
            "importance": 3,
            "required": True,
            "matched": False,
        },
    ]


def test_report_preserves_exact_skill_baseline_and_quotes_evidence():
    resume = "Built APIs with Python. Used containers in a class project."
    report = build_matching_report(
        _gap(),
        resume,
        "Required: Python and Docker.",
        skill_evidence=[{
            "skill_id": 1,
            "skill_name": "Python",
            "evidence": "Python",
        }],
    )

    assert report["components"]["exact_skill_match"]["score"] == 62.5
    assert report["overall_match_score"] == 62.5
    evidence = report["requirements"][0]["evidence"][0]
    assert evidence["source"] == "resume"
    assert evidence["quote"] == "Python"
    assert resume[evidence["start"]:evidence["end"]] == evidence["quote"]
    assert evidence["line"] == 1
    assert report["missing_skills"] == ["Docker"]
    assert report["eligibility_state"] == "not_assessed"
    assert report["confirmed_ineligibility"] is False
    assert "unassessed dimensions are excluded" in report["score_explanation"].lower()


def test_semantic_matches_are_partial_and_require_verbatim_resume_evidence():
    resume = "Used containerization for a class project."
    report = build_matching_report(
        _gap(),
        resume,
        "Required Python and Docker.",
        semantic_matches={
            "Docker": {
                "similarity": 0.81,
                "evidence": "containerization for a class project",
            }
        },
        semantic_status="available",
    )

    assert report["components"]["semantic_skill_match"]["score"] == 81.0
    partial = report["partially_matched_skills"][0]
    assert partial["skill"] == "Docker"
    assert partial["similarity"] == 0.81
    assert partial["evidence"]["source"] == "resume"
    assert resume[partial["evidence"]["start"]:partial["evidence"]["end"]] == partial["evidence"]["quote"]
    assert partial["evidence"]["line"] == 1
    assert report["missing_skills"] == []
    assert report["requirements"][1]["status"] == "partially_matched"


def test_unavailable_resume_sections_are_neutral_and_quotes_are_source_checked():
    report = build_matching_report(
        _gap(),
        "Python",
        "Python. Requires five years of experience.",
        extracted_requirements=[{
            "category": "experience",
            "requirement": "Five years of experience",
            "jd_evidence": "Requires five years of experience.",
            "resume_evidence": ["five years of experience"],
            "importance": 5,
            "required": True,
        }],
    )

    assert report["components"]["experience_match"]["status"] == "not_assessed"
    assert report["components"]["experience_match"]["score"] is None
    assert report["components"]["eligibility_match"]["status"] == "not_assessed"
    assert report["requirements"][-1]["status"] == "not_assessed"
    assert report["confirmed_ineligibility"] is False


def test_grounded_non_skill_evidence_is_scored_and_unverified_quotes_are_ignored():
    jd = "Experience: internship on a payments service. Must be fluent in French."
    report = build_matching_report(
        _gap(),
        "Interned on a payments service using Python.",
        jd,
        extracted_requirements=[
            {
                "category": "experience",
                "requirement": "Payments service internship",
                "jd_evidence": "internship on a payments service",
                "resume_evidence": ["Interned on a payments service"],
                "importance": 4,
                "required": False,
            },
            {
                "category": "education",
                "requirement": "French fluency",
                "jd_evidence": "Must be fluent in French.",
                "resume_evidence": ["Native French speaker"],
                "importance": 2,
                "required": True,
            },
        ],
    )

    assert report["components"]["experience_match"]["score"] == 100
    assert report["components"]["eligibility_match"]["status"] == "not_assessed"
    evidence = report["requirements"][-1]["evidence"][0]
    assert evidence["source"] == "job_description"
    assert jd[evidence["start"]:evidence["end"]] == evidence["quote"]
    assert evidence["line"] == 1


class FakeEmbeddingProvider:
    def embed(self, texts):
        return [
            [1.0, 0.0] if "container" in text.lower() else [0.8, 0.6]
            for text in texts
        ]


def test_embedding_matcher_returns_cosine_similarity_and_grounded_chunk():
    matcher = EmbeddingSemanticMatcher(FakeEmbeddingProvider(), threshold=0.7)

    matches = matcher.match("Built containerized services.", ["Docker"])

    assert matches["Docker"]["similarity"] == 0.8
    assert matches["Docker"]["evidence"] == "Built containerized services."


def test_embedding_matcher_rejects_malformed_vectors():
    class BadProvider:
        def embed(self, texts):
            return [[1.0], [1.0, 2.0]]

    with pytest.raises(SemanticMatchingError, match="dimensions"):
        EmbeddingSemanticMatcher(BadProvider()).match("resume", ["skill"])


def test_matching_weights_are_validated_and_override_defaults():
    weights = validate_match_weights({"exact_skill_match": 0.7})

    assert weights["exact_skill_match"] == 0.7
    assert weights["semantic_skill_match"] == 0.15
    with pytest.raises(ValueError, match="unknown matching weight"):
        validate_match_weights({"unknown": 1})
    with pytest.raises(ValueError, match="at least one"):
        validate_match_weights({key: 0 for key in weights})


def test_config_reads_validated_matching_weights_from_environment(monkeypatch):
    monkeypatch.setenv(
        "PLACEX_MATCH_WEIGHTS",
        '{"exact_skill_match":0.65,"semantic_skill_match":0.1}',
    )

    weights = Config().matching_weights

    assert weights["exact_skill_match"] == 0.65
    assert weights["semantic_skill_match"] == 0.1
    assert weights["project_relevance"] == 0.1


def test_config_rejects_malformed_matching_weights(monkeypatch):
    monkeypatch.setenv("PLACEX_MATCH_WEIGHTS", "not-json")

    with pytest.raises(ValueError, match="must be a JSON object"):
        Config()


def test_llm_evidence_extraction_is_structured_and_removes_contact_lines():
    class FakeProvider:
        def generate_json(self, *, system_prompt, user_data, response_schema):
            self.input = user_data
            return json.dumps({
                "requirements": [{
                    "category": "project",
                    "requirement": "Built payment systems",
                    "jd_evidence": "Build payment systems.",
                    "resume_evidence": ["Built payment systems with Python."],
                    "importance": 4,
                    "required": True,
                }]
            }), {"model": "mock-model", "input_tokens": 10, "output_tokens": 12}

    provider = FakeProvider()
    service = LLMService(provider)

    extraction, usage = service.extract_matching_evidence(
        "Student Name\nstudent@example.test\nBuilt payment systems with Python.",
        "Build payment systems.",
        "Backend Engineer",
    )

    assert extraction.requirements[0].category == "project"
    assert extraction.requirements[0].resume_evidence == [
        "Built payment systems with Python."
    ]
    assert "student@example.test" not in provider.input["resume_text"]
    assert usage["model"] == "mock-model"
