import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from placex import api
from placex.core import PlaceXCore
from placex.tasks.handlers import make_handlers


def test_external_research_is_wired_into_analyzer_builder_and_prep(client, monkeypatch):
    user = client.post("/users", json={
        "email": "research-all-modules@example.test",
        "full_name": "Research Student",
        "password": "correct horse battery staple",
    }).json()
    client.headers.update({
        "Authorization": "Bearer " + user["access_token"],
    })
    user_id = user["user_id"]
    resume = api.core.repo.add_resume(
        user_id,
        "UPLOADED",
        "research-integration.txt",
        None,
        None,
        64,
        "Skills: Python, SQL\nProjects:\n- Built a Python API and queried SQL databases.",
    )

    class FakeResearch:
        def __init__(self):
            self.queries = []

        def search(self, query, *, company_name=None):
            self.queries.append((query, company_name))
            return {
                "status": "live_search",
                "provider_configured": True,
                "provider": "mock-search",
                "query": query,
                "searched_for_this_request": True,
                "cache_hit": False,
                "retrieved_at": "2026-11-15T00:00:00+00:00",
                "sources": [{
                    "title": "Official learning reference",
                    "url": "https://docs.example.test/learning",
                    "snippet": "A source-attributed role learning reference.",
                    "source_domain": "docs.example.test",
                    "published_at": None,
                    "retrieved_at": "2026-11-15T00:00:00+00:00",
                    "source_classification": "secondary_source",
                    "evidence_type": "search_provider_snippet",
                }],
                "notice": "Mock search result.",
            }

    research = FakeResearch()
    core = api.core
    handlers = make_handlers(
        core.repo,
        core.ai,
        core.store,
        core.cache,
        llm=None,
        matching_weights=core.cfg.matching_weights,
        semantic_matcher=core.semantic_matcher,
        research=research,
    )
    core.pool.handlers.update(handlers)

    analyzer = core.submit(
        user_id,
        "RESUME_ANALYSIS",
        {"resume_id": resume["id"], "jd_id": 1},
    )
    analyzed = _wait(client, analyzer["id"])
    assert analyzed["status"] == "DONE", analyzed.get("error")
    assert analyzed["result"]["external_research"]["status"] == "live_search"
    assert analyzed["result"]["matching_report"]["overall_match_score"] is not None
    assert len(research.queries) == 1

    planner = core.submit(
        user_id,
        "PREP_PLAN",
        {"report_id": analyzed["result"]["report_id"], "daily_study_hours": 2},
    )
    planned = _wait(client, planner["id"])
    assert planned["status"] == "DONE", planned.get("error")
    assert planned["result"]["external_research"]["sources"][0]["url"] == (
        "https://docs.example.test/learning"
    )
    saved_roadmaps = client.get(f"/users/{user_id}/roadmaps")
    assert saved_roadmaps.status_code == 200
    assert saved_roadmaps.json()[0]["external_research"]["status"] == "live_search"
    assert len(research.queries) == 2

    builder = core.submit(
        user_id,
        "RESUME_BUILD",
        {
            "jd_id": 1,
            "profile": {
                "skills": ["Python", "SQL"],
                "projects": [{"name": "API", "description": "Built a Python API."}],
            },
        },
    )
    built = _wait(client, builder["id"])
    assert built["status"] == "DONE", built.get("error")
    assert built["result"]["external_research"]["status"] == "live_search"
    assert built["result"]["external_research"]["sources"][0]["url"] == (
        "https://docs.example.test/learning"
    )
    assert len(research.queries) == 3


def _wait(client: TestClient, job_id: int):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get(f"/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] in {"DONE", "FAILED", "CANCELLED"}:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def test_research_fallback_does_not_change_deterministic_match_score(core, user):
    class NoSearch:
        def search(self, query, *, company_name=None):
            return {
                "status": "provider_unavailable",
                "provider_configured": True,
                "provider": "mock-search",
                "query": query,
                "searched_for_this_request": False,
                "cache_hit": False,
                "retrieved_at": None,
                "sources": [],
                "notice": "Search unavailable.",
            }

    repo = core.repo
    resume = repo.add_resume(
        user["id"], "UPLOADED", "no-search.txt", None, None, 10,
        "Python SQL",
    )
    handlers = make_handlers(
        repo, core.ai, core.store, core.cache, llm=None,
        matching_weights=core.cfg.matching_weights,
        semantic_matcher=core.semantic_matcher,
        research=NoSearch(),
    )
    job = {
        "id": 9981,
        "user_id": user["id"],
        "payload": {"resume_id": resume["id"], "jd_id": 1},
    }

    # The analyzer's score is still calculated from the database-backed JD/skills;
    # this direct handler check avoids depending on network availability.
    core.pool.handlers["RESUME_ANALYSIS"] = handlers["RESUME_ANALYSIS"]
    submitted = core.submit(
        user["id"],
        "RESUME_ANALYSIS",
        {"resume_id": resume["id"], "jd_id": 1},
    )
    result = core.wait(submitted["id"], timeout=10)
    report = repo.get_matching_report(
        user["id"], resume["id"], 1
    )

    assert result["status"] == "DONE", result.get("error")
    assert report["ats_score"] == Decimal(str(result["result"]["ats_score"]))
    assert result["result"]["external_research"]["status"] == "provider_unavailable"
    assert result["result"]["matching_report"]["overall_match_score"] is not None


def test_research_cache_persists_in_postgres_and_avoids_repeat_search(core):
    class CountingProvider:
        name = "cache-test"

        def __init__(self):
            self.calls = 0

        def search(self, query, limit):
            self.calls += 1
            return [{
                "title": "Cache test source",
                "url": "https://research-cache.example.test/role",
                "snippet": "A test-only source.",
                "source_domain": "research-cache.example.test",
                "published_at": None,
                "retrieved_at": "2026-11-15T00:00:00+00:00",
                "source_classification": "secondary_source",
                "evidence_type": "search_provider_snippet",
            }]

        def close(self):
            pass

    from placex.research import ResearchService

    provider = CountingProvider()
    service = ResearchService(provider, repo=core.repo, cache_ttl_seconds=60)
    query = f"cache persistence regression {time.monotonic_ns()}"

    first = service.search(query)
    second = service.search(query)

    assert first["status"] == "live_search"
    assert second["status"] == "cached"
    assert second["sources"][0]["url"] == first["sources"][0]["url"]
    assert provider.calls == 1


@pytest.fixture
def client(cfg):
    api.core = PlaceXCore(cfg, probe=lambda: (1, 1, 1)).start()
    yield TestClient(api.app)
    api.core.stop()
    api.core = None
