import sys
import types
import json

import pytest

from placex.job_sources import (
    ConfiguredJSONJobProvider,
    JobListing,
    JobProviderError,
    JobSearchFilters,
)
from placex.matching import SemanticMatchingError, SentenceTransformerProvider


def test_configured_provider_uses_secure_auth_and_validates_source_records(monkeypatch):
    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            return [json.dumps({
                "jobs": [{
                    "external_id": "provider-123",
                    "source_url": "https://careers.example/jobs/provider-123",
                    "company_name": "Provider Company",
                    "role_title": "Backend Intern",
                    "description": "Build backend services with Python.",
                }]
            }).encode()]

    class Client:
        def __init__(self, *, headers, timeout, follow_redirects):
            self.headers = headers
            self.follow_redirects = follow_redirects

        def stream(self, method, endpoint, params):
            self.endpoint = endpoint
            self.params = params
            return Response()

        def close(self):
            pass

    clients = []

    def create_client(**kwargs):
        client = Client(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr("placex.job_sources.httpx.Client", create_client)
    provider = ConfiguredJSONJobProvider(
        name="test-source",
        endpoint="https://jobs.example/api",
        api_key="test-key-not-a-secret",
    )

    result = provider.search(JobSearchFilters(query="Backend", remote=True))

    assert result[0].external_id == "provider-123"
    assert result[0].is_verified is False
    assert clients[0].headers["Authorization"] == "Bearer test-key-not-a-secret"
    assert clients[0].follow_redirects is False
    assert clients[0].params["query"] == "Backend"
    assert clients[0].params["remote"] is True
    provider.close()


def test_provider_refuses_to_send_api_key_over_http():
    with pytest.raises(ValueError, match="HTTPS"):
        ConfiguredJSONJobProvider(
            name="test-source",
            endpoint="http://jobs.example/api",
            api_key="test-key",
        )


def test_provider_rejects_malformed_listing_without_success_fallback(monkeypatch):
    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            return [b'{"jobs":[{"role_title":"Missing required source fields"}]}']

    class Client:
        def __init__(self, **kwargs):
            pass

        def stream(self, *args, **kwargs):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr("placex.job_sources.httpx.Client", Client)
    provider = ConfiguredJSONJobProvider(name="test-source", endpoint="https://jobs.example/api")

    with pytest.raises(JobProviderError, match="invalid listing"):
        provider.search(JobSearchFilters())
    provider.close()


def test_listing_requires_source_url_and_core_source_fields():
    with pytest.raises(ValueError):
        JobListing.model_validate({"external_id": "no-source-data"})


def test_sentence_transformer_model_is_loaded_lazily_and_reused(monkeypatch):
    calls = []

    class FakeModel:
        def encode(self, texts, **kwargs):
            return types.SimpleNamespace(
                tolist=lambda: [[1.0, 0.0] for _ in texts]
            )

    def load_model(model_name, **kwargs):
        calls.append((model_name, kwargs))
        return FakeModel()

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = load_model
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    provider = SentenceTransformerProvider("local/test-model", local_files_only=True)

    assert calls == []
    assert provider.embed(["first"])[0] == [1.0, 0.0]
    assert provider.embed(["second"])[0] == [1.0, 0.0]
    assert len(calls) == 1
    assert calls[0][0] == "local/test-model"
    assert calls[0][1]["local_files_only"] is True


def test_sentence_transformer_missing_dependency_is_a_safe_match_error(monkeypatch):
    import builtins

    monkeypatch.delitem(sys.modules, "sentence_transformers", raising=False)
    provider = SentenceTransformerProvider("missing/model", local_files_only=True)
    original_import = builtins.__import__
    monkeypatch.setattr(
        builtins,
        "__import__",
        lambda name, *args, **kwargs: (
            (_ for _ in ()).throw(ImportError("not installed"))
            if name == "sentence_transformers"
            else original_import(name, *args, **kwargs)
        ),
    )

    with pytest.raises(SemanticMatchingError, match="not installed"):
        provider.embed(["resume text"])
