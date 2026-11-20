import time

import pytest
from fastapi.testclient import TestClient

from placex import api
from placex.ai.llm import JourneyPreparationAdvice, LLMError, PotentialJourneyStage
from placex.core import PlaceXCore
from placex.tasks.handlers import _validate_student_personalization, make_handlers


@pytest.fixture
def client(cfg):
    api.core = PlaceXCore(cfg, probe=lambda: (1, 1, 1)).start()
    yield TestClient(api.app)
    api.core.stop()
    api.core = None


class MockJourneyLLM:
    def __init__(self):
        self.calls = []

    def prepare_journey(
        self, company_name, job_role, location, experience_level, employment_type,
        job_description, database_context, student_context,
    ):
        self.calls.append({
            "company_name": company_name,
            "job_role": job_role,
            "location": location,
            "experience_level": experience_level,
            "employment_type": employment_type,
            "job_description": job_description,
            "database_context": database_context,
            "student_context": student_context,
        })
        return JourneyPreparationAdvice(
            company_role_overview=f"Preparation for {job_role}.",
            role_responsibilities=["Build and maintain role-relevant solutions."],
            relevant_technologies=["Python"],
            required_skills=["Problem solving"],
            desirable_skills=["Communication"],
            estimated_preparation_hours=12,
            preparation_sequence=["Review foundations", "Practice with a project"],
            potential_stages=[PotentialJourneyStage(
                stage_name="Role fundamentals",
                evidence_classification="ai_generated_preparation",
                expectation="A general preparation area, not a confirmed employer interview stage.",
                preparation_topics=["Role-specific foundations"],
                practical_tasks=["Explain a role-relevant concept."],
                common_mistakes=["Claiming unsupported experience."],
                practice_questions=["Which fundamentals matter most for this role?"],
            )],
            technical_subjects=[],
            coding_roadmap=[],
            technical_questions=[],
            project_preparation=[],
            behavioral_competencies=["Communication"],
            behavioral_questions=[],
            practical_assignments=[],
            preparation_tasks=[{
                "title": "Review role foundations",
                "description": "Review core role concepts.",
                "related_skill": "",
                "level": "BEGINNER",
                "estimated_hours": 2,
                "milestone": "Explain three role concepts.",
            }],
            skill_assessment=[],
            preparation_phases=[],
            prioritized_strategy=["Review the role description."],
        ), {"model": "mock-model", "input_tokens": 20, "output_tokens": 30}


class UnavailableJourneyLLM:
    def prepare_journey(self, *args, **kwargs):
        raise LLMError(
            "Could not connect to Groq",
            category="network_failure",
        )


class InvalidJourneyLLM:
    def prepare_journey(self, *args, **kwargs):
        raise LLMError(
            "Groq response did not match the required JSON schema",
            category="invalid_structured_response",
        )


def signup(client, email):
    response = client.post("/users", json={
        "email": email,
        "full_name": "Journey Test",
        "password": "correct horse battery staple",
    })
    assert response.status_code == 201, response.text
    session = response.json()
    session["id"] = session["user_id"]
    client.headers.update({"Authorization": "Bearer " + session["access_token"]})
    return session


