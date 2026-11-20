# PlaceX: Placement preparation platform

PlaceX uses a deterministic keyword backend for skill extraction and ATS baseline
scoring, with optional structured Groq enhancements. The Analyzer also produces
an evidence-backed matching report; unassessed dimensions are excluded from its
weighted overall score rather than treated as failures.

## Run it

```bash
# 1. PostgreSQL 14+ running; create a local role and database with your PostgreSQL
#    administrator. Supply its DSN through PLACEX_DSN instead of storing credentials here.
pip install -r requirements.txt
export PLACEX_DSN=postgresql://placex:placex@localhost:5432/placex   # optional, this is the default

python -m placex.initdb                  # initialize a fresh application database
uvicorn placex.api:app --reload          # API at http://127.0.0.1:8000/docs
```

The API starts in degraded mode if PostgreSQL is temporarily unavailable rather
than blocking unrelated startup. Check `GET http://127.0.0.1:8000/health` for
separate backend and database status; database-dependent routes return HTTP 503
until the database is reachable. The frontend defaults to the matching local
API URL. To use another backend address, set `window.PLACEX_API_BASE_URL` before
loading `js/config.js` and configure that backend's `PLACEX_ALLOWED_ORIGINS`.
`/health/live` reports process liveness without querying PostgreSQL, while
`/health/ready` reports database readiness. Direct `*.supabase.co` connections
use TLS (`sslmode=require`) by default when no SSL mode is configured. Pool
minimum/maximum sizes, connect timeout, startup wait, and reconnection window
can be adjusted with `PLACEX_DB_POOL_MIN_SIZE`,
`PLACEX_DB_POOL_MAX_SIZE`, `PLACEX_DB_CONNECT_TIMEOUT_S`,
`PLACEX_DB_POOL_WAIT_TIMEOUT_S`, and `PLACEX_DB_RECONNECT_TIMEOUT_S`.

`initdb --reset`, `demo_flow.py`, and `benchmark.py` drop and recreate the configured
database schema. Reset is refused unless the database name ends with `_test`, `_demo`,
`_bench`, or `_benchmark`. Keep real development data in `placex` and use disposable
databases for destructive demos:

```bash
# Create placex_demo and placex_benchmark with the same local PostgreSQL owner.
export PLACEX_DSN=postgresql://placex:<local-password>@localhost:5432/placex_demo
python demos/demo_flow.py

export PLACEX_DSN=postgresql://placex:<local-password>@localhost:5432/placex_benchmark
python demos/benchmark.py

python demos/race_demo.py
python -m pytest -q                   # recreates PLACEX_TEST_DSN (default: placex_test)
```

Never set `PLACEX_TEST_DSN` to the application database; the test fixture drops and
recreates its target before running the suite.

## Architecture

```
HTTP (api.py) -> PlaceXCore.submit() -> INSERT jobs (READY) + BoundedJobQueue.put()
                                              |  policy picks next (FCFS / priority / aging / SJF)
                                       WorkerPool threads -> claim_job() (atomic SQL) -> handler
                                              |                                         |
                              ResourceMonitor pauses queue                                               handlers + repo.* transactions
                              when RAM/CPU is high                         -> reports / roadmaps / resumes
```

## Where each concept lives (for your viva)

| Concept | File | What to say |
|---|---|---|
| Producer-consumer, bounded buffer | `os_layer/queue.py` | One mutex, two condition variables (`not_full`, `not_empty`). Full queue = backpressure (HTTP 503). |
| Mutex / race condition | `os_layer/worker.py` (`PoolStats`), `demos/race_demo.py` | All shared counters sit behind one lock. Demo: 13,992 of 16,000 updates lost without it, 0 with it. |
| CPU scheduling + starvation | `os_layer/policies.py` | 4 swappable policies. Aging adds +1 priority per N seconds waited, so nothing starves. |
| Process lifecycle | `jobs` table + `worker.py` | READY -> RUNNING -> DONE / FAILED / CANCELLED, retry with exponential backoff. DB `CHECK`s forbid illegal states. |
| Atomic claim / MVCC | `repo.claim_job` | `UPDATE ... WHERE id=(SELECT ... FOR UPDATE SKIP LOCKED)`: 20 racing threads, exactly 1 winner (tested). |
| Row-level locking | `handlers.resume_analysis` | `SELECT ... FOR UPDATE` on the resume. Found by a failing concurrency test (see below). |
| Memory management | `os_layer/memory.py`, `matching.py` | LRU+TTL cache; optional Sentence Transformers embeddings load lazily once per backend process and remain disabled by default. |
| Admission control | `os_layer/monitor.py` | psutil sampling; pause at 85% RAM, resume at 75% (hysteresis, so it does not flap). |
| File system | `os_layer/filestore.py` | Content-hash dedupe, per-user quota, size/type/magic-byte checks, atomic write (tmp + fsync + rename), path-traversal safe. |
| Crash recovery | `repo.recover_jobs`, `core.start` | RUNNING jobs left by a dead process return to READY and are re-queued on boot. |
| Timeouts | `JobContext.check()` | Cooperative: Python threads cannot be killed safely. |

