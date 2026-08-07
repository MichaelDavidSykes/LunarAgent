# LunarAgent

`LunarAgent` is the dedicated reasoning and research service embedded in
LunarChain Explorer.

The current design keeps responsibilities separated:

- `LunarSurfaceBackend` handles auth, Explorer query execution, and session state.
- `LunarAgent` receives the current Explorer session, selected entities, scoped summary, and conversation history.
- `LunarAgent` uses bounded internal backend tools to inspect the intelligence graph before answering.
- For broad or current questions, `LunarAgent` can combine graph evidence with live public-web research.
- `LunarAgent` returns:
  - `reply`
  - `actions`
  - `followUps`

This keeps auth and graph access in the backend while still letting the agent do report-level reasoning.

## Explorer Codex runtime

`POST /v1/explorer-agent/respond` runs a persistent Codex SDK thread using
ChatGPT-managed Codex authentication rather than `OPENAI_API_KEY` billing.
The runtime is configured for `gpt-5.6-sol` with `medium` reasoning, live web
research, a read-only/no-host-execution policy, and the LunarGraph MCP bridge.

Security boundaries:

- the browser never receives ChatGPT auth, graph credentials, or service tokens;
- the Python service launches Codex with a scrubbed environment;
- the checked `codex_runtime/execution_policy.json` contract forces the Codex
  SDK to `sandboxMode=read-only`, disables SDK network access, and declares
  host commands and file writes unavailable;
- the MCP bridge registers only read-only LunarGraph evidence tools. It exposes
  no shell, code execution, file, host, or command-broker tool;
- any unexpected SDK command or file-change event aborts the turn and maps to
  a safe policy failure instead of being presented as successful work;
- a least-privilege Codex permission profile also denies access to
  `CODEX_HOME` (including `auth.json`) and Linux process metadata;
- live public research uses Codex's native web-search capability, while graph
  access uses the scoped MCP bridge;
- graph access uses a short-lived, per-turn, read-only delegated token;
- each MCP process enforces per-turn logical evidence budgets: four curated
  graph searches, six report reads, two schema inspections, and four custom
  read queries. Overlapping/synonym-only searches are discouraged, while one
  bounded retry for HTTP `429`/`502`/`503`/`504` is handled inside the bridge
  rather than consuming another model-planned lookup;
- write AQL is rejected by the backend guardrail;
- detailed SDK events are streamed back to the backend, with credentials and
  raw chain-of-thought removed;
- as soon as the official SDK establishes a thread, LunarAgent checkpoints its
  opaque thread id into the exact active backend turn. Because Codex session
  storage is on the protected persistent `CODEX_HOME` mount, a replacement
  LunarAgent process can retrieve that checkpoint during graph bootstrap and
  resume the same SDK thread after a service restart. The checkpoint is
  internal-only, tenant/access checked, compare-and-set, and never appears in
  browser activity logs;
- after SDK completion and output guardrails, LunarAgent privately checkpoints
  the bounded typed final response against the same active request, request
  fingerprint, and Codex thread before ordinary HTTP delivery. A replacement
  process validates the checkpoint hash and exact identities during graph
  bootstrap and can return it without repeating graph research, web research,
  or local execution. Backend terminal completion, failure, or cancellation
  clears the checkpoint, and stale worker snapshots cannot overwrite it;
- when a medium-reasoning SDK stream has no new event for 12 seconds, a
  bounded liveness-only activity pulse keeps Explorer visibly responsive. It
  repeats no more than once every 20 seconds, stops after 24 pulses or
  immediately on completion/cancellation, and reports no inferred work,
  reasoning text, or hidden chain-of-thought;
- credential-shaped content is removed from tool results, activity events,
  final Markdown, entity fields, citations, and follow-ups
  before any of those values can reach the browser;
- citation links fail closed unless either the exact normalized URL appeared in
  a completed LunarGraph tool result's explicit source/link field or the
  official Codex SDK reported a completed native live-web search. The SDK
  web-search event exposes the query and completion state, but not its result
  URLs, so activity logs distinguish exact tool-evidence URL binding from
  URL-safe citations accepted after native search and never claim a stronger
  page-level binding than the SDK provides. Arbitrary `url` fields and URLs
  embedded only in untrusted report prose are not promoted to citation
  evidence. Citation URLs also reject single-label/internal hosts, private or
  link-local addresses, and IPv4-mapped/private IPv6 forms. When Codex omits a
  structured citation after directly inspecting an authorized
  `get_graph_report` result, the guardrail retains that report's exact direct
  `sourceLink`, title, source name, and valid timestamp as a deterministic
  citation. This path never reads search-result lists, arbitrary metadata,
  nested entity links, snippets, report prose, or custom-query output, and it
  re-requires the URL in the existing completed-tool evidence allowlist;