def wait_for_job(client, job_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get(f"/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] in {"DONE", "FAILED", "CANCELLED"}:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def use_mock_llm(monkeypatch, llm, research=None):
    core = api.core
    handlers = make_handlers(
        core.repo, core.ai, core.store, core.cache, llm=llm,
        matching_weights=core.cfg.matching_weights,
        semantic_matcher=core.semantic_matcher,
        research=research,
    )
    monkeypatch.setitem(core.pool.handlers, "JOURNEY_GUIDE", handlers["JOURNEY_GUIDE"])


def generate(client, payload):
    response = client.post("/journey-guides", json=payload)
    assert response.status_code == 202, response.text
    return wait_for_job(client, response.json()["id"])


def test_dynamic_journey_matches_existing_company_role_and_labels_database_data(client, monkeypatch):
    signup(client, "dynamic-existing@example.test")
    llm = MockJourneyLLM()
    use_mock_llm(monkeypatch, llm)

    job = generate(client, {
        "company_name": "acme technologies (sample)",
        "job_role": "Backend Developer Intern",
        "location": "India",
        "experience_level": "Fresher",
    })

    assert job["status"] == "DONE", job.get("error")
    result = job["result"]
    assert result["information_availability"]["company_database_record"] is True
    assert result["information_availability"]["stored_hiring_rounds"] == 3
    assert result["database_information"]["company"]["data_classification"] == "sample"
    assert result["rounds"][0]["data_classification"] == "sample"
    assert result["guidance_status"] == "ai_generated"
    assert result["ai_guidance"]["potential_stages"][0]["stage_name"] == "Role fundamentals"
    assert result["verification_notice"].startswith(
        "Company and hiring information is labeled by its source"
    )
    assert result["external_research"]["status"] == "not_configured"
    assert "Live web research is not configured" in result["external_research"]["notice"]
    assert llm.calls[0]["job_role"] == "Backend Developer Intern"
    assert llm.calls[0]["database_context"]["database_information"]["hiring_rounds"]


def test_journey_combines_database_external_sources_and_groq_context(client, monkeypatch):
    signup(client, "dynamic-research@example.test")
    llm = MockJourneyLLM()

    class FakeResearch:
        def search(self, query, *, company_name=None):
            return {
                "status": "live_search",
                "provider_configured": True,
                "provider": "mock-search",
                "query": query,
                "searched_for_this_request": True,
                "cache_hit": False,
                "retrieved_at": "2026-11-15T00:00:00+00:00",
                "sources": [{
                    "title": "Company career page",
                    "url": "https://careers.example.test/software",
                    "snippet": "Role requirements from search provider.",
                    "source_domain": "careers.example.test",
                    "published_at": None,
                    "retrieved_at": "2026-11-15T00:00:00+00:00",
                    "source_classification": "company_domain_candidate",
                    "evidence_type": "search_provider_snippet",
                }],
                "notice": "Search snippets are unverified source-attributed context.",
            }

    use_mock_llm(monkeypatch, llm, FakeResearch())
    job = generate(client, {
        "company_name": "Acme Technologies (sample)",
        "job_role": "Backend Developer Intern",
    })

    assert job["status"] == "DONE", job.get("error")
    assert job["result"]["external_research"]["status"] == "live_search"
    assert job["result"]["external_research"]["sources"][0]["url"].startswith("https://")
    assert llm.calls[0]["database_context"]["database_information"]["hiring_rounds"]
    assert llm.calls[0]["database_context"]["external_research"]["sources"][0]["title"] == "Company career page"
    saved = client.get(
        f"/journey-guides/{job['result']['guide_id']}"
    )
    assert saved.status_code == 200
    assert saved.json()["retrieved_context"]["external_research"]["sources"][0]["url"] == (
        "https://careers.example.test/software"
    )


def test_journey_schema_readiness_checks_required_columns_in_current_schema(client):
    user = signup(client, "dynamic-schema-ready@example.test")

    assert api.core.repo.journey_schema_missing_columns() == []
    assert api.core.repo.journey_roadmap_schema_missing_columns() == []
    response = client.get(f"/users/{user['id']}/journey-guides")

    assert response.status_code == 200
    assert response.json() == []


def test_saved_guide_endpoint_reports_missing_required_schema_objects(client, monkeypatch, caplog):
    user = signup(client, "dynamic-schema-missing@example.test")
    monkeypatch.setattr(
        api.core.repo,
        "journey_schema_missing_columns",
        lambda: ["journey_guides.guidance"],
    )

    response = client.get(f"/users/{user['id']}/journey-guides")

    assert response.status_code == 503
    assert "temporarily unavailable" in response.json()["detail"]
    assert "journey_schema_unavailable operation=list_saved" in caplog.text
    assert "journey_guides.guidance" in caplog.text


def test_unknown_company_and_role_are_accepted_and_saved_with_optional_fields_omitted(client, monkeypatch):
    user = signup(client, "dynamic-unknown@example.test")
    llm = MockJourneyLLM()
    use_mock_llm(monkeypatch, llm)

    job = generate(client, {
        "company_name": "Brand New Employer 90210",
        "job_role": "Quantum Materials Researcher",
    })

    assert job["status"] == "DONE", job.get("error")
    result = job["result"]
    assert result["company_id"] is None
    assert result["data_available"] is False
    assert result["database_information"]["company"] is None
    assert result["information_availability"]["verified_hiring_process"] is False
    assert result["ai_guidance_source"].startswith("AI-generated")
    assert llm.calls[0]["location"] is None
    assert llm.calls[0]["database_context"]["database_information"]["hiring_rounds"] == []

    saved = client.get(f"/users/{user['id']}/journey-guides")
    assert saved.status_code == 200
    assert len(saved.json()) == 1
    assert saved.json()[0]["company_name"] == "Brand New Employer 90210"
    loaded = client.get(f"/journey-guides/{result['guide_id']}")
    assert loaded.status_code == 200
    assert loaded.json()["guidance"]["job_role"] == "Quantum Materials Researcher"


def test_database_retrieval_failure_is_logged_with_stage_and_safe_job_error(client, monkeypatch, caplog):
    signup(client, "dynamic-db-failure@example.test")
    use_mock_llm(monkeypatch, MockJourneyLLM())

    def fail_context(*args, **kwargs):
        raise RuntimeError("sensitive query parameter")

    monkeypatch.setattr(api.core.repo, "journey_context", fail_context)
    job = generate(client, {
        "company_name": "Arbitrary Employer",
        "job_role": "Arbitrary Role",
    })

    assert job["status"] == "FAILED"
    assert "Journey Guide could not be generated" in job["error"]
    assert "job_failure job_id=" in caplog.text
    assert "operation=journey_guide failure_stage=database_retrieval" in caplog.text
    assert "exception_class=RuntimeError safe_error=Unexpected backend failure" in caplog.text
    assert "sensitive query parameter" not in caplog.text
    assert "sensitive query parameter" not in job["error"]


def test_journey_uses_saved_resume_and_matching_ats_gaps(client, monkeypatch):
    user = signup(client, "dynamic-personalized@example.test")
    resume = api.core.repo.add_resume(
        user["id"], "UPLOADED", "journey-resume.txt", None, None, 32,
        "Skills: Python, SQL\nProjects:\n- Built a Python API and queried SQL databases.",
    )
    found = api.core.ai.extract_skills("Python SQL", api.core.repo.list_skills())
    api.core.repo.set_resume_skills(resume["id"], found)
    api.core.repo.save_report(user["id"], resume["id"], 1, None, 50, [])
    llm = MockJourneyLLM()
    use_mock_llm(monkeypatch, llm)

    job = generate(client, {
        "company_name": "Acme Technologies (sample)",
        "job_role": "Backend Developer Intern",
    })

    assert job["status"] == "DONE", job.get("error")
    student = llm.calls[0]["student_context"]
    assert student["resume_available"] is True
    assert student["ats_analysis_available"] is True
    assert "Python" in student["skills"]
    assert "Data Structures" in student["missing_skills"]
    assert job["result"]["personalization"]["ats_analysis_available"] is True


def test_student_personalization_only_keeps_supported_evidence_and_owned_projects():
    advice = {
        "project_preparation": [
            {"project_index": 0, "concepts_to_review": ["Python"]},
            {"project_index": 9, "concepts_to_review": ["Invented project"]},
        ],
        "skill_assessment": [
            {
                "skill": "Python",
                "status": "demonstrated",
                "evidence": ["Built a Python API"],
                "next_action": "Practice testing.",
            },
            {
                "skill": "Go",
                "status": "demonstrated",
                "evidence": ["I have extensive Go experience."],
                "next_action": "Practice Go.",
            },
            {
                "skill": "Kubernetes",
                "status": "not_demonstrated_in_available_information",
                "evidence": [],
                "next_action": "Confirm your experience.",
            },
        ],
    }

    validated = _validate_student_personalization(advice, {
        "projects": [{"index": 0, "evidence": "Built a Python API"}],
        "skills": ["Python"],
        "missing_skills": ["Kubernetes"],
        "skill_evidence": ["Python"],
    })

    assert [item["project_index"] for item in validated["project_preparation"]] == [0]
    assert validated["skill_assessment"][0]["status"] == "demonstrated"
    assert validated["skill_assessment"][0]["evidence"] == ["Built a Python API"]
    assert validated["skill_assessment"][1]["status"] == "requires_student_confirmation"
    assert validated["skill_assessment"][1]["evidence"] == []
    assert validated["skill_assessment"][2]["status"] == "not_demonstrated_in_available_information"


def test_saved_guide_creates_idempotent_trackable_prep_plan(client, monkeypatch):
    user = signup(client, "dynamic-guide-plan@example.test")
    use_mock_llm(monkeypatch, MockJourneyLLM())
    job = generate(client, {
        "company_name": "Arbitrary Employer",
        "job_role": "Role without a database ID",
    })
    assert job["status"] == "DONE", job.get("error")
    guide_id = job["result"]["guide_id"]

    created = client.post(
        f"/journey-guides/{guide_id}/roadmap",
        json={"daily_study_hours": 3, "deadline_days": 15},
    )
    assert created.status_code == 201, created.text
    assert created.json()["created"] is True
    assert created.json()["prep_url"] == "/prep-plan.html"

    repeated = client.post(
        f"/journey-guides/{guide_id}/roadmap",
        json={"daily_study_hours": 3, "deadline_days": 15},
    )
    assert repeated.status_code == 201, repeated.text
    assert repeated.json()["id"] == created.json()["id"]
    assert repeated.json()["created"] is False

    response = client.get(f"/users/{user['id']}/roadmaps")
    assert response.status_code == 200
    roadmap = response.json()[0]
    assert roadmap["source_guide_id"] == guide_id
    assert roadmap["jd_id"] is None
    assert roadmap["daily_study_hours"] == 3
    assert roadmap["deadline_days"] == 15
    assert roadmap["items"][0]["title"] == "Review role foundations"
    assert roadmap["items"][0]["skill"] == "Review role foundations"
    assert roadmap["items"][0]["practice_task"] == "Review core role concepts."

    item_id = roadmap["items"][0]["id"]
    update = client.patch(f"/roadmap-items/{item_id}", json={"status": "DONE"})
    assert update.status_code == 200, update.text
    refreshed = client.get(f"/users/{user['id']}/roadmaps").json()[0]
    assert refreshed["items"][0]["status"] == "DONE"
    assert refreshed["completion_pct"] == 100

    other = client.post("/users", json={
        "email": "dynamic-guide-plan-other@example.test",
        "full_name": "Other Student",
        "password": "correct horse battery staple",
    }).json()
    client.headers.update({"Authorization": "Bearer " + other["access_token"]})
    assert client.get(f"/users/{user['id']}/roadmaps").status_code == 403
    assert client.patch(
        f"/roadmap-items/{item_id}", json={"status": "TODO"}
    ).status_code == 404


def test_groq_unavailable_uses_and_persists_unverified_fallback(client, monkeypatch):
    user = signup(client, "dynamic-fallback@example.test")
    use_mock_llm(monkeypatch, UnavailableJourneyLLM())

    job = generate(client, {
        "company_name": "Unknown Fallback Employer",
        "job_role": "Accessibility Consultant",
    })

    assert job["status"] == "DONE", job.get("error")
    result = job["result"]
    assert result["guidance_status"] == "fallback"
    assert result["ai_status"] == "unavailable"
    assert result["ai_error_category"] == "network_failure"
    assert "could not be reached" in result["ai_notice"]
    assert result["ai_guidance"]["potential_stages"]
    assert "not verified" in result["ai_guidance_source"]
    saved = client.get(f"/users/{user['id']}/journey-guides")
    assert saved.status_code == 200 and saved.json()[0]["guidance"]["guidance_status"] == "fallback"


def test_invalid_groq_response_uses_fallback_and_persists_guide(client, monkeypatch):
    user = signup(client, "dynamic-invalid-response@example.test")
    use_mock_llm(monkeypatch, InvalidJourneyLLM())

    job = generate(client, {
        "company_name": "Unlisted Company",
        "job_role": "Specialist Role",
    })

    assert job["status"] == "DONE", job.get("error")
    assert job["result"]["guidance_status"] == "fallback"
    assert job["result"]["ai_error_category"] == "invalid_structured_response"
    assert "valid structured guide" in job["result"]["ai_notice"]
    assert job["result"]["ai_guidance"]["potential_stages"]
    saved = client.get(f"/users/{user['id']}/journey-guides")
    assert saved.status_code == 200
    assert saved.json()[0]["guidance"]["guidance_status"] == "fallback"


def test_missing_groq_configuration_uses_clear_fallback_and_persists_guide(client):
    user = signup(client, "dynamic-no-groq-config@example.test")

    job = generate(client, {
        "company_name": "Unlisted Company",
        "job_role": "Specialist Role",
    })

    assert job["status"] == "DONE", job.get("error")
    assert job["result"]["guidance_status"] == "fallback"
    assert job["result"]["ai_error_category"] == "not_configured"
    assert "Groq is not configured" in job["result"]["ai_notice"]
    saved = client.get(f"/users/{user['id']}/journey-guides")
    assert saved.status_code == 200
    assert saved.json()[0]["guidance"]["guidance_status"] == "fallback"


def test_dynamic_journey_validates_required_text_and_saved_guides_are_owner_scoped(client):
    user = signup(client, "dynamic-owner@example.test")
    assert client.post("/journey-guides", json={
        "company_name": "   ", "job_role": "Software Engineer",
    }).status_code == 422
    other = client.post("/users", json={
        "email": "dynamic-other@example.test",
        "full_name": "Other Student",
        "password": "correct horse battery staple",
    }).json()
    client.headers.update({"Authorization": "Bearer " + other["access_token"]})
    assert client.get(f"/users/{user['id']}/journey-guides").status_code == 403
