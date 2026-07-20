export const GRAPH_TOOL_LIMITS = Object.freeze({
  "graph-schema": 2,
  "search-intelligence-graph": 4,
  "run-graph-read-query": 4,
  "get-graph-report": 6,
  "get-graph-entity-neighborhood": 4,
});

export const MAX_GRAPH_HTTP_ATTEMPTS = 2;
export const TRANSIENT_GRAPH_HTTP_STATUSES = new Set([429, 502, 503, 504]);

function safeCount(value) {
  const count = Number(value);
  return Number.isSafeInteger(count) && count >= 0 ? count : 0;
}

export function claimGraphToolBudget(callCounts, tool) {
  if (!(callCounts instanceof Map)) {
    throw new TypeError("Graph tool call counts must be a Map");
  }
  const normalizedTool = String(tool || "").trim();
  const limit = GRAPH_TOOL_LIMITS[normalizedTool] || 1;
  const used = safeCount(callCounts.get(normalizedTool));
  if (used >= limit) {
    return { allowed: false, limit, used };
  }
  callCounts.set(normalizedTool, used + 1);
  return { allowed: true, limit, used: used + 1 };
}

export function shouldRetryGraphHttpStatus(status, attempt) {
  return (
    Number.isSafeInteger(Number(status)) &&
    TRANSIENT_GRAPH_HTTP_STATUSES.has(Number(status)) &&
    safeCount(attempt) + 1 < MAX_GRAPH_HTTP_ATTEMPTS
  );
}

export function graphRetryDelayMs(retryAfter, nowMs = Date.now()) {
  const raw = String(retryAfter || "").trim();
  let delay = 350;
  if (raw) {
    const seconds = Number(raw);
    if (Number.isFinite(seconds) && seconds >= 0) {
      delay = seconds * 1000;
    } else {
      const retryAt = Date.parse(raw);
      if (Number.isFinite(retryAt)) delay = retryAt - Number(nowMs || 0);
    }
  }
  return Math.max(250, Math.min(Math.round(delay), 2000));
}
