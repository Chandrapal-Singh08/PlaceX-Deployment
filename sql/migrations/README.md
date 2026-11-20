# PostgreSQL migrations

The base schema in `../schema.sql` includes all migrations for a fresh
installation. For an existing database, take a verified backup and apply the
incremental migrations in order:

```sh
psql "$PLACEX_DSN" --set ON_ERROR_STOP=1 --file sql/migrations/001_matching_details.sql
psql "$PLACEX_DSN" --set ON_ERROR_STOP=1 --file sql/migrations/002_jobs_recommendations_applications.sql
psql "$PLACEX_DSN" --set ON_ERROR_STOP=1 --file sql/migrations/003_dynamic_journey_guides.sql
psql "$PLACEX_DSN" --set ON_ERROR_STOP=1 --file sql/migrations/004_research_cache.sql
psql "$PLACEX_DSN" --set ON_ERROR_STOP=1 --file sql/migrations/005_journey_prep_plans.sql
```

All migrations are additive and idempotent. They do not drop tables or delete
student or report data. Migration 004 adds the shared web-research result cache.
Migration 005 allows saved Journey Guides to become progress-tracked Prep Planner
roadmaps without a predefined job-description ID. The second migration marks only records belonging to
companies whose existing names explicitly end in `(sample)` as sample data.
Other existing company and round information is classified as unverified, not
silently treated as employer-verified. Keep the additions during an application
rollback; removing them would discard new data.

Run against a staging clone and back up the target before applying any
migration. Never use `placex.initdb --reset` for an existing database.

For an application rollback, redeploy the previous application version and retain
the additive schema and any data written through the new version. Do not execute a
destructive down migration; removing these objects would discard user data.
