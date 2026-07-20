import assert from "node:assert/strict";
import test from "node:test";
import {
  claimGraphToolBudget,
  graphRetryDelayMs,
  MAX_GRAPH_HTTP_ATTEMPTS,
  shouldRetryGraphHttpStatus,
} from "../codex_runtime/graph_tool_policy.mjs";

test("logical graph tool budgets are per tool and fail closed", () => {
  const counts = new Map();
  for (let index = 0; index < 4; index += 1) {
    assert.equal(
      claimGraphToolBudget(counts, "search-intelligence-graph").allowed,
      true,
    );
  }
  assert.deepEqual(
    claimGraphToolBudget(counts, "search-intelligence-graph"),
    { allowed: false, limit: 4, used: 4 },
  );
  assert.deepEqual(
    claimGraphToolBudget(counts, "graph-schema"),
    { allowed: true, limit: 2, used: 1 },
  );
  for (let index = 0; index < 4; index += 1) {
    assert.equal(
      claimGraphToolBudget(counts, "get-graph-entity-neighborhood").allowed,
      true,
    );
  }
  assert.deepEqual(
    claimGraphToolBudget(counts, "get-graph-entity-neighborhood"),
    { allowed: false, limit: 4, used: 4 },
  );
  assert.deepEqual(
    claimGraphToolBudget(counts, "unknown-tool"),
    { allowed: true, limit: 1, used: 1 },
  );
  assert.equal(claimGraphToolBudget(counts, "unknown-tool").allowed, false);
});

test("only one bounded retry is allowed for transient read-only responses", () => {
  assert.equal(MAX_GRAPH_HTTP_ATTEMPTS, 2);
  for (const status of [429, 502, 503, 504]) {
    assert.equal(shouldRetryGraphHttpStatus(status, 0), true);
    assert.equal(shouldRetryGraphHttpStatus(status, 1), false);
  }
  for (const status of [400, 401, 403, 404, 409, 500]) {
    assert.equal(shouldRetryGraphHttpStatus(status, 0), false);
  }
});

test("retry-after delays are honored within a tight bound", () => {
  assert.equal(graphRetryDelayMs("0"), 250);
  assert.equal(graphRetryDelayMs("1.5"), 1500);
  assert.equal(graphRetryDelayMs("99"), 2000);
  assert.equal(graphRetryDelayMs("invalid"), 350);
  assert.equal(
    graphRetryDelayMs("Thu, 01 Jan 2026 00:00:01 GMT", Date.parse("2026-01-01T00:00:00Z")),
    1000,
  );
});
