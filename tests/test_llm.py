import json

import httpx
import pytest

from placex.ai.llm import GroqProvider, LLMError, LLMService, create_llm_service
from placex.config import Config


def test_groq_provider_uses_groq_configuration_and_endpoint(monkeypatch):
    observed = {}
    real_client = httpx.Client

    def handle_request(request):
        observed["url"] = str(request.url)
        observed["authorization"] = request.headers["Authorization"]
        observed["request"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "groq-test-model",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": '{"ok": true}'},
                }],
                "usage": {"prompt_tokens": 11, "completion_tokens": 5},
            },
        )

    def mocked_client(*args, **kwargs):
        return real_client(
            *args,
            transport=httpx.MockTransport(handle_request),
            **kwargs,
        )

    monkeypatch.setattr("placex.ai.llm.httpx.Client", mocked_client)
    monkeypatch.setenv("PLACEX_LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.setenv("GROQ_MODEL", "test-groq-model")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    service = create_llm_service(Config())
    try:
        assert isinstance(service.provider, GroqProvider)
        content, usage = service.provider.generate_json(
            system_prompt="Return a JSON object.",
            user_data={"value": 1},
            response_schema={
                "name": "test_response",
                "schema": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
            },
        )
    finally:
        service.close()

    assert content == '{"ok": true}'
    assert usage == {
        "model": "groq-test-model",
        "input_tokens": 11,
        "output_tokens": 5,
    }
    assert observed["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert "api.openai.com" not in observed["url"]
    assert observed["authorization"] == "Bearer test-groq-key"
    assert observed["request"]["model"] == "test-groq-model"


def test_missing_groq_api_key_disables_optional_ai_with_diagnostic(monkeypatch, caplog):
    monkeypatch.setenv("PLACEX_LLM_PROVIDER", "groq")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert create_llm_service(Config()) is None
    assert "category=missing_api_configuration" in caplog.text


def test_groq_provider_treats_blank_api_key_as_missing(monkeypatch, caplog):
    monkeypatch.setenv("PLACEX_LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "   ")

    assert create_llm_service(Config()) is None
    assert "category=missing_api_configuration" in caplog.text


def test_llm_disabled_by_default(monkeypatch):
    monkeypatch.delenv("PLACEX_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    assert create_llm_service(Config()) is None


def test_journey_llm_generates_general_guidance_without_verified_rounds():
    class FakeProvider:
        def generate_json(self, *, system_prompt, user_data, response_schema):
            self.prompt = system_prompt
            self.data = user_data
            return json.dumps({
                "rounds": [],
                "mock_interview_questions": ["Describe a project you built."],
            }), {"model": "mock", "input_tokens": 4, "output_tokens": 3}

    provider = FakeProvider()
    advice, _ = LLMService(provider).explain_journey(
        "Example Employer", "Backend Engineer", []
    )

    assert advice.rounds == []
    assert advice.mock_interview_questions == ["Describe a project you built."]
    assert provider.data["database_rounds"] == []
    assert "do not claim anything about this company's hiring process" in provider.prompt.lower()


def journey_response():
    return {
        "company_role_overview": "Prepare for the requested role.",
        "role_responsibilities": ["Build and test role-relevant solutions."],
        "relevant_technologies": ["Python"],
        "required_skills": ["Problem solving"],
        "desirable_skills": ["Communication"],
        "estimated_preparation_hours": 12,
        "preparation_sequence": ["Review foundations", "Practice"],
        "potential_stages": [{
            "stage_name": "Role fundamentals",
            "evidence_classification": "ai_generated_preparation",
            "expectation": "A general preparation area, not a confirmed employer interview stage.",
            "preparation_topics": ["Core role concepts"],
            "practical_tasks": ["Explain one concept."],
            "common_mistakes": ["Claiming unsupported experience."],
            "practice_questions": ["Explain a relevant concept."],
        }],
        "technical_subjects": [{
            "name": "Core role concepts",
            "why_it_matters": "These support daily role tasks.",
            "priority": "high",
            "practice_action": "Explain the concept and solve a small example.",
        }],
        "coding_roadmap": [],
        "technical_questions": [],
        "project_preparation": [],
        "behavioral_competencies": ["Communication"],
        "behavioral_questions": [],
        "practical_assignments": [],
        "preparation_tasks": [{
            "title": "Review role foundations",
            "description": "Review core role topics.",
            "related_skill": "",
            "level": "BEGINNER",
            "estimated_hours": 2,
            "milestone": "Explain three role concepts.",
        }],
        "skill_assessment": [],
        "preparation_phases": [],
        "prioritized_strategy": ["Review the role requirements."],
    }


def test_dynamic_journey_uses_structured_context_and_validates_response():
    class FakeProvider:
        def generate_json(self, *, system_prompt, user_data, response_schema):
            self.prompt = system_prompt
            self.data = user_data
            self.schema = response_schema
            return json.dumps(journey_response()), {
                "model": "mock-model", "input_tokens": 25, "output_tokens": 40,
            }

    provider = FakeProvider()
    advice, usage = LLMService(provider).prepare_journey(
        "Any Employer", "Any Specialist Role", "Remote", "Fresher", None, None,
        {"hiring_rounds": [], "job_descriptions": []},
        {"skills": ["Python"], "missing_skills": ["SQL"]},
    )

    assert advice.potential_stages[0].stage_name == "Role fundamentals"
    assert usage["model"] == "mock-model"
    assert provider.data["request"]["job_role"] == "Any Specialist Role"
    assert provider.data["student_context"]["missing_skills"] == ["SQL"]
    assert "general ai-generated guidance" in provider.prompt.lower()
    assert provider.schema["name"] == "journey_preparation_guide"
    assert provider.schema["max_output_tokens"] == 4096


def test_dynamic_journey_rejects_invalid_structured_response():
    class FakeProvider:
        def generate_json(self, **kwargs):
            return '{"not_a_journey": true}', {"model": "mock"}

    with pytest.raises(LLMError, match="required JSON schema"):
        LLMService(FakeProvider()).prepare_journey(
            "Employer", "Role", None, None, None, None, {}, {}
        )


def test_groq_rate_limit_returns_safe_error_after_retry(monkeypatch):
    calls = []

    def handle_request(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": {"message": "rate limited"}})

    monkeypatch.setattr(GroqProvider, "_wait_before_retry", staticmethod(lambda *args: None))
    client = httpx.Client(
        base_url=GroqProvider.API_BASE_URL,
        headers={"Authorization": "Bearer test-key"},
        transport=httpx.MockTransport(handle_request),
        timeout=1,
    )
    provider = GroqProvider("test-key", "test-model", 1, 1, 100)
    provider.client.close()
    provider.client = client
    with pytest.raises(LLMError, match="HTTP 429") as error:
        provider.generate_json(system_prompt="x", user_data={}, response_schema={"name": "x", "schema": {}})
    assert len(calls) == 2
    assert error.value.category == "rate_limit"
    assert error.value.http_status == 429
    provider.close()


def test_groq_timeout_returns_safe_error_without_leaking_details():
    def handle_request(request):
        raise httpx.ReadTimeout("sensitive transport detail")

    client = httpx.Client(
        base_url=GroqProvider.API_BASE_URL,
        headers={"Authorization": "Bearer test-key"},
        transport=httpx.MockTransport(handle_request),
        timeout=1,
    )
    provider = GroqProvider("test-key", "test-model", 1, 0, 100)
    provider.client.close()
    provider.client = client
    with pytest.raises(LLMError, match="timed out") as error:
        provider.generate_json(system_prompt="x", user_data={}, response_schema={"name": "x", "schema": {}})
    assert "sensitive transport detail" not in str(error.value)
    assert error.value.category == "timeout"
    provider.close()


@pytest.mark.parametrize(
    ("status", "code", "category"),
    [
        (401, "invalid_api_key", "authentication_failure"),
        (400, "model_not_found", "invalid_model"),
    ],
)
def test_groq_http_failures_are_classified_without_provider_details(
    status, code, category,
):
    response = httpx.Response(
        status,
        json={"error": {"code": code, "message": "provider diagnostic detail"}},
    )
    error = GroqProvider._http_error(response)

    assert error.category == category
    assert error.http_status == status
    assert error.provider_code == code
    assert "provider diagnostic detail" not in str(error)


def test_groq_retries_provider_json_schema_generation_error(monkeypatch):
    requests = []

    def handle_request(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(
                400,
                json={"error": {
                    "code": "json_validate_failed",
                    "message": "Generated JSON does not match schema",
                }},
            )
        return httpx.Response(
            200,
            json={
                "model": "groq-test-model",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": '{"ok": true}'},
                }],
                "usage": {"prompt_tokens": 11, "completion_tokens": 5},
            },
        )

    monkeypatch.setattr(GroqProvider, "_wait_before_retry", staticmethod(lambda *args: None))
    client = httpx.Client(
        base_url=GroqProvider.API_BASE_URL,
        headers={"Authorization": "******"},
        transport=httpx.MockTransport(handle_request),
        timeout=1,
    )
    provider = GroqProvider("test-key", "test-model", 1, 1, 100)
    provider.client.close()
    provider.client = client
    try:
        content, _ = provider.generate_json(
            system_prompt="Return a complete object.",
            user_data={},
            response_schema={
                "name": "test_response",
                "schema": {"type": "object"},
            },
        )
    finally:
        provider.close()

    assert content == '{"ok": true}'
    assert len(requests) == 2
    assert "Do not omit any required field" in requests[1]["messages"][0]["content"]


def test_groq_schema_generation_failure_is_classified_after_retry(monkeypatch):
    def handle_request(request):
        return httpx.Response(
            400,
            json={"error": {
                "code": "json_validate_failed",
                "message": "details should not escape",
            }},
        )

    monkeypatch.setattr(GroqProvider, "_wait_before_retry", staticmethod(lambda *args: None))
    client = httpx.Client(
        base_url=GroqProvider.API_BASE_URL,
        headers={"Authorization": "******"},
        transport=httpx.MockTransport(handle_request),
        timeout=1,
    )
    provider = GroqProvider("test-key", "test-model", 1, 1, 100)
    provider.client.close()
    provider.client = client
    try:
        with pytest.raises(LLMError) as error:
            provider.generate_json(
                system_prompt="Return JSON.",
                user_data={},
                response_schema={"name": "test_response", "schema": {"type": "object"}},
            )
    finally:
        provider.close()

    assert error.value.category == "invalid_structured_response"
    assert error.value.http_status == 400
    assert "details should not escape" not in str(error.value)
