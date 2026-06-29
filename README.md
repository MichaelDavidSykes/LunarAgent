# LunarAgent

`LunarAgent` is the dedicated reasoning service for Explorer AI mode.

The current design keeps responsibilities separated:

- `LunarSurfaceBackend` handles auth, Explorer query execution, and session state.
- `LunarAgent` receives the current Explorer session id, scoped summary, and conversation history.
- `LunarAgent` can call bounded internal backend tools to inspect scoped reports before answering.
- For broad or current-events questions, `LunarAgent` first checks LunarGraph and can then use OpenAI web search to add public web context.
- `LunarAgent` returns:
  - `reply`
  - `actions`
  - `followUps`

This keeps auth and graph access in the backend while still letting the agent do report-level reasoning.

## API

### `GET /health`

Returns service health and the configured model.

### `POST /v1/explorer-agent/respond`

Input:

```json
{
  "sessionId": "optional-backend-session-id",
  "conversationHistory": [
    { "role": "assistant", "content": "..." },
    { "role": "user", "content": "..." }
  ],
  "queryPreview": "Active Explorer query",
  "queryContext": {},
  "querySummary": {},
  "currentUserMessage": "Which actors matter most here?"
}
```

Output:

```json
{
  "reply": "markdown answer",
  "actions": [],
  "followUps": [],
  "model": "gpt-5"
}
```

## Environment

Required:

- `OPENAI_API_KEY`

Optional:

- `LUNAR_AGENT_MODEL`
- `LUNAR_AGENT_HTTP_TIMEOUT`
- `LUNAR_AGENT_WEB_RESEARCH_ENABLED` (default `true`)
- `LUNAR_AGENT_WEB_SEARCH_CONTEXT_SIZE` (default `medium`)
- `LUNAR_AGENT_WEB_REASONING_EFFORT` (default `low`)
- `LUNAR_AGENT_WEB_MAX_OUTPUT_TOKENS` (default `1000`)
- `LUNAR_AGENT_SHARED_TOKEN`
- `LUNAR_AGENT_BACKEND_BASE_URL`
- `LUNAR_AGENT_BACKEND_SHARED_TOKEN`
- `LUNAR_AGENT_BACKEND_HTTP_TIMEOUT`
- `LUNAR_AGENT_AREA_RISK_MODEL` (default `gpt-5-mini`)
- `LUNAR_AGENT_AREA_RISK_SEARCH_CONTEXT_SIZE` (default `medium`)
- `LUNAR_AGENT_AREA_RISK_MAX_OUTPUT_TOKENS` (default `700`)
- `LUNAR_AGENT_AREA_RISK_MAX_EVIDENCE_ITEMS` (default `12`)
- `LUNAR_AGENT_AREA_RISK_MAX_ZONES` (default `6`)
- `LUNAR_AGENT_AREA_RISK_FALLBACK_ON_EMPTY_WEB` (default `false`, avoids a second model call when web research returns no named zones)

If `LUNAR_AGENT_SHARED_TOKEN` is set, callers must send:

```http
Authorization: Bearer <token>
```

## Local Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
uvicorn lunar_agent.main:app --reload --port 8310
```

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

If `EXPLORER_AGENT_BASE_URL` is unset, the backend falls back to its current in-process Explorer agent logic.
