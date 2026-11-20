from contextlib import contextmanager

import psycopg
import pytest
from psycopg_pool import PoolTimeout

from placex import db


def test_connection_pool_recovers_after_temporary_database_failure(monkeypatch, caplog):
    class RecoverablePool:
        available = False
        closed = False

        def wait(self, timeout):
            if not self.available:
                raise PoolTimeout("simulated database outage")

        @contextmanager
        def connection(self):
            if not self.available:
                raise PoolTimeout("simulated database outage")
            yield self

        def execute(self, query):
            assert query == "SELECT 1"
            return self

        def fetchone(self):
            return (1,)

        def close(self):
            self.closed = True

    pool = RecoverablePool()
    db.close_pool()
    monkeypatch.setattr(db, "ConnectionPool", lambda *args, **kwargs: pool)
    try:
        assert db.init_pool(
            "postgresql://localhost/placex_test", wait_timeout=0.01
        ) is pool
        with pytest.raises(db.DatabaseUnavailable):
            db.check_connection()
        assert "database_startup_connection_timeout" in caplog.text

        pool.available = True
        db.check_connection()
        assert not pool.closed
    finally:
        db.close_pool()
    assert pool.closed


def test_direct_supabase_pool_requires_ssl_and_uses_bounded_timeouts(monkeypatch):
    observed = {}

    class Pool:
        def wait(self, timeout):
            observed["wait_timeout"] = timeout

        def close(self):
            pass

    def create_pool(dsn, **kwargs):
        observed["dsn"] = dsn
        observed["kwargs"] = kwargs
        return Pool()

    monkeypatch.setattr(db, "ConnectionPool", create_pool)
    db.close_pool()
    try:
        db.init_pool(
            "postgresql://tester:placeholder@db.nhpuyxnyljxloyfmbovl.supabase.co:5432/postgres",
            min_size=1,
            max_size=6,
            wait_timeout=0.25,
            connect_timeout=4,
            reconnect_timeout=12,
        )
    finally:
        db.close_pool()

    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(observed["dsn"])
    assert info["host"] == "db.nhpuyxnyljxloyfmbovl.supabase.co"
    assert info["port"] == "5432"
    assert info["dbname"] == "postgres"
    assert info["sslmode"] == "require"
    assert observed["kwargs"]["min_size"] == 1
    assert observed["kwargs"]["max_size"] == 6
    assert observed["kwargs"]["kwargs"]["connect_timeout"] == 4
    assert observed["kwargs"]["timeout"] == 0.25
    assert observed["kwargs"]["reconnect_timeout"] == 12
    assert observed["wait_timeout"] == 0.25


def test_database_diagnostics_report_dns_failure_without_sensitive_details(monkeypatch):
    def failed_lookup(*args, **kwargs):
        raise db.socket.gaierror(-2, "private resolver diagnostic")

    monkeypatch.setattr(db.socket, "getaddrinfo", failed_lookup)
    diagnostic = db.diagnose_connection(
        "postgresql://tester:secret@db.example.supabase.co:5432/postgres"
    )

    assert diagnostic == {
        "configuration": "loaded",
        "hostname_resolution": "failed",
        "connection": "unavailable",
        "ssl": "not_checked",
        "query": "not_checked",
        "failure_category": "dns",
    }
    assert "secret" not in repr(diagnostic)
    assert "private resolver diagnostic" not in repr(diagnostic)


def test_database_diagnostics_report_query_and_tls_status(monkeypatch):
    class ConnectionInfo:
        ssl_in_use = True

    class Connection:
        info = ConnectionInfo()

        def execute(self, query):
            assert query == "SELECT 1"
            return self

        def fetchone(self):
            return (1,)

    @contextmanager
    def transaction():
        yield Connection()

    monkeypatch.setattr(db.socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 5432))])
    monkeypatch.setattr(db, "transaction", transaction)

    diagnostic = db.diagnose_connection(
        "postgresql://tester:secret@db.example.supabase.co:5432/postgres"
    )

    assert diagnostic == {
        "configuration": "loaded",
        "hostname_resolution": "resolved",
        "connection": "available",
        "ssl": "enabled",
        "query": "ok",
        "failure_category": None,
    }