## Database (21 tables, 3NF)

`users, companies, job_descriptions, skills, resumes, resume_skills, jd_skills, analysis_reports,
hiring_rounds, resources, roadmaps, roadmap_items, jobs, activity_logs, user_sessions,
external_jobs, user_job_preferences, job_recommendations, applications, application_events,
journey_guides`

- **Junction tables** make matched vs missing skills a single `LEFT JOIN` (`fn_skill_gap`), and the ATS
  score an importance-weighted SQL function (`fn_ats_score`).
- `analysis_reports.matching_details` stores the versioned component scores, evidence, and score explanation.
  The original `ats_score` remains unchanged for API compatibility.
- **Triggers:** every job state change is audited into `activity_logs`; `updated_at` and roadmap
  `completed_at` are maintained by the database.
- **View:** `v_student_dashboard` (best/latest ATS, roadmap progress %, pending jobs).
- **Constraints/indexes:** CHECK, UNIQUE, FK cascades, partial index on READY jobs.
- The 20 tables include the `resume_skills` and `jd_skills` junction tables, source listings,
user job preferences, recommendations, applications, and application events.

## Measured results (1-core sandbox)

| Test | Result |
|---|---|
| Tests | Run `python -m pytest -q` for the current DB, API, worker, queue, and resume test suite |
| Race demo | no mutex: 13,992 / 16,000 updates lost; with mutex: 0 |
| Throughput, 60 jobs, 50 ms simulated AI latency | 1 worker 9.7 s, 2 workers 5.1 s (1.9x), 4 workers 3.4 s (2.9x) |
| Policies (urgent stream vs low backlog) | FCFS: urgent wait 1.89 s. Strict priority: urgent 0.63 s but low jobs wait up to 5.65 s (starved). Aging: urgent 1.58 s, low max 2.36 s |

Throughput plateaus at 4 workers **only because this sandbox has 1 CPU core**. Threads overlap *waiting*
(DB, files, API calls); they cannot create CPU. On your laptop re-run `benchmark.py` and report your own numbers.

### A real bug the tests caught
With 40 concurrent analyses of one resume, 13 jobs failed with `UniqueViolation`: two transactions both did
delete-then-insert on `resume_skills` under READ COMMITTED. Fixed by locking the resume row (`FOR UPDATE`),
and the burst test now passes. Worth including in your report: it shows why the DB and OS layers must be designed together.

## AI backend and worker considerations

Implement the three methods of `ai/base.py::AIBackend` (`extract_skills`, `suggest_improvements`,
`build_resume`) and pass it: `PlaceXCore(ai=MyBackend())`. Notes:
- Optional embedding inference is **CPU-bound**; the current worker threads share a lazy model per process. For high CPU concurrency, move embedding inference behind a dedicated model service/process rather than loading one model per worker.
- Groq API calls are I/O-bound, so the current thread pool is the right fit. The separate LLM service uses
  `GROQ_API_KEY` and `GROQ_MODEL`; it is optional and does not control ATS scores.
- If Groq is unavailable or not configured, Journey Guide saves clearly labeled general fallback guidance.
  Logs include the operation, failure stage, category, HTTP status, and safe diagnostic; credentials and
  provider response bodies are not logged. Rate limits and transient provider failures use the configured
  retry policy, and a schema-generation rejection gets one bounded corrective retry.

## Hybrid matching and existing-database upgrade

Exact and catalog-alias skill matching remains the deterministic ATS baseline. Optional non-skill
requirements are extracted as structured Groq suggestions, but only verbatim quotes found in the
job description and resume are accepted as evidence. Missing or unassessed education, experience,
project, and responsibility dimensions are not scored as failures. No confirmed ineligibility is
inferred from absent resume information.

Semantic skill matching uses an injectable embedding-provider interface; no model or embedding
dependency is enabled by default. To adjust the visible, normalized weighted score, set
`PLACEX_MATCH_WEIGHTS` to a JSON object; defaults are listed in `.env.example`.

To enable the optional Sentence Transformers adapter, install `requirements-ml.txt`,
cache the selected model, then set `PLACEX_EMBEDDINGS_ENABLED=true` and
`PLACEX_EMBEDDING_MODEL`. Models load lazily once per backend process. If the optional
package or cached model is unavailable, semantic matching is marked unavailable and the
deterministic ATS path continues to work.

## Job discovery and application tracking

PlaceX does not scrape job boards or synthesize job listings. Configure a permitted JSON
job API or feed with `PLACEX_JOB_PROVIDER_URL`; optionally set
`PLACEX_JOB_PROVIDER_NAME`, `PLACEX_JOB_PROVIDER_API_KEY` and
`PLACEX_JOB_PROVIDER_TIMEOUT_S`. The provider must accept the query filters and return a
JSON object with a `jobs` array of source-supplied records. Each record requires a stable
`external_id`, `source_url`, `company_name`, `role_title`, and `description`; supported
optional fields include `location`, `employment_type`, `experience_level`,
`experience_requirements`, `education_requirements`, `skills`, `is_remote`, `is_verified`,
`status`, and `expires_at`. API keys are sent only over HTTPS. The source URL, provider
name, retrieval time, and source-reported verification time are stored. Listings default
to unverified and are labeled source-reported until a configured source explicitly marks
them verified. Confirm job details at the source before applying.

