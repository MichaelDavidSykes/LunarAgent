export function failureCode(value, fallback = "tool_failed") {
  const text = String(value || "").toLowerCase();
  if (/\b401\b|\b403\b|unauthori[sz]ed|forbidden|delegated.+token/.test(text)) {
    return "delegated_token_rejected";
  }
  if (/\b409\b|session.+(?:stale|inactive)|turn.+not.+active/.test(text)) {
    return "graph_session_stale";
  }
  if (/\b429\b|rate.?limit|too many requests/.test(text)) {
    return "tool_rate_limited";
  }
  if (/timed?.?out|timeout|abort/.test(text)) {
    return "tool_timeout";
  }
  return fallback;
}

export function safeToolError(item, fallback = "Tool execution failed.") {
  const raw = item?.error?.message || item?.error || "";
  if (!raw) return {};
  const code = failureCode(raw);
  const messages = {
    delegated_token_rejected: "The delegated tool authorization was rejected.",
    graph_session_stale: "The delegated graph session is no longer active.",
    tool_rate_limited: "The tool is temporarily rate limited.",
    tool_timeout: "The tool timed out before completing.",
    tool_failed: fallback,
  };
  return {
    errorCode: code,
    error: messages[code] || fallback,
  };
}

export function timingFor(itemTimings, itemId, stage, now = Date.now()) {
  const key = String(itemId || "unknown");
  if (!itemTimings.has(key)) itemTimings.set(key, now);
  const startedAtMs = itemTimings.get(key);
  const timing = {
    startedAt: new Date(startedAtMs).toISOString(),
    updatedAt: new Date(now).toISOString(),
    durationMs: Math.max(0, now - startedAtMs),
  };
  if (stage === "completed") itemTimings.delete(key);
  return timing;
}