- interactive entities fail closed unless the exact canonical graph document
  ID, label, and type appeared in the current turn's completed curated graph
  search or report-detail result. The evidence-authoritative type replaces any
  model-supplied type. Web-only names, arbitrary custom-query fields, inferred
  labels, mismatched labels, and invented graph references never receive
  clickable follow-up actions;
- consequential external actions are not enabled.

The response includes Markdown, Explorer actions, grounded clickable entities,
citations, follow-up prompts, the Codex thread id, and model metadata.

## API

### `GET /health`

Returns readiness and the configured model. It returns `503` when authentication, OpenAI, or backend tool configuration is incomplete.

### `GET /live`

Lightweight process liveness endpoint used by the container health check.

### `POST /v1/explorer-agent/respond`

Input:

```json
{
  "sessionId": "optional-backend-session-id",
  "requestId": "per-turn-idempotency-id",
  "codexThreadId": "optional-persistent-codex-thread-id",
  "clientId": "workspace-id",
  "conversationHistory": [
    { "role": "assistant", "content": "..." },
    { "role": "user", "content": "..." }
  ],
  "queryPreview": "Active Explorer query",
  "queryContext": {},
  "querySummary": {},
  "currentUserMessage": "Which actors matter most here?",
  "selectedEntities": [],
}
```

Output:

```json
{
  "reply": "grounded Markdown answer",
  "codexThreadId": "persistent-codex-thread-id",
  "model": "gpt-5.6-sol",
  "actions": [],
  "followUps": [],
  "entities": [],
  "citations": []
}
```

`GET /v1/explorer-agent/health` verifies the Explorer runtime, backend bridge,
and mounted ChatGPT-managed Codex authentication. Readiness runs the official
`codex login status` check and accepts only the ChatGPT login method, never an
API-key login. If a turn reports an authentication failure, readiness stays
fail-closed during a bounded cooldown, then safely rechecks the official Codex
login status so automatically recovered ChatGPT sessions do not leave the
service permanently unavailable. Rotating the credential file bypasses the
cooldown immediately. A short probe cache prevents a process launch per
concurrent health request.

## Environment

Optional:

- `LUNAR_AGENT_MODEL` (default `gpt-5.1`)
- `LUNAR_AGENT_HTTP_TIMEOUT`
- `LUNAR_AGENT_WEB_RESEARCH_ENABLED` (default `true`)
- `LUNAR_AGENT_WEB_SEARCH_CONTEXT_SIZE` (default `medium`)
- `LUNAR_AGENT_WEB_REASONING_EFFORT` (default `low`)
- `LUNAR_AGENT_WEB_MAX_OUTPUT_TOKENS` (default `1000`)
- `LUNAR_AGENT_TOTAL_TURN_TIMEOUT` (default `125`; hard end-to-end turn deadline)
- `LUNAR_AGENT_MAX_PARALLEL_TOOL_CALLS` (default `3`)
- `LUNAR_AGENT_MAX_CONCURRENT_REQUESTS` (default `8`)
- `LUNAR_AGENT_QUOTA_REQUESTS_PER_HOUR` (default `60` per quota key)
- `LUNAR_AGENT_QUOTA_REQUESTS_PER_DAY` (default `300` per quota key)
- `LUNAR_AGENT_SHARED_TOKEN` (required; the service fails closed without it)
- `LUNAR_AGENT_BACKEND_BASE_URL`
- `LUNAR_AGENT_BACKEND_SHARED_TOKEN`
- `LUNAR_AGENT_BACKEND_HTTP_TIMEOUT`
- `LUNAR_AGENT_AREA_RISK_PROVIDER_MODE` (default `chatgpt-account`; set to
  `openai-api` only when API billing is intentionally enabled)
- `LUNAR_AGENT_AREA_RISK_ACCOUNT_ENABLED` (default `true`; allows the primary
  ChatGPT-managed account provider)
- `LUNAR_AGENT_AREA_RISK_MODEL` (default `gpt-5.1`; API mode only)
- `LUNAR_AGENT_AREA_RISK_WEB_RESEARCH_ENABLED` (default `true`; API mode only)
- `LUNAR_AGENT_AREA_RISK_SEARCH_CONTEXT_SIZE` (default `medium`)
- `LUNAR_AGENT_AREA_RISK_MAX_OUTPUT_TOKENS` (default `700`)
- `LUNAR_AGENT_AREA_RISK_MAX_EVIDENCE_ITEMS` (default `12`)
- `LUNAR_AGENT_AREA_RISK_MAX_ZONES` (default `6`)
- `LUNAR_AGENT_AREA_RISK_FALLBACK_ON_EMPTY_WEB` (default `false`, avoids a second model call when web research returns no named zones)
- `LUNAR_AGENT_AREA_RISK_CODEX_MODEL` (default `LUNAR_AGENT_CODEX_MODEL` or
  `gpt-5.6-sol`)