The authenticated `POST /job-discovery/search` endpoint fetches and upserts current source
results; `GET /job-listings` searches stored active listings. With no provider configured,
search returns stored records and an explicit configuration message. The dashboard adds
recommendations, preferences, saved opportunities, and application status/history. A
listing with a linked job description can be passed directly to Resume Analyzer, Resume
Builder, Prep Planner, or Journey Guide. Recommendations use deterministic evidence and
do not establish eligibility.

Existing databases must apply the additive migrations in the order shown in
[sql/migrations/README.md](sql/migrations/README.md). Migration `002` adds source metadata,
external listings, preferences, recommendation history, application/event tables, and
roadmap practice fields; migration `003` adds private saved Journey Guides. Migrations
are not run automatically and do not drop existing data.

Journey Guide accepts free-text company and role inputs. Database hiring records and
resources are shown with their stored classifications; Groq supplies structured, role-
specific preparation advice, which is always labeled as unverified general guidance.
Generated guides are saved to the authenticated student's account. No live company
hiring-process research is claimed when no approved source is integrated. Submit requests
through authenticated `POST /journey-guides`, poll the returned job at `GET /jobs/{id}`,
and retrieve saved guides from `GET /users/{user_id}/journey-guides` or
`GET /journey-guides/{guide_id}`. `PLACEX_JOURNEY_MAX_OUTPUT_TOKENS` bounds the
structured guide response (default 2400, maximum 4096).

Fresh databases receive `matching_details` from `sql/schema.sql`. For an existing PostgreSQL or
Supabase database, take a backup and follow [sql/migrations/README.md](sql/migrations/README.md)
before deploying this application version. Do not run `placex.initdb --reset` against existing data.
If application rollback is needed, roll back the application release and leave additive
schema and newly written data in place; do not drop the migration's objects. Restore a
pre-migration backup only through the database owner's established recovery procedure.

## Live web research

PlaceX has an optional, provider-neutral research adapter using Brave Search. Groq
does not search the web. To enable actual search, set `PLACEX_RESEARCH_PROVIDER=brave`
and provide `BRAVE_SEARCH_API_KEY` on the backend only. Configure the timeout,
result limit, and cache lifetime with the `PLACEX_RESEARCH_*` variables in
`.env.example`. The default provider is `none`; in that mode, module results explicitly
say live research is unavailable rather than implying Groq browsed the internet.

Search queries contain only the company, role, location/experience where applicable,
and requirement skill names. Resume text, contact details, account identifiers, and
other student profile data are not sent to the search provider. Results retain the
provider title, URL, snippet, domain, publication date when supplied, retrieval time,
and an indicative source classification. A company-domain match is only a candidate,
not independent proof of authenticity. Search snippets are untrusted and may be stale;
PlaceX does not crawl arbitrary pages or bypass robots rules. Users should verify
claims at their linked source.

Journey Guide, Prep Planner, Resume Analyzer, and Resume Builder use web research as
context, with source references shown separately. Research does not set ATS scores
or count as evidence that a student has a skill. Resume rewriting remains constrained
to the student's supplied profile. Results are cached by provider and normalized query
in `research_cache`; expired cache entries may be used as stale context if the provider
is unavailable and are labeled accordingly.

Existing databases must apply the additive `004_research_cache.sql` migration after
checking actual schema state. Fresh installs include the cache table in `schema.sql`.
This migration does not alter existing user, job, or roadmap data.

## Known limitations (be upfront about these)

- Sample companies, rounds and JDs in `seed.sql` are **placeholders**. Replace with verified data.
- Passwords use PBKDF2; API sessions are revocable, hashed bearer tokens. User-specific endpoints enforce session ownership.
- Existing accounts with no password hash require an administrator-provisioned password/reset flow before they can sign in.
- Exact skill extraction is keyword-based (case-insensitive, alias-aware); embeddings are not enabled
  until an embedding model/provider is selected. PDFs need `pip install pypdf`.
- Queue state is in-process; one server process per database. Multi-process would need DB-driven claiming (the `claim_job` SQL already supports it).
- Timeouts are cooperative and a handler must call `ctx.check()`.
- Development schema reset is intentionally limited to explicitly disposable database names.

## Suggested team split

1. **DB:** `sql/`, `repo.py`, `test_database.py`
2. **Scheduler/queue:** `os_layer/queue.py`, `policies.py`, `test_queue.py`, policy benchmark
3. **Workers/monitor/memory/files:** `worker.py`, `monitor.py`, `memory.py`, `filestore.py`, `test_os_layer.py`
4. **API/UI + AI stub:** `api.py`, `ai/`, `tasks/handlers.py`, frontend
