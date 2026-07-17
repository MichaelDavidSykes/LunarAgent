#!/usr/bin/env node

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import * as z from "zod/v4";

const toolsUrl = String(process.env.LUNAR_GRAPH_TOOLS_URL || "").replace(/\/+$/, "");
const delegatedToken = String(process.env.LUNAR_GRAPH_DELEGATED_TOKEN || "");

if (!toolsUrl || !delegatedToken) {
  console.error("LunarGraph MCP configuration is missing");
  process.exit(1);
}

const server = new McpServer({
  name: "lunarchain-intelligence-graph",
  version: "1.0.0",
});

async function callGraphTool(tool, payload) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 65000);
  try {
    const response = await fetch(`${toolsUrl}/${tool}`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${delegatedToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload || {}),
      signal: controller.signal,
    });
    const text = await response.text();
    let data;
    try {
      data = JSON.parse(text);
    } catch {
      data = { error: "LunarGraph returned a non-JSON response." };
    }
    if (!response.ok) {
      const detail =
        typeof data?.detail === "string"
          ? data.detail
          : data?.detail?.message || `LunarGraph tool failed with HTTP ${response.status}.`;
      return {
        isError: true,
        content: [{ type: "text", text: String(detail).slice(0, 800) }],
      };
    }
    const encoded = JSON.stringify(data);
    const bounded =
      encoded.length <= 100000
        ? encoded
        : JSON.stringify({
            truncated: true,
            summary: "LunarGraph result exceeded the MCP display limit.",
          });
    return {
      content: [{ type: "text", text: bounded }],
      structuredContent: data,
    };
  } catch (error) {
    return {
      isError: true,
      content: [
        {
          type: "text",
          text:
            error?.name === "AbortError"
              ? "LunarGraph tool timed out."
              : "LunarGraph tool is unavailable.",
        },
      ],
    };
  } finally {
    clearTimeout(timer);
  }
}

server.registerTool(
  "graph_schema",
  {
    description:
      "Inspect the live LunarChain intelligence graph schema, node types, and relationship types before writing a custom AQL query.",
    inputSchema: {},
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  async () => callGraphTool("graph-schema", {}),
);

server.registerTool(
  "search_intelligence_graph",
  {
    description:
      "Search the full LunarChain intelligence graph using natural-language terms and optional date/location constraints. Prefer this before custom AQL.",
    inputSchema: {
      query: z.string().min(1).max(500),
      terms: z.array(z.string().max(160)).max(14).optional(),
      locationTerms: z.array(z.string().max(160)).max(8).optional(),
      createdFrom: z.string().max(80).optional(),
      createdTo: z.string().max(80).optional(),
      limit: z.number().int().min(1).max(30).default(12),
    },
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  async (input) => callGraphTool("search-intelligence-graph", input),
);

server.registerTool(
  "run_graph_read_query",
  {
    description:
      "Run a bounded read-only AQL query against the full LunarChain graph. Write operations are rejected. Inspect graph_schema first.",
    inputSchema: {
      query: z.string().min(1).max(20000),
      bindVars: z.record(z.string(), z.unknown()).optional(),
      resultLimit: z.number().int().min(1).max(150).default(80),
      maxRuntimeSeconds: z.number().min(1).max(30).default(18),
    },
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  async (input) => callGraphTool("run-graph-read-query", input),
);

server.registerTool(
  "get_graph_report",
  {
    description:
      "Read the full content and entity evidence for a graph report returned by search_intelligence_graph or run_graph_read_query.",
    inputSchema: {
      reportId: z.string().min(1).max(240),
    },
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  async (input) => callGraphTool("get-graph-report", input),
);

const transport = new StdioServerTransport();
await server.connect(transport);
