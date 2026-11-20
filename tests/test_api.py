import time
import pytest
from fastapi.testclient import TestClient
from placex import api, db
from placex.core import PlaceXCore

@pytest.fixture
def client(cfg):
    from tests.conftest import TEST_DSN
    api.core = PlaceXCore(cfg, probe=lambda: (1, 1, 1)).start()
    yield TestClient(api.app)
    with db.transaction() as conn:
        conn.execute("DELETE FROM job_descriptions WHERE id > 3")
        conn.execute(
            """DELETE FROM companies c WHERE c.name = 'Custom job description'
               AND NOT EXISTS (SELECT 1 FROM job_descriptions j WHERE j.company_id = c.id)"""
        )
    api.core.stop()


def signup(client, email, name):
    response = client.post("/users", json={
        "email": email, "full_name": name, "password": "correct horse battery staple"
    })
    assert response.status_code == 201, response.text
    session = response.json()
    session["id"] = session["user_id"]
    client.headers.update({"Authorization": "Bearer " + session["access_token"]})
    return session


def wait_for_job(client, job_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] in ("DONE", "FAILED", "CANCELLED"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish before timeout")


def test_password_auth_and_jd_endpoints(client):
    assert client.get("/auth/me").status_code == 401
    assert "/users/by-email" not in api.app.openapi()["paths"]
    assert client.post("/auth/login", json={
        "email": "missing@example.test", "password": "correct horse battery staple"
    }).status_code == 401

    session = signup(client, "known@example.test", "Known")
    assert client.get("/auth/me").json() == {
        "id": session["user_id"], "email": "known@example.test", "full_name": "Known", "role": "student"
    }
    assert client.post("/auth/login", json={
        "email": "known@example.test", "password": "wrong password"
    }).status_code == 401
    login = client.post("/auth/login", json={
        "email": "known@example.test", "password": "correct horse battery staple"
    })
    assert login.status_code == 200
    client.headers.update({"Authorization": "Bearer " + login.json()["access_token"]})

    duplicate = client.post("/users", json={
        "email": "known@example.test", "full_name": "Changed name", "password": "correct horse battery staple"
    })
    assert duplicate.status_code == 409
    assert client.get(f"/users/{session['user_id']}/dashboard").json()["full_name"] == "Known"

    existing = client.get("/job-descriptions")
    assert existing.status_code == 200 and len(existing.json()) >= 3

    text = "Backend Developer Intern\nAcme Technologies\nPython, SQL, Git"
    created = client.post("/job-descriptions", data={"text": text})
    assert created.status_code == 201
    assert created.json()["company_matched"] is True
    assert {"Python", "SQL", "Git"}.issubset(created.json()["skills"])
    assert client.post("/job-descriptions", data={"text": text}).json()["id"] == created.json()["id"]
    assert created.json()["id"] in {item["id"] for item in client.get("/job-descriptions").json()}

    uploaded = client.post(
        "/job-descriptions",
        files={"file": ("role.txt", b"Backend Developer\nPython and Docker", "text/plain")},
    )
    assert uploaded.status_code == 201 and "Docker" in uploaded.json()["skills"]
    assert uploaded.json()["company_matched"] is False
    rejected = client.post("/job-descriptions", data={"text": "A collaborative and welcoming team."})
    assert rejected.status_code == 422 and "no known skills" in rejected.json()["detail"]
    assert client.post("/job-descriptions", files={"file": ("bad.exe", b"MZ", "application/octet-stream")}).status_code == 415
    assert client.post("/auth/logout").status_code == 200
    assert client.get("/auth/me").status_code == 401


def test_blank_signup_name_is_rejected_before_database_write(client):
    response = client.post("/users", json={
        "email": "blank-name@example.test",
        "full_name": "   ",
        "password": "correct horse battery staple",
    })

    assert response.status_code == 422
    assert "full_name must not be blank" in response.text


def test_cors_headers_are_preserved_on_server_errors(client, monkeypatch):
    def fail_registration(*args, **kwargs):
        raise RuntimeError("simulated registration failure")

    monkeypatch.setattr(api.core.repo, "register_user", fail_registration)
    browser = TestClient(api.app, raise_server_exceptions=False)
    response = browser.post(
        "/users",
        json={
            "email": "cors-error@example.test",
            "full_name": "Test User",
            "password": "correct horse battery staple",
        },
        headers={"Origin": "http://127.0.0.1:5500"},
    )

    assert response.status_code == 500
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5500"


def test_health_reports_backend_and_database_separately_and_recovers(client, monkeypatch):
    browser = TestClient(api.app)
    origin = {"Origin": "http://127.0.0.1:5500"}
    available = browser.get("/health", headers=origin)
    assert available.status_code == 200
    assert available.json() == {
        "status": "ok",
        "backend": "available",
        "database": "available",
    }
    assert available.headers["access-control-allow-origin"] == origin["Origin"]
    live = browser.get("/health/live", headers=origin)
    assert live.status_code == 200
    assert live.json() == {"status": "ok", "backend": "available"}
    ready = browser.get("/health/ready", headers=origin)
    assert ready.status_code == 200 and ready.json()["database"] == "available"

    state = {"available": False}

    def database_status():
        if not state["available"]:
            raise db.DatabaseUnavailable("test database outage")

    monkeypatch.setattr(api.db, "check_connection", database_status)
    unavailable = browser.get("/health", headers=origin)
    assert unavailable.status_code == 503
    assert unavailable.json() == {
        "status": "degraded",
        "backend": "available",
        "database": "unavailable",
    }
    assert unavailable.headers["access-control-allow-origin"] == origin["Origin"]
    live_during_outage = browser.get("/health/live", headers=origin)
    assert live_during_outage.status_code == 200
    ready_during_outage = browser.get("/health/ready", headers=origin)
    assert ready_during_outage.status_code == 503
    assert ready_during_outage.json()["database"] == "unavailable"

    state["available"] = True
    restored = browser.get("/health", headers=origin)
    assert restored.status_code == 200
    assert restored.json()["database"] == "available"


def test_database_unavailable_is_a_safe_structured_api_error(client, monkeypatch):
    def unavailable():
        raise db.DatabaseUnavailable("private database connection detail")

    monkeypatch.setattr(api.core.repo, "list_companies", unavailable)
    response = TestClient(api.app).get(
        "/companies", headers={"Origin": "http://127.0.0.1:5500"}
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "The PlaceX database is temporarily unavailable. Please retry shortly.",
        "code": "database_unavailable",
    }
    assert "private database connection detail" not in response.text
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5500"


def test_database_diagnostics_endpoint_reports_safe_runtime_state(client, monkeypatch):
    diagnostic = {
        "configuration": "loaded",
        "hostname_resolution": "failed",
        "connection": "unavailable",
        "ssl": "not_checked",
        "query": "not_checked",
        "failure_category": "dns",
    }
    monkeypatch.setattr(api.db, "diagnose_connection", lambda dsn: diagnostic)

    response = TestClient(api.app).get("/health/database")

    assert response.status_code == 503
    assert response.json() == diagnostic
    assert "dsn" not in response.text.casefold()
    assert "password" not in response.text.casefold()


def test_empty_company_suggestions_are_successful(client, monkeypatch):
    monkeypatch.setattr(api.core.repo, "list_companies", lambda: [])
    response = TestClient(api.app).get("/companies")

    assert response.status_code == 200
    assert response.json() == []


def test_session_cannot_access_another_users_data(client):
    owner = signup(client, "owner@example.test", "Owner")
    submitted = client.post("/jobs", json={
        "user_id": owner["user_id"], "job_type": "JOURNEY_GUIDE", "payload": {"company_id": 1}
    })
    assert submitted.status_code == 202
    outsider = client.post("/users", json={
        "email": "outsider@example.test", "full_name": "Outsider", "password": "correct horse battery staple"
    }).json()
    client.headers.update({"Authorization": "Bearer " + outsider["access_token"]})
    assert client.get(f"/users/{owner['user_id']}/dashboard").status_code == 403
    assert client.get(f"/jobs/{submitted.json()['id']}").status_code == 404
    assert client.post("/jobs", json={
        "user_id": owner["user_id"], "job_type": "JOURNEY_GUIDE", "payload": {"company_id": 1}
    }).status_code == 403


def test_job_discovery_recommendations_and_application_lifecycle(client):
    from placex.job_sources import JobListing

    user = signup(client, "placement-tracker@example.test", "Placement Tracker")
    resume = api.core.repo.add_resume(
        user["id"], "UPLOADED", "resume.txt", None, None, 45,
        "Python developer. Built backend API services in a course project.",
    )

    class FakeProvider:
        name = "fixture-job-source"

        def search(self, filters):
            return [JobListing(
                external_id="backend-intern-01",
                source_url="https://careers.example/jobs/backend-intern-01",
                company_name="Fixture Source Company",
                role_title="Backend Engineer Intern",
                description="Build backend services. Required skills: Python, SQL, Docker.",
                location="Remote",
                employment_type="Internship",
                experience_level="Entry-level",
                skills=["Python", "SQL", "Docker"],
                is_remote=True,
                expires_at=None,
            )]

        def close(self):
            pass

    api.core.job_provider = FakeProvider()
    try:
        discovered = client.post("/job-discovery/search", json={
            "query": "Backend", "remote": True,
        })
        assert discovered.status_code == 200, discovered.text
        data = discovered.json()
        assert data["provider_configured"] is True
        assert data["fetched"] == 1
        listing = data["jobs"][0]
        assert listing["is_verified"] is False
        assert listing["company_id"] is not None
        assert listing["jd_id"] is not None
        assert listing["source_url"] == "https://careers.example/jobs/backend-intern-01"

        # A repeated source response updates the same listing instead of duplicating it.
        repeated = client.post("/job-discovery/search", json={"query": "Backend"})
        assert repeated.status_code == 200
        assert len(repeated.json()["jobs"]) == 1

        recommendations = client.get(f"/users/{user['id']}/recommendations")
        assert recommendations.status_code == 200
        match = recommendations.json()["recommendations"][0]
        assert match["id"] == listing["id"]
        assert match["resume_id"] == resume["id"]
        assert match["recommendation_id"] > 0
        assert match["category"] in {"STRONG_MATCH", "ACHIEVABLE_MATCH", "SKILL_BUILDING"}
        assert match["reasons"]
        assert "Docker" in match["missing_skills"]

        preferences = client.put(f"/users/{user['id']}/job-preferences", json={
            "preferred_roles": ["Backend"],
            "preferred_locations": ["Remote"],
            "remote_preference": "REMOTE",
            "employment_types": ["Internship"],
            "experience_level": "Entry-level",
        })
        assert preferences.status_code == 200
        assert len(client.get(f"/users/{user['id']}/recommendations").json()["recommendations"]) == 1
        client.put(f"/users/{user['id']}/job-preferences", json={
            "preferred_roles": ["Data Scientist"],
            "preferred_locations": [],
            "remote_preference": "ANY",
            "employment_types": [],
            "experience_level": None,
        })
        assert client.get(f"/users/{user['id']}/recommendations").json()["recommendations"] == []

        saved = client.post(f"/users/{user['id']}/applications", json={
            "external_job_id": listing["id"],
        })
        assert saved.status_code == 201, saved.text
        application_id = saved.json()["id"]
        updated = client.patch(f"/applications/{application_id}", json={
            "status": "APPLIED",
            "related_resume_id": resume["id"],
            "notes": "Applied through the source career page.",
        })
        assert updated.status_code == 200, updated.text
        assert updated.json()["status"] == "APPLIED"
        assert updated.json()["applied_at"] is not None

        history = client.get(f"/applications/{application_id}/events")
        assert history.status_code == 200
        assert [event["event_type"] for event in history.json()] == [
            "created", "status_changed",
        ]
        assert client.patch(f"/applications/{application_id}", json={
            "status": "SAVED",
        }).status_code == 409
        assert client.get(f"/users/{user['id']}/placement-dashboard").json()[
            "statistics"
        ]["applied"] == 1

        dismissed = client.post(
            f"/users/{user['id']}/recommendations/{match['recommendation_id']}/dismiss"
        )
        assert dismissed.status_code == 200
        client.put(f"/users/{user['id']}/job-preferences", json={
            "preferred_roles": [],
            "preferred_locations": [],
            "remote_preference": "ANY",
            "employment_types": [],
            "experience_level": None,
        })
        assert client.get(f"/users/{user['id']}/recommendations").json()["recommendations"] == []
    finally:
        with db.transaction() as conn:
            conn.execute("DELETE FROM users WHERE id=%s", (user["id"],))
            conn.execute(
                "DELETE FROM external_jobs WHERE source_name=%s",
                ("fixture-job-source",),
            )
            conn.execute(
                "DELETE FROM companies WHERE name=%s",
                ("Fixture Source Company",),
            )


def test_matched_jd_uses_configured_hiring_rounds(client):
    user = signup(client, "rounds@example.test", "Rounds")
    jd = client.post(
        "/job-descriptions",
        data={"text": "Software Engineer\nAcme Technologies\nPython, SQL and Git"},
    ).json()
    submitted = client.post("/jobs", json={
        "user_id": user["id"], "job_type": "JOURNEY_GUIDE", "payload": {"jd_id": jd["id"]}
    }).json()
    for _ in range(100):
        job = client.get(f"/jobs/{submitted['id']}").json()
        if job["status"] in ("DONE", "FAILED"):
            break
        time.sleep(0.05)
    assert job["status"] == "DONE", job.get("error")
    assert [round_["no"] for round_ in job["result"]["rounds"]] == [1, 2, 3]
    assert job["result"]["company_name"] == "Acme Technologies (sample)"
    assert job["result"]["role_title"] == "Software Engineer"
    assert job["result"]["data_available"] is True


def test_journey_guide_returns_an_empty_state_when_rounds_are_unavailable(client):
    user = signup(client, "no-rounds@example.test", "No Rounds")
    with db.transaction() as conn:
        company = conn.execute(
            "INSERT INTO companies(name) VALUES (%s) RETURNING id",
            ("Company without rounds",),
        ).fetchone()
    submitted = client.post("/jobs", json={
        "user_id": user["id"], "job_type": "JOURNEY_GUIDE",
        "payload": {"company_id": company["id"]},
    }).json()

    for _ in range(100):
        job = client.get(f"/jobs/{submitted['id']}").json()
        if job["status"] in ("DONE", "FAILED"):
            break
        time.sleep(0.05)

    assert job["status"] == "DONE", job.get("error")
    assert job["result"]["data_available"] is False
    assert job["result"]["rounds"] == []
    assert "No hiring-round information" in job["result"]["note"]


def test_get_companies_returns_database_records(client):
    response = client.get("/companies")

    assert response.status_code == 200, response.text
    companies = response.json()
    assert companies
    assert all({"id", "name"} <= company.keys() for company in companies)


def test_company_hiring_rounds_load_through_journey_job(client):
    user = signup(client, "journey-flow@example.test", "Journey Flow")
    response = client.post("/jobs", json={
        "user_id": user["id"],
        "job_type": "JOURNEY_GUIDE",
        "payload": {"jd_id": 1},
    })
    assert response.status_code == 202, response.text

    job = wait_for_job(client, response.json()["id"])

    assert job["status"] == "DONE", job.get("error")
    assert job["result"]["company_id"] > 0
    assert job["result"]["rounds"]
    assert all("data_classification" in round_ for round_ in job["result"]["rounds"])


def test_prep_plan_generation_and_saved_roadmaps_api_flow(client):
    user = signup(client, "prep-flow@example.test", "Prep Flow")
    upload = client.post(
        f"/users/{user['id']}/resumes",
        files={"file": ("resume.txt", b"Python SQL", "text/plain")},
    )
    assert upload.status_code == 201, upload.text

    analysis_request = client.post("/jobs", json={
        "user_id": user["id"],
        "job_type": "RESUME_ANALYSIS",
        "payload": {
            "resume_id": upload.json()["id"],
            "jd_id": 1,
        },
    })
    assert analysis_request.status_code == 202, analysis_request.text
    analysis = wait_for_job(client, analysis_request.json()["id"])
    assert analysis["status"] == "DONE", analysis.get("error")
    report_id = analysis["result"]["report_id"]

    plan_request = client.post("/jobs", json={
        "user_id": user["id"],
        "job_type": "PREP_PLAN",
        "payload": {"report_id": report_id},
    })
    assert plan_request.status_code == 202, plan_request.text
    plan = wait_for_job(client, plan_request.json()["id"])

    assert plan["status"] == "DONE", plan.get("error")
    assert plan["result"]["roadmap_id"]
    assert plan["result"]["items"]

    saved = client.get(f"/users/{user['id']}/roadmaps")
    assert saved.status_code == 200, saved.text
    roadmaps = saved.json()
    assert len(roadmaps) == 1
    assert roadmaps[0]["id"] == plan["result"]["roadmap_id"]
    assert roadmaps[0]["items"]
    assert all(item["status"] == "TODO" for item in roadmaps[0]["items"])


def test_schema_mismatch_returns_actionable_safe_error(client, monkeypatch, caplog):
    from psycopg.errors import UndefinedColumn

    user = signup(client, "schema-error@example.test", "Schema Error")

    def missing_column(*args, **kwargs):
        raise UndefinedColumn("internal_column_name")

    monkeypatch.setattr(api.core.repo, "list_job_descriptions", missing_column)
    response = client.get("/job-descriptions")

    assert response.status_code == 503
    assert "Apply the documented additive migrations" in response.json()["detail"]
    assert "internal_column_name" not in response.text
    assert "Database schema mismatch" in caplog.text
    assert user["id"] > 0


def test_failed_job_does_not_expose_internal_errors_to_the_client(client, monkeypatch):
    user = signup(client, "failed-job-error@example.test", "Failed Job Error")
    monkeypatch.setattr(api.core.repo, "get_job", lambda job_id: {
        "id": job_id,
        "user_id": user["id"],
        "status": "FAILED",
        "error": "UndefinedColumn: internal_column_name",
    })

    response = client.get("/jobs/999")

    assert response.status_code == 200
    assert response.json()["error"] == (
        "The job failed because of a server error. Please try again later."
    )
    assert "internal_column_name" not in response.text


def test_saved_roadmaps_and_item_progress_are_owner_scoped(client):
    user = signup(client, "roadmap-owner@example.test", "Roadmap Owner")
    resume = api.core.repo.add_resume(
        user["id"], "UPLOADED", "resume.txt", None, None, 12, "Python SQL",
    )
    report = api.core.repo.save_report(
        user["id"], resume["id"], 1, None, 50, [],
    )
    roadmap = api.core.repo.create_roadmap(
        user["id"], 1, report["id"], "Prep plan: Backend Developer Intern",
        [(1, None, 8), (7, None, 4)],
    )
    with db.transaction() as conn:
        item_ids = conn.execute(
            "SELECT id FROM roadmap_items WHERE roadmap_id=%s ORDER BY position",
            (roadmap["id"],),
        ).fetchall()

    response = client.get(f"/users/{user['id']}/roadmaps")
    assert response.status_code == 200
    saved = response.json()[0]
    assert saved["id"] == roadmap["id"]
    assert saved["role_title"] == "Backend Developer Intern"
    assert saved["total_hours"] == 12
    assert saved["remaining_hours"] == 12
    assert saved["completion_pct"] == 0
    assert [item["status"] for item in saved["items"]] == ["TODO", "TODO"]

    updated = client.patch(
        f"/roadmap-items/{item_ids[0]['id']}",
        json={"status": "IN_PROGRESS"},
    )
    assert updated.status_code == 200
    assert updated.json()["status"] == "IN_PROGRESS"
    updated = client.patch(
        f"/roadmap-items/{item_ids[0]['id']}",
        json={"status": "DONE"},
    )
    assert updated.status_code == 200
    assert updated.json()["completed_at"] is not None

    saved = client.get(f"/users/{user['id']}/roadmaps").json()[0]
    assert saved["completed_items"] == 1
    assert saved["completion_pct"] == 50
    assert saved["remaining_hours"] == 4

    other = signup(client, "roadmap-other@example.test", "Other User")
    assert client.get(f"/users/{user['id']}/roadmaps").status_code == 403
    assert client.patch(
        f"/roadmap-items/{item_ids[0]['id']}",
        json={"status": "TODO"},
    ).status_code == 404
    assert other["id"] != user["id"]


def test_resume_and_matching_analysis_endpoints_are_owner_scoped(client):
    owner = signup(client, "resume-list-owner@example.test", "Resume Owner")
    resume = client.post(
        f"/users/{owner['id']}/resumes",
        files={"file": ("resume.txt", b"Python SQL PostgreSQL", "text/plain")},
    ).json()
    assert resume["has_text"] is True
    assert not {"raw_text", "file_path", "file_hash"}.intersection(resume)

    listed = client.get(f"/users/{owner['id']}/resumes")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [resume["id"]]
    assert "raw_text" not in listed.json()[0]
    assert "file_path" not in listed.json()[0]
    assert client.get(f"/users/{owner['id']}/matching-analysis", params={
        "resume_id": resume["id"], "jd_id": 1,
    }).json() is None

    report = api.core.repo.save_report(
        owner["id"], resume["id"], 1, None, 50, [],
    )
    matching = client.get(f"/users/{owner['id']}/matching-analysis", params={
        "resume_id": resume["id"], "jd_id": 1,
    })
    assert matching.status_code == 200
    assert matching.json()["id"] == report["id"]

    outsider = signup(client, "resume-list-outsider@example.test", "Resume Outsider")
    assert client.get(f"/users/{owner['id']}/resumes").status_code == 403
    assert client.get(f"/users/{owner['id']}/matching-analysis", params={
        "resume_id": resume["id"], "jd_id": 1,
    }).status_code == 403
    assert client.get(f"/users/{outsider['id']}/matching-analysis", params={
        "resume_id": resume["id"], "jd_id": 1,
    }).status_code == 404


def test_jd_analysis_general_mode_and_resume_build(client):
    user = signup(client, "pipeline@example.test", "Pipeline")
    resume = client.post(
        f"/users/{user['id']}/resumes",
        files={"file": ("cv.txt", b"Python SQL Git Docker", "text/plain")},
    ).json()
    jd = client.post("/job-descriptions", data={"text": "Platform Engineer\nPython, SQL and Docker"}).json()

    def wait_for(job_id):
        for _ in range(100):
            current = client.get(f"/jobs/{job_id}").json()
            if current["status"] in ("DONE", "FAILED"):
                return current
            time.sleep(0.05)
        raise AssertionError(f"job {job_id} did not finish")

    general = client.post("/jobs", json={
        "user_id": user["id"], "job_type": "RESUME_ANALYSIS",
        "payload": {"resume_id": resume["id"], "mode": "GENERAL"},
    }).json()
    general_result = wait_for(general["id"])
    assert general_result["status"] == "DONE", general_result.get("error")
    assert general_result["result"]["mode"] == "GENERAL"
    assert general_result["result"]["report_id"] is None
    assert general_result["result"]["score_explanation"]
    assert len(general_result["result"]["score_evidence"]) == 5

    comparison = client.post("/jobs", json={
        "user_id": user["id"], "job_type": "RESUME_ANALYSIS",
        "payload": {"resume_id": resume["id"], "jd_id": jd["id"]},
    }).json()
    analysis = wait_for(comparison["id"])
    assert analysis["status"] == "DONE", analysis.get("error")
    assert analysis["result"]["report_id"]
    assert analysis["result"]["gap"]
    assert analysis["result"]["matching_report"]["score_explanation"]

    build = client.post("/jobs", json={
        "user_id": user["id"], "job_type": "RESUME_BUILD",
        "payload": {"jd_id": jd["id"], "profile": {"name": "Pipeline", "skills": ["Python"]}},
    }).json()
    generated = wait_for(build["id"])
    assert generated["status"] == "DONE", generated.get("error")
    assert generated["result"]["resume_id"]


def test_generated_resume_download_is_authenticated_and_confined(client, tmp_path):
    owner = signup(client, "download-owner@example.test", "Download Owner")
    owner_token = client.headers["Authorization"]
    markdown = "# Generated Resume\n\n## Professional Summary\nStudent targeting a role.\n"
    generated_file = api.core.store.write_generated(owner["id"], "resume", markdown)
    from placex.resume_pdf import generate_resume_pdf
    generated_pdf = api.core.store.write_generated_pdf(
        owner["id"], generated_file["sha256"], generate_resume_pdf(markdown)
    )
    resume = api.core.repo.add_resume(
        owner["id"], "GENERATED", "Test resume", generated_file["path"],
        generated_file["sha256"], generated_file["size"], markdown,
    )
    other = client.post("/users", json={
        "email": "download-other@example.test",
        "full_name": "Download Other",
        "password": "correct horse battery staple",
    }).json()

    response = client.get(
        f"/resumes/{resume['id']}/download",
        headers={"Authorization": owner_token},
    )
    assert response.status_code == 200
    assert response.content == markdown.encode()
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.headers["content-disposition"].endswith(f'filename="resume-{resume["id"]}.md"')
    assert client.get(
        f"/resumes/{resume['id']}/download/md",
        headers={"Authorization": owner_token},
    ).content == markdown.encode()
    pdf = client.get(
        f"/resumes/{resume['id']}/download/pdf",
        headers={"Authorization": owner_token},
    )
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-")
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.headers["content-disposition"].endswith(f'filename="resume-{resume["id"]}.pdf"')

    assert TestClient(api.app).get(f"/resumes/{resume['id']}/download").status_code == 401
    assert TestClient(api.app).get(f"/resumes/{resume['id']}/download/pdf").status_code == 401
    assert client.get(
        f"/resumes/{resume['id']}/download",
        headers={"Authorization": "Bearer " + other["access_token"]},
    ).status_code == 404
    assert client.get(
        f"/resumes/{resume['id']}/download/pdf",
        headers={"Authorization": "Bearer " + other["access_token"]},
    ).status_code == 404
    api.core.store.delete(generated_pdf["path"])
    assert client.get(
        "/resumes/99999999/download",
        headers={"Authorization": owner_token},
    ).status_code == 404
    assert client.get(
        f"/resumes/{resume['id']}/download/pdf",
        headers={"Authorization": owner_token},
    ).status_code == 404
    assert client.get(
        f"/resumes/{resume['id']}/download/docx",
        headers={"Authorization": owner_token},
    ).status_code == 404

    outside_file = tmp_path / "outside.md"
    outside_file.write_text("must not be served", encoding="utf-8")
    with db.transaction() as conn:
        conn.execute("UPDATE resumes SET file_path=%s WHERE id=%s", (str(outside_file), resume["id"]))
    traversal = client.get(
        f"/resumes/{resume['id']}/download",
        headers={"Authorization": owner_token},
    )
    assert traversal.status_code == 404
    assert b"must not be served" not in traversal.content
    with db.transaction() as conn:
        conn.execute(
            "UPDATE resumes SET file_hash=%s WHERE id=%s",
            ("../outside", resume["id"]),
        )
    assert client.get(
        f"/resumes/{resume['id']}/download/pdf",
        headers={"Authorization": owner_token},
    ).status_code == 404


def test_http_flow(client):
    u = signup(client, "h@x.in", "H")
    r = client.post(f"/users/{u['id']}/resumes", files={"file": ("cv.txt", b"Python SQL Git", "text/plain")})
    assert r.status_code == 201
    j = client.post("/jobs", json={"user_id": u["id"], "job_type": "RESUME_ANALYSIS",
                                   "payload": {"resume_id": r.json()["id"], "jd_id": 1}, "priority": 7})
    assert j.status_code == 202
    for _ in range(100):
        s = client.get(f"/jobs/{j.json()['id']}").json()
        if s["status"] in ("DONE", "FAILED"): break
        time.sleep(0.05)
    assert s["status"] == "DONE" and s["result"]["ats_score"] > 0
    assert client.get(f"/users/{u['id']}/dashboard").json()["reports"] == 1
    assert client.get("/system").json()["pool"]["completed"] >= 1

def test_http_errors(client):
    u = signup(client, "e@x.in", "E")
    assert client.post(f"/users/{u['id']}/resumes", files={"file": ("a.exe", b"MZ", "x")}).status_code == 415
    assert client.post("/jobs", json={"user_id": u["id"], "job_type": "NOPE"}).status_code == 422
    assert client.get("/jobs/99999").status_code == 404

def test_http_503_under_backpressure(cfg):
    cfg.queue_capacity = 1
    api.core = PlaceXCore(cfg, probe=lambda: (1, 1, 1))        # not started -> queue never drains
    try:
        c = TestClient(api.app)
        u = signup(c, "b@x.in", "B")
        body = {"user_id": u["id"], "job_type": "PREP_PLAN"}
        assert c.post("/jobs", json=body).status_code == 202
        r = c.post("/jobs", json=body)
        assert r.status_code == 503 and r.headers["retry-after"] == "2"
    finally:
        api.core.stop()
