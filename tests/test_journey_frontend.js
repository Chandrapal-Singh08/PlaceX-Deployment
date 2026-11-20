const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

test("Journey Guide API submits arbitrary text and reads the completed job response", async () => {
  const calls = [];
  const guideResult = {
    company_name: "New Company 42",
    job_role: "Climate Risk Analyst",
    guidance_status: "ai_generated",
  };
  const px = {
    session: { token: () => "test-session-token" },
    history: { add: () => {} },
  };
  const window = {
    PLACEX_CONFIG: {
      API_BASE_URL: "http://placex.test",
      POLL_INTERVAL_MS: 0,
      POLL_TIMEOUT_MS: 1000,
    },
    PX: px,
  };
  const sandbox = {
    window,
    PX: px,
    Headers,
    AbortController,
    setTimeout,
    clearTimeout,
    fetch: async (url, options) => {
      calls.push({ url, options });
      if (url.endsWith("/journey-guides")) {
        return {
          ok: true,
          status: 202,
          text: async () => JSON.stringify({
            id: 87,
            job_type: "JOURNEY_GUIDE",
            status: "READY",
          }),
        };
      }
      if (url.endsWith("/jobs/87")) {
        return {
          ok: true,
          status: 200,
          text: async () => JSON.stringify({
            id: 87,
            job_type: "JOURNEY_GUIDE",
            status: "DONE",
            result: guideResult,
          }),
        };
      }
      if (url.endsWith("/journey-guides/15/roadmap")) {
        return {
          ok: true,
          status: 201,
          text: async () => JSON.stringify({
            id: 99,
            created: true,
            prep_url: "/prep-plan.html",
          }),
        };
      }
      throw new Error(`Unexpected request: ${url}`);
    },
  };
  const clientPath = path.join(__dirname, "../frontend/js/api.js");
  vm.runInNewContext(fs.readFileSync(clientPath, "utf8"), sandbox, {
    filename: clientPath,
  });

  const payload = {
    company_name: "New Company 42",
    job_role: "Climate Risk Analyst",
    location: "Anywhere",
    experience_level: null,
    employment_type: null,
    job_description: "Assess climate-related financial exposure.",
  };
  const completedJob = await window.API.generateJourneyGuide(payload);
  const preferences = { daily_study_hours: 3, deadline_days: 15 };
  const roadmap = await window.API.journeyRoadmap(15, preferences);

  assert.equal(calls.length, 3);
  assert.equal(calls[0].url, "http://placex.test/journey-guides");
  assert.equal(calls[0].options.method, "POST");
  assert.deepEqual(JSON.parse(calls[0].options.body), payload);
  assert.equal(calls[0].options.headers.get("Authorization"), "Bearer test-session-token");
  assert.equal(calls[1].url, "http://placex.test/jobs/87");
  assert.equal(completedJob.result.company_name, payload.company_name);
  assert.equal(completedJob.result.job_role, payload.job_role);
  assert.equal(calls[2].url, "http://placex.test/journey-guides/15/roadmap");
  assert.equal(calls[2].options.method, "POST");
  assert.deepEqual(JSON.parse(calls[2].options.body), preferences);
  assert.equal(roadmap.id, 99);
  assert.equal(roadmap.prep_url, "/prep-plan.html");
});

test("Journey Guide does not offer a polling retry for a final failed job", async () => {
  const px = {
    session: { token: () => "test-session-token" },
    history: { add: () => {} },
  };
  const window = {
    PLACEX_CONFIG: {
      API_BASE_URL: "http://placex.test",
      POLL_INTERVAL_MS: 0,
      POLL_TIMEOUT_MS: 1000,
    },
    PX: px,
  };
  const sandbox = {
    window,
    PX: px,
    Headers,
    AbortController,
    setTimeout,
    clearTimeout,
    fetch: async url => ({
      ok: true,
      status: 200,
      text: async () => JSON.stringify(url.endsWith("/journey-guides")
        ? { id: 87, job_type: "JOURNEY_GUIDE", status: "READY" }
        : { id: 87, job_type: "JOURNEY_GUIDE", status: "FAILED", error: "safe failure" }),
    }),
  };
  const clientPath = path.join(__dirname, "../frontend/js/api.js");
  vm.runInNewContext(fs.readFileSync(clientPath, "utf8"), sandbox, {
    filename: clientPath,
  });

  await assert.rejects(
    window.API.generateJourneyGuide({ company_name: "New Company", job_role: "New Role" }),
    error => error.kind === "job" && error.jobId === undefined,
  );
});
