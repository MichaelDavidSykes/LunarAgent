#!/usr/bin/env node

import { Codex } from "@openai/codex-sdk";
import fs from "node:fs";
import { safePublicUrl } from "./output_guardrails.mjs";

const EXECUTION_POLICY = Object.freeze(
  JSON.parse(
    fs.readFileSync(
      new URL("./execution_policy.json", import.meta.url),
      "utf8",
    ),
  ),
);

if (
  EXECUTION_POLICY.schema !== 1 ||
  EXECUTION_POLICY.mode !== "read-only-no-host-exec" ||
  EXECUTION_POLICY.sandboxMode !== "read-only" ||
  EXECUTION_POLICY.networkAccessEnabled !== false ||
  EXECUTION_POLICY.hostCommands !== false ||
  EXECUTION_POLICY.fileWrites !== false
) {
  throw new Error("Area-risk Codex execution policy is invalid");
}

const MAX_PROMPT_CHARS = 48_000;
const MAX_ZONES = 6;
const MAX_ZONE_RADIUS_M = 2_500;
const MAX_EVIDENCE_AGE_MS = 365 * 24 * 60 * 60 * 1000;
const MAX_FUTURE_EVIDENCE_SKEW_MS = 2 * 24 * 60 * 60 * 1000;
const ALLOWED_RESULT_ITEM_TYPES = new Set([
  "agent_message",
  "reasoning",
  "todo_list",
  "web_search",
]);

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8");
}

function requiredString(value, field, maxChars) {
  const normalized = String(value || "").trim();
  if (!normalized || normalized.length > maxChars) {
    throw new Error(`Invalid area-risk Codex field: ${field}`);
  }
  return normalized;
}

function configPathKey(value) {
  const path = String(value || "");
  if (!path.startsWith("/") || path.split("/").some((part) => part.includes("."))) {
    throw new Error("Area-risk Codex permission paths must be absolute aliases without dotted components");
  }
  return path;
}

function canonicalEvidenceDate(value, nowMs = Date.now()) {
  const raw = String(value || "").trim();
  if (!raw || !/^\d{4}-\d{2}-\d{2}(?:T.*)?$/.test(raw)) return "";
  const timestamp = Date.parse(raw);
  if (
    !Number.isFinite(timestamp) ||
    timestamp < nowMs - MAX_EVIDENCE_AGE_MS ||
    timestamp > nowMs + MAX_FUTURE_EVIDENCE_SKEW_MS
  ) return "";
  return new Date(timestamp).toISOString();
}

function outputSchema(maxZones) {
  const nullableNumber = { type: ["number", "null"] };
  return {
    type: "object",
    additionalProperties: false,
    properties: {
      zones: {
        type: "array",
        maxItems: maxZones,
        items: {
          type: "object",
          additionalProperties: false,
          properties: {
            label: { type: "string" },
            severity: {
              type: "string",
              enum: ["low", "medium", "high", "critical"],
            },
            risk_score: { type: "integer", minimum: 0, maximum: 100 },
            confidence: {
              type: "string",
              enum: ["source-backed", "modelled", "analyst-reviewed"],
            },
            lat: nullableNumber,
            lon: nullableNumber,
            radius_m: { type: ["integer", "null"], minimum: 50, maximum: MAX_ZONE_RADIUS_M },
            coordinates: {
              type: "array",
              maxItems: 24,
              items: {
                type: "object",
                additionalProperties: false,
                properties: {
                  lat: { type: "number", minimum: -90, maximum: 90 },
                  lon: { type: "number", minimum: -180, maximum: 180 },
                },
                required: ["lat", "lon"],
              },
            },
            display_color: {
              type: "string",
              enum: ["green", "orange", "red"],
            },
            icon: { type: "string" },
            notes: { type: "string" },
            evidence_urls: {
              type: "array",
              maxItems: 8,
              items: { type: "string" },
            },
            evidence: {
              type: "array",
              maxItems: 8,
              items: {
                type: "object",
                additionalProperties: false,
                properties: {
                  url: { type: "string" },
                  published_at: { type: "string" },
                },
                required: ["url", "published_at"],
              },
            },
          },
          required: [
            "label",
            "severity",
            "risk_score",
            "confidence",
            "lat",
            "lon",
            "radius_m",
            "coordinates",
            "display_color",
            "icon",
            "notes",
            "evidence_urls",
            "evidence",
          ],
        },
      },
      notes: { type: "string" },
      verifiedSourceUrls: {
        type: "array",
        maxItems: 24,
        items: { type: "string" },
      },
    },
    required: ["zones", "notes", "verifiedSourceUrls"],
  };
}