- `LUNAR_AGENT_AREA_RISK_CODEX_REASONING_EFFORT` (default `low`)
- `LUNAR_AGENT_AREA_RISK_CODEX_TIMEOUT` (default `180` seconds)
- `LUNAR_AGENT_CODEX_ENABLED` (default `true`)
- `LUNAR_AGENT_CODEX_MODEL` (default `gpt-5.6-sol`)
- `LUNAR_AGENT_CODEX_REASONING_EFFORT` (default `medium`)
- `LUNAR_AGENT_CODEX_TIMEOUT` (default `900`)
- `LUNAR_AGENT_CODEX_MAX_CONCURRENT_REQUESTS` (default `2`)
- `LUNAR_AGENT_CODEX_WORKSPACE_ROOT` (default `/tmp/lunar-agent-workspaces`;
  production `/tmp/lunar-agent-codex-workspaces` on the container tmpfs)
- `CODEX_HOME` (default and production value `/codex-auth`)
- `LUNAR_AGENT_APP_ROOT` (production image value `/app`)
- `CODEX_CLI_PATH` (optional bundled CLI override)
- `CODEX_NODE_BINARY` (default `node`)

The Explorer LunarAgent endpoint does not pass `OPENAI_API_KEY` to Codex and
uses the ChatGPT-managed authentication mounted at `CODEX_HOME`. SafeRoute
area-risk research uses that same ChatGPT-account provider by default, including
its server-side public web research. It receives only bounded public evidence
and public AOI metadata, has MCP/shell/file and local-network access disabled,
and never receives `OPENAI_API_KEY` or Lunar service credentials.

`LUNAR_AGENT_AREA_RISK_PROVIDER_MODE=openai-api` is the single explicit billing
switch. In that mode the existing Responses API web-research and evidence
analysis logic is retained. Neither mode silently falls through to the other:
an unavailable selected provider fails truthfully so the caller can retry,
without surprise billing or a false successful zero-zone result. Deterministic
headline/regex label extraction is not part of the pipeline; only AI-generated,
source-validated locality zones can be returned. Both providers are constrained
to locality-level zones with a hard 2,500 metre radius ceiling; city-scale
radius or coordinate geometry is rejected rather than silently shrunk into a
falsely precise hotspot.

Explorer turns cannot run commands or create files. The former host command
broker, its Unix socket, host workspace mount, deployment unit, and MCP tool
have been removed. The Agent still starts each Codex runner in its own process
group and terminates that group on cancellation. The MCP bridge monitors its
exact runner process identity and exits when the owner disappears.

The Agent health route fails closed unless it can load the exact checked
read-only policy. Its `commandSandbox` compatibility field reports
`mode=read-only-no-host-exec`, `sandboxMode=read-only`,
`networkAccessEnabled=false`, `hostCommands=false`, and `fileWrites=false`.

Callers must send the configured `LUNAR_AGENT_SHARED_TOKEN`:

```http
Authorization: Bearer <token>
```

## Local Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
npm ci
uvicorn lunar_agent.main:app --reload --port 8310
```

`deploy/lunar-agent.service` runs the authentication-bearing Agent container
read-only, non-root, capability-free, with `no-new-privileges`, bounded
resources, and a private tmpfs. It has no command socket or host workspace
mount.

## Backend Wiring

Configure `LunarSurfaceBackend` with:

```env
EXPLORER_AGENT_BASE_URL=http://localhost:8310
EXPLORER_AGENT_SHARED_TOKEN=your-shared-token
```

Configure `LunarAgent` with:

```env
LUNAR_AGENT_BACKEND_BASE_URL=http://localhost:8000
LUNAR_AGENT_BACKEND_SHARED_TOKEN=your-shared-token
```

The shared token is used both ways:

- `LunarSurfaceBackend -> LunarAgent` for the main `/v1/explorer-agent/respond` call
- `LunarAgent -> LunarSurfaceBackend` for the internal scoped report tools

For each Explorer turn, the backend issues a short-lived delegated graph token
bound to the active session, request, and tenant. The MCP process receives only
that scoped token, never the long-lived backend token. Logical tool budgets
bound model-directed repetition independently of the backend's lower-level
delegated-token HTTP call ceiling.

## Production Codex authentication

Provision Codex authentication interactively on the host and store it outside
the repository. The systemd unit expects the protected host directory
`/etc/lunar-agent/codex` to be mounted at `/codex-auth`. Built-in Codex shell
execution and file mutation are denied by the checked read-only execution
policy; no command broker is available. Set host ownership to container UID
`10001`, restrict directory
permissions, and treat `auth.json` like a password. Never commit or print its
contents.

If `EXPLORER_AGENT_BASE_URL` or its shared token is unset, the Explorer Agent
fails closed with `503`; it never falls back to an API-key-backed model path.
