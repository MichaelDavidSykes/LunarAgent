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
research, workspace-scoped command execution, and the LunarGraph MCP bridge.

Security boundaries:

- the browser never receives ChatGPT auth, graph credentials, or service tokens;
- the Python service launches Codex with a scrubbed environment;
- a least-privilege Codex permission profile denies model-generated commands
  from reading `CODEX_HOME` (including `auth.json`) or Linux process metadata;
- shell commands have no direct network path; current research uses Codex's
  native live web-search tool and graph access uses the scoped MCP bridge;
- graph access uses a short-lived, per-turn, read-only delegated token;
- write AQL is rejected by the backend guardrail;
- detailed SDK events are streamed back to the backend, with credentials and
  raw chain-of-thought removed;
- credential-shaped content is removed from command text/output, tool results,
  activity events, final Markdown, entity fields, citations, and follow-ups
  before any of those values can reach the browser;
- citation links fail closed unless either the exact normalized URL appeared in
  a completed LunarGraph tool result's explicit source/link field or the
  official Codex SDK reported a completed native live-web search. The SDK
  web-search event exposes the query and completion state, but not its result
  URLs, so activity logs distinguish exact tool-evidence URL binding from
  URL-safe citations accepted after native search and never claim a stronger
  page-level binding than the SDK provides;
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
fail-closed for that credential-file generation until Codex authentication is
reconnected or rotated; a short probe cache prevents a process launch per
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
- `LUNAR_AGENT_AREA_RISK_MODEL` (default `gpt-5.1`)
- `LUNAR_AGENT_AREA_RISK_SEARCH_CONTEXT_SIZE` (default `medium`)
- `LUNAR_AGENT_AREA_RISK_MAX_OUTPUT_TOKENS` (default `700`)
- `LUNAR_AGENT_AREA_RISK_MAX_EVIDENCE_ITEMS` (default `12`)
- `LUNAR_AGENT_AREA_RISK_MAX_ZONES` (default `6`)
- `LUNAR_AGENT_AREA_RISK_FALLBACK_ON_EMPTY_WEB` (default `false`, avoids a second model call when web research returns no named zones)
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
uses the ChatGPT-managed authentication mounted at `CODEX_HOME`. The separate
legacy SafeRoute area-risk endpoint still requires `OPENAI_API_KEY`; that key is
not used for Explorer LunarAgent turns.

Explorer workspace commands do not run inside the authentication-bearing Agent
container. Codex calls the `run_workspace_command` MCP tool, which reaches a
token-authenticated Unix-socket broker. The broker executes each command inside
a fresh Bubblewrap namespace with:

- no network namespace connectivity;
- only `/usr`, a minimal `/dev` and `/proc`, a private `/tmp`, and the opaque
  per-session workspace mounted;
- no Codex home, LunarChain services, environment secrets, host home directory,
  or tenant data mounts;
- cleared environment variables, resource limits, concurrency limits, bounded
  output, a hard deadline, and content-level credential redaction.

The Agent health route fails closed when this command sandbox is unavailable.
The broker health contract runs and briefly caches a real isolated probe; it
verifies namespace setup, the private workspace, absence of mounted auth/backend
secrets, and rejection of IP sockets rather than reporting ready from binary
presence alone.
The runtime never treats a failed command as verified, and the final response
receives a deterministic warning if a model attempts to claim otherwise.
When a Codex turn is stopped, terminating the Agent-side runner closes its
Unix-socket command request. The broker monitors that request connection across
queueing and execution; a disconnect cancels and reaps the complete Bubblewrap
process group instead of allowing a previously started command to continue
after the Explorer session is cancelled. The Agent starts each Codex runner in
its own process group and terminates that whole group on cancellation, ensuring
the MCP client closes its broker request and triggers the same cleanup boundary.
The broker and Agent container share UID `10001` for the protected broker socket,
but the host command-workspace root is deliberately not mounted into the Agent
container. The broker lazily creates the opaque host workspace on the first
command, while Codex receives a separate session cwd on the container's private
`/tmp`. A turn therefore cannot inspect a sibling session's command files.
Commands for the same investigation workspace are serialized, while commands
for two different investigations may use the bounded global concurrency in
parallel. Inactive host workspaces are removed after 24 hours by default;
`LUNAR_AGENT_COMMAND_WORKSPACE_TTL_SECONDS` and
`LUNAR_AGENT_COMMAND_WORKSPACE_CLEANUP_INTERVAL_SECONDS` can narrow or extend
that bounded temporary retention. Active and executable-health workspaces are
never removed. The per-command process ceiling accounts for Codex threads
charged to the shared UID, while the broker's independent systemd `TasksMax`
cgroup stays the tighter process boundary for command execution.

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

The production command broker runs separately as
`lunar-agent-command-broker.service`; see `deploy/`. It requires Bubblewrap,
the dedicated unprivileged `lunaragent` account, a root-owned environment file
containing the broker token, and the shared workspace directory. The Agent
container receives only the broker socket and broker token; it does not receive
the host command-workspace mount. The broker unit pins `PYTHONPATH` to the
root-controlled `current/src` release link, so an atomic release-link change
and service restart loads the exact staged broker source rather than a stale
package previously installed in the shared dependency virtual environment.
`deploy/lunar-agent.service` preserves the existing read-only, capability-free
Agent container boundary and adds only the read-only socket mount. The broker itself runs as
the unprivileged host account with systemd hardening. Its only allowed socket
families are Unix sockets and the netlink socket Bubblewrap needs to create a
private, disconnected network namespace.
The Agent unit is bound to and part of the broker unit's restart lifecycle. A
broker restart therefore also recreates the Agent container, remounting the
current Unix-socket directory and reloading a rotated broker token instead of
leaving a stale socket or credential in a long-lived container.

Ubuntu hosts that enforce restricted unprivileged user namespaces must install
the repository's narrow AppArmor exception for a dedicated, group-restricted
Bubblewrap executable. Do not relax the host-wide
`unprivileged_userns` profile:

```bash
install -d -o root -g lunaragent -m 0750 /opt/lunar-agent-command-broker/bin
install -o root -g lunaragent -m 0750 \
  /usr/bin/bwrap /opt/lunar-agent-command-broker/bin/bwrap
install -o root -g root -m 0644 \
  deploy/lunar-agent-command-broker.apparmor \
  /etc/apparmor.d/lunar-agent-command-broker-bwrap
apparmor_parser -r /etc/apparmor.d/lunar-agent-command-broker-bwrap
```

The broker environment must then set
`LUNAR_AGENT_BWRAP_BINARY=/opt/lunar-agent-command-broker/bin/bwrap`. The
AppArmor exception is attached only to that root-owned executable, which is
executable only by the dedicated service group. The systemd unit keeps the
broker unprivileged with no ambient capabilities; the bounded namespace setup
capabilities are available only inside Bubblewrap's new user namespace.

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
that scoped token, never the long-lived backend token.

## Production Codex authentication

Provision Codex authentication interactively on the host and store it outside
the repository. The systemd unit expects the protected host directory
`/etc/lunar-agent/codex` to be mounted at `/codex-auth`. Built-in Codex shell
execution remains denied from this authentication-bearing container; all
commands are delegated to the credential-free command broker sandbox described
above. Set host ownership to container UID `10001`, restrict directory
permissions, and treat `auth.json` like a password. Never commit or print its
contents.

If `EXPLORER_AGENT_BASE_URL` or its shared token is unset, the Explorer Agent
fails closed with `503`; it never falls back to an API-key-backed model path.
