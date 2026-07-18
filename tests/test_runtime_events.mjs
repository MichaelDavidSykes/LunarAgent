import assert from "node:assert/strict";
import test from "node:test";

import {
  failureCode,
  safeToolError,
  timingFor,
} from "../codex_runtime/runtime_events.mjs";

test("failure codes distinguish delegated auth, stale turns, limits, and timeouts", () => {
  assert.equal(failureCode("HTTP 401 unauthorized"), "delegated_token_rejected");
  assert.equal(failureCode("Explorer turn is not active (409)"), "graph_session_stale");
  assert.equal(failureCode("HTTP 429 too many requests"), "tool_rate_limited");
  assert.equal(failureCode("operation timed out"), "tool_timeout");
  assert.equal(failureCode("unexpected failure"), "tool_failed");
});

test("safe tool errors never expose raw error text or credentials", () => {
  const safe = safeToolError({
    error: {
      message: "HTTP 401 Authorization: Bearer smoke-super-secret",
    },
  });

  assert.deepEqual(safe, {
    errorCode: "delegated_token_rejected",
    error: "The delegated tool authorization was rejected.",
  });
  assert.equal(JSON.stringify(safe).includes("smoke-super-secret"), false);
});

test("tool timing is stable across updates and removed after completion", () => {
  const timings = new Map();
  assert.deepEqual(timingFor(timings, "tool-1", "started", 1_000), {
    startedAt: "1970-01-01T00:00:01.000Z",
    updatedAt: "1970-01-01T00:00:01.000Z",
    durationMs: 0,
  });
  assert.deepEqual(timingFor(timings, "tool-1", "updated", 1_350), {
    startedAt: "1970-01-01T00:00:01.000Z",
    updatedAt: "1970-01-01T00:00:01.350Z",
    durationMs: 350,
  });
  assert.deepEqual(timingFor(timings, "tool-1", "completed", 1_900), {
    startedAt: "1970-01-01T00:00:01.000Z",
    updatedAt: "1970-01-01T00:00:01.900Z",
    durationMs: 900,
  });
  assert.equal(timings.has("tool-1"), false);
});