@pytest.mark.parametrize(
    ("sqlstate", "message", "expected"),
    [
        ("28P01", "password authentication failed", "authentication"),
        (None, "certificate verify failed", "ssl"),
        (None, "connection timed out", "network"),
        (None, "could not resolve host", "dns"),
    ],
)
def test_database_failure_categories_are_safe(sqlstate, message, expected):
    class Failure(Exception):
        pass

    error = Failure(message)
    error.sqlstate = sqlstate
    assert db._failure_category(error) == expected


def mkuser(conn, email="a@x.in"):
    return conn.execute(
        "INSERT INTO users(email, full_name) VALUES (%s,'A') RETURNING id",
        (email,),
    ).fetchone()["id"]


def test_seed_loaded(conn):
    assert conn.execute("SELECT count(*) AS n FROM skills").fetchone()["n"] >= 30
    assert conn.execute(
        "SELECT count(*) AS n FROM job_descriptions"
    ).fetchone()["n"] == 3


def test_platform_tables_and_relationships_are_present(conn):
    tables = {
        row["table_name"]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE'"
        )
    }
    assert {
        "external_jobs",
        "user_job_preferences",
        "job_recommendations",
        "applications",
        "application_events",
    }.issubset(tables)


def test_research_cache_schema_is_present(conn):
    columns = {
        row["column_name"]
        for row in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='research_cache'"
        )
    }
    assert {
        "query_hash",
        "provider_name",
        "query_text",
        "results",
        "retrieved_at",
        "expires_at",
    }.issubset(columns)


def test_application_requires_exactly_one_job_source(conn):
    user_id = mkuser(conn, "application-source@example.test")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO applications(user_id) VALUES (%s)",
            (user_id,),
        )


def test_email_unique_case_insensitive(conn):
    mkuser(conn, "Bob@x.in")
    with pytest.raises(psycopg.errors.UniqueViolation):
        mkuser(conn, "bob@X.IN")


def test_check_constraints(conn):
    u = mkuser(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO jobs(user_id, job_type, priority) "
            "VALUES (%s,'PREP_PLAN', 99)",
            (u,),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO jd_skills(jd_id, skill_id, importance) "
            "VALUES (1, 3, 9)"
        )


def test_job_state_machine_enforced_by_db(conn):
    u = mkuser(conn)
    jid = conn.execute(
        "INSERT INTO jobs(user_id, job_type) "
        "VALUES (%s,'PREP_PLAN') RETURNING id",
        (u,),
    ).fetchone()["id"]

    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "UPDATE jobs SET status='RUNNING' WHERE id=%s", (jid,)
        )

    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "UPDATE jobs SET status='DONE' WHERE id=%s", (jid,)
        )


def test_foreign_key_cascade(conn):
    u = mkuser(conn)
    conn.execute(
        "INSERT INTO resumes(user_id, kind, title) "
        "VALUES (%s,'UPLOADED','r')",
        (u,),
    )
    conn.execute("DELETE FROM users WHERE id=%s", (u,))
    assert conn.execute(
        "SELECT count(*) AS n FROM resumes"
    ).fetchone()["n"] == 0


def test_trigger_audits_job_lifecycle(conn):
    u = mkuser(conn)
    jid = conn.execute(
        "INSERT INTO jobs(user_id, job_type) "
        "VALUES (%s,'PREP_PLAN') RETURNING id",
        (u,),
    ).fetchone()["id"]

    conn.execute(
        "UPDATE jobs SET status='RUNNING', locked_by='w1' WHERE id=%s",
        (jid,),
    )
    conn.execute(
        "UPDATE jobs SET status='DONE', locked_by=NULL, "
        "finished_at=now() WHERE id=%s",
        (jid,),
    )

    actions = [
        r["action"]
        for r in conn.execute(
            "SELECT action FROM activity_logs "
            "WHERE entity_id=%s ORDER BY id",
            (jid,),
        )
    ]
    assert actions == ["job_created", "job_running", "job_done"]