async function main() {
  const input = JSON.parse(await readStdin());
  const prompt = requiredString(input.prompt, "prompt", MAX_PROMPT_CHARS);
  const workspace = requiredString(input.workspace, "workspace", 500);
  const codexHome = requiredString(
    input.codexHome || process.env.CODEX_HOME,
    "codexHome",
    500,
  );
  const model = requiredString(input.model || "gpt-5.6-sol", "model", 120);
  const reasoningEffort = String(input.reasoningEffort || "low").trim().toLowerCase();
  if (!["minimal", "low", "medium", "high", "xhigh"].includes(reasoningEffort)) {
    throw new Error("Invalid area-risk Codex reasoning effort");
  }
  const maxZones = Math.max(1, Math.min(Number(input.maxZones) || MAX_ZONES, MAX_ZONES));

  const codexEnv = {
    PATH: process.env.PATH || "/usr/local/bin:/usr/bin:/bin",
    HOME: process.env.HOME || "/home/lunaragent",
    CODEX_HOME: codexHome,
    LANG: process.env.LANG || "C.UTF-8",
    LC_ALL: process.env.LC_ALL || "C.UTF-8",
    NO_COLOR: "1",
  };
  const codex = new Codex({
    ...(input.codexPath ? { codexPathOverride: String(input.codexPath) } : {}),
    env: codexEnv,
    config: {
      show_raw_agent_reasoning: false,
      features: {
        shell_tool: false,
        unified_exec: false,
        code_mode: false,
        code_mode_host: false,
      },
      default_permissions: "lunar_area_risk",
      permissions: {
        lunar_area_risk: {
          description: "Sanitized SafeRoute area-risk evidence analysis only",
          extends: ":workspace",
          filesystem: {
            [configPathKey(codexHome)]: "deny",
            [configPathKey("/proc")]: "deny",
          },
          network: { enabled: false },
        },
      },
    },
  });
  const thread = codex.startThread({
    model,
    modelReasoningEffort: reasoningEffort,
    workingDirectory: workspace,
    skipGitRepoCheck: true,
    webSearchMode: "live",
    approvalPolicy: "never",
    sandboxMode: EXECUTION_POLICY.sandboxMode,
    networkAccessEnabled: EXECUTION_POLICY.networkAccessEnabled,
  });
  const turn = await thread.run(prompt, {
    outputSchema: outputSchema(maxZones),
  });
  let webSearchCompleted = false;
  for (const item of turn.items || []) {
    if (!ALLOWED_RESULT_ITEM_TYPES.has(item?.type)) {
      throw new Error(`Area-risk Codex emitted a forbidden item type: ${String(item?.type || "unknown")}`);
    }
    if (item?.type === "web_search") webSearchCompleted = true;
  }
  let result;
  try {
    result = JSON.parse(String(turn.finalResponse || ""));
  } catch (error) {
    throw new Error("Area-risk Codex returned invalid structured JSON", { cause: error });
  }
  if (
    !result ||
    !Array.isArray(result.zones) ||
    typeof result.notes !== "string" ||
    !Array.isArray(result.verifiedSourceUrls)
  ) {
    throw new Error("Area-risk Codex returned an invalid structured payload");
  }
  const suppliedEvidenceUrls = new Set(
    (Array.isArray(input.evidenceUrls) ? input.evidenceUrls : [])
      .map(safePublicUrl)
      .filter(Boolean),
  );
  const verifiedSourceUrls = webSearchCompleted
    ? [...new Set(result.verifiedSourceUrls.map(safePublicUrl).filter(Boolean))].slice(0, 24)
    : [];
  const allowedEvidenceUrls = new Set([...suppliedEvidenceUrls, ...verifiedSourceUrls]);
  const zones = result.zones.slice(0, maxZones).map((zone) => {
    const evidence_urls = [...new Set((zone.evidence_urls || []).map(safePublicUrl).filter(
      (url) => url && allowedEvidenceUrls.has(url),
    ))].slice(0, 8);
    const evidence = [];
    for (const item of Array.isArray(zone.evidence) ? zone.evidence : []) {
      const url = safePublicUrl(item?.url);
      const published_at = canonicalEvidenceDate(item?.published_at);
      if (
        url &&
        evidence_urls.includes(url) &&
        published_at &&
        !evidence.some((existing) => existing.url === url)
      ) {
        evidence.push({ url, published_at });
      }
      if (evidence.length >= 8) break;
    }
    return { ...zone, evidence_urls, evidence };
  }).filter((zone) => zone.evidence_urls.length > 0);
  process.stdout.write(`${JSON.stringify({
    zones,
    notes: result.notes.slice(0, 1000),
    model,
    webSearchCompleted,
    verifiedSourceUrls,
  })}\n`);
}

main().catch((error) => {
  process.stderr.write(`Area-risk Codex runtime error: ${String(error?.message || error).slice(0, 1000)}\n`);
  process.exit(1);
});
