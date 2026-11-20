from datetime import datetime, timedelta, timezone

import httpx
import pytest

from placex.research import (
    BRAVE_SEARCH_URL,
    BraveSearchProvider,
    ResearchProviderError,
    ResearchService,
    create_research_service,
)


def brave_result():
    return {
        "web": {
            "results": [
                {
                    "title": "Company careers",
                    "url": "https://careers.acme.example/jobs/1",
                    "description": "Official careers result snippet.",
                    "page_age": "2026-01-02T03:04:05Z",
                },
                {
                    "title": "Duplicate URL",
                    "url": "https://careers.acme.example/jobs/1#details",
                    "description": "Duplicate result.",
                },
                {
                    "title": "Community discussion",
                    "url": "https://www.reddit.com/r/jobs/abc",
                    "description": "Community source snippet.",
                },
                {
                    "title": "Invalid scheme",
                    "url": "javascript:alert(1)",
                    "description": "Must be discarded.",
                },
            ]
        }
    }


def test_brave_provider_maps_attributed_results_and_uses_server_side_auth(monkeypatch):
    observed = {}

    def handle_request(request):
        observed["url"] = str(request.url)
        observed["token"] = request.headers["X-Subscription-Token"]
        return httpx.Response(200, json=brave_result())

    real_client = httpx.Client

    def mocked_client(*args, **kwargs):
        return real_client(
            *args,
            transport=httpx.MockTransport(handle_request),
            **kwargs,
        )

    monkeypatch.setattr("placex.research.httpx.Client", mocked_client)
    provider = BraveSearchProvider("test-brave-key")
    try:
        results = provider.search("Acme software engineer interviews", 8)
    finally:
        provider.close()

    assert observed["url"].startswith(BRAVE_SEARCH_URL)
    assert observed["token"] == "test-brave-key"
    assert len(results) == 2
    assert results[0]["source_domain"] == "careers.acme.example"
    assert results[0]["published_at"] == "2026-01-02T03:04:05+00:00"
    assert results[0]["evidence_type"] == "search_provider_snippet"


def test_brave_provider_reports_rate_limit_without_fabricating_results(monkeypatch):
    def handle_request(request):
        return httpx.Response(429, json={"error": "rate limited"})

    real_client = httpx.Client

    def mocked_client(*args, **kwargs):
        return real_client(
            *args,
            transport=httpx.MockTransport(handle_request),
            **kwargs,
        )

    monkeypatch.setattr("placex.research.httpx.Client", mocked_client)
    provider = BraveSearchProvider("test-brave-key")
    try:
        with pytest.raises(ResearchProviderError, match="rate limit"):
            provider.search("software engineer", 5)
    finally:
        provider.close()


def test_no_provider_explicitly_reports_live_research_unavailable():
    result = ResearchService(None).search("Acme Backend Engineer")

    assert result["status"] == "not_configured"
    assert result["provider_configured"] is False
    assert result["searched_for_this_request"] is False
    assert result["sources"] == []
    assert "Live web research is not configured" in result["notice"]


class FakeProvider:
    name = "fake-search"

    def __init__(self, results=None, error=None):
        self.results = results or []
        self.error = error
        self.queries = []

    def search(self, query, limit):
        self.queries.append((query, limit))
        if self.error:
            raise self.error
        return self.results

    def close(self):
        pass


class FakeResearchRepo:
    def __init__(self):
        self.rows = {}

    def get_research_cache(self, query_hash, include_expired=False):
        return self.rows.get(query_hash)

    def save_research_cache(
        self, query_hash, provider_name, query_text, results,
        retrieved_at, expires_at,
    ):
        self.rows[query_hash] = {
            "provider_name": provider_name,
            "query_text": query_text,
            "results": results,
            "retrieved_at": retrieved_at,
            "expires_at": expires_at,
        }


def test_research_service_ranks_sources_and_uses_persistent_cache():
    results = [
        {
            "title": "Community",
            "url": "https://reddit.com/r/Acme/1",
            "snippet": "Community discussion",
            "source_domain": "reddit.com",
            "published_at": None,
            "retrieved_at": "2026-01-01T00:00:00+00:00",
        },
        {
            "title": "Acme careers",
            "url": "https://careers.acme.example/jobs/1",
            "snippet": "Company careers snippet",
            "source_domain": "careers.acme.example",
            "published_at": "2026-01-02T00:00:00+00:00",
            "retrieved_at": "2026-01-02T00:00:00+00:00",
        },
        {
            "title": "Duplicate",
            "url": "https://careers.acme.example/jobs/1?tracking=1",
            "snippet": "Duplicate source",
            "source_domain": "careers.acme.example",
            "published_at": None,
            "retrieved_at": "2026-01-02T00:00:00+00:00",
        },
    ]
    repo = FakeResearchRepo()
    provider = FakeProvider(results)
    service = ResearchService(provider, repo, cache_ttl_seconds=3600, result_limit=8)

    first = service.search("Acme Software Engineer", company_name="Acme")
    second = service.search("Acme Software Engineer", company_name="Acme")

    assert first["status"] == "live_search"
    assert first["searched_for_this_request"] is True
    assert first["sources"][0]["source_classification"] == "company_domain_candidate"
    assert len(first["sources"]) == 2
    assert second["status"] == "cached"
    assert second["cache_hit"] is True
    assert len(provider.queries) == 1


def test_research_service_uses_expired_cache_if_provider_is_down():
    repo = FakeResearchRepo()
    provider = FakeProvider([
        {
            "title": "Old source",
            "url": "https://docs.example/role",
            "snippet": "A previously retrieved snippet",
            "source_domain": "docs.example",
            "published_at": None,
            "retrieved_at": "2026-01-01T00:00:00+00:00",
        }
    ])
    ResearchService(provider, repo, cache_ttl_seconds=1).search("Target role")
    row = next(iter(repo.rows.values()))
    row["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    down = ResearchService(FakeProvider(error=ResearchProviderError("offline")), repo)

    result = down.search("Target role")

    assert result["status"] == "stale_cache"
    assert result["cache_hit"] is True
    assert result["searched_for_this_request"] is False
    assert len(result["sources"]) == 1
    assert "offline" not in result["notice"]


def test_research_provider_outage_without_cache_returns_explicit_unavailable():
    result = ResearchService(
        FakeProvider(error=ResearchProviderError("offline"))
    ).search("Target role")

    assert result["status"] == "provider_unavailable"
    assert result["provider_configured"] is True
    assert result["sources"] == []
    assert "unavailable" in result["notice"].lower()


def test_research_factory_requires_key_only_when_brave_is_selected():
    class Config:
        research_provider = "brave"
        brave_search_api_key = None
        research_timeout_s = 5
        research_cache_ttl_seconds = 60
        research_result_limit = 5

    with pytest.raises(ValueError, match="BRAVE_SEARCH_API_KEY"):
        create_research_service(Config())