def _resume_with(conn, u, names):
    rid = conn.execute(
        "INSERT INTO resumes(user_id, kind, title) "
        "VALUES (%s,'UPLOADED','r') RETURNING id",
        (u,),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO resume_skills(resume_id, skill_id) "
        "SELECT %s, id FROM skills WHERE name = ANY(%s)",
        (rid, names),
    )
    return rid


def test_ats_score_function_is_importance_weighted(conn):
    u = mkuser(conn)
    rid = _resume_with(conn, u, ["Python", "SQL"])
    assert float(
        conn.execute(
            "SELECT fn_ats_score(%s,1) AS s", (rid,)
        ).fetchone()["s"]
    ) == round(100 * 10 / 29, 2)

    full = _resume_with(
        conn,
        u,
        [
            "Python", "SQL", "REST APIs", "PostgreSQL",
            "Data Structures", "Git", "Docker", "Linux",
        ],
    )
    assert float(
        conn.execute(
            "SELECT fn_ats_score(%s,1) AS s", (full,)
        ).fetchone()["s"]
    ) == 100

    empty = _resume_with(conn, u, [])
    assert float(
        conn.execute(
            "SELECT fn_ats_score(%s,1) AS s", (empty,)
        ).fetchone()["s"]
    ) == 0


def test_skill_gap_matched_vs_missing(conn):
    u = mkuser(conn)
    rid = _resume_with(conn, u, ["Python"])
    gap = {
        r["skill_name"]: r["matched"]
        for r in conn.execute("SELECT * FROM fn_skill_gap(%s,1)", (rid,))
    }
    assert gap["Python"] is True
    assert gap["SQL"] is False
    assert len(gap) == 8


def test_analysis_reports_store_matching_details_as_jsonb(conn):
    column = conn.execute(
        """SELECT data_type, column_default
           FROM information_schema.columns
           WHERE table_schema='public' AND table_name='analysis_reports'
             AND column_name='matching_details'"""
    ).fetchone()

    assert column["data_type"] == "jsonb"
    assert "{}" in column["column_default"]


def test_transaction_rollback_is_atomic(core, user):
    from placex import db

    r = core.repo.add_resume(
        user["id"], "UPLOADED", "x", None, "a" * 64, 1, "t"
    )
    with pytest.raises(RuntimeError):
        with db.transaction() as c:
            core.repo.set_resume_skills(r["id"], [(1, 1.0)], c)
            raise RuntimeError("boom")

    with db.transaction() as c:
        assert c.execute(
            "SELECT count(*) AS n FROM resume_skills"
        ).fetchone()["n"] == 0


def test_roadmap_item_completion_trigger_and_dashboard_view(core, user):
    core.repo.add_resume(
        user["id"], "UPLOADED", "x", None, "b" * 64, 1, "t"
    )
    rm = core.repo.create_roadmap(
        user["id"], 1, None, "plan", [(1, None, 5), (7, None, 5)]
    )

    from placex import db

    with db.transaction() as c:
        item = c.execute(
            "SELECT id FROM roadmap_items "
            "WHERE roadmap_id=%s ORDER BY position",
            (rm["id"],),
        ).fetchone()

    done = core.repo.set_item_status(item["id"], "DONE")
    assert done["completed_at"] is not None

    d = core.repo.dashboard(user["id"])
    assert d["roadmap_items_total"] == 2
    assert float(d["roadmap_progress_pct"]) == 50.0
    assert d["resumes"] == 1


def test_core_closes_pool_if_auth_schema_initialization_fails(
    cfg, monkeypatch
):
    from placex import core, db

    def fail_schema():
        raise RuntimeError("schema initialization failed")

    monkeypatch.setattr(db, "ensure_auth_schema", fail_schema)

    try:
        with pytest.raises(
            RuntimeError, match="schema initialization failed"
        ):
            core.PlaceXCore(cfg)
        assert db._pool is None
    finally:
        db.close_pool()


def test_schema_reset_rejects_the_development_database():
    from placex.initdb import apply_schema

    with pytest.raises(ValueError, match="Refusing to reset"):
        apply_schema("dbname=placex", reset=True)
