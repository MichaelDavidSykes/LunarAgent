import os

from pydantic_settings import BaseSettings, SettingsConfigDict


_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool) -> bool:
    fallback = "true" if default else "false"
    return os.getenv(name, fallback).strip().lower() in _TRUTHY_ENV_VALUES


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False)

    project_name: str = "LunarAgent"
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    model: str = os.getenv("LUNAR_AGENT_MODEL", "gpt-5.1")
    http_timeout: int = _env_int("LUNAR_AGENT_HTTP_TIMEOUT", 60)
    web_research_enabled: bool = _env_bool("LUNAR_AGENT_WEB_RESEARCH_ENABLED", True)
    web_search_context_size: str = os.getenv("LUNAR_AGENT_WEB_SEARCH_CONTEXT_SIZE", "medium")
    web_reasoning_effort: str = os.getenv("LUNAR_AGENT_WEB_REASONING_EFFORT", "low")
    web_max_output_tokens: int = _env_int("LUNAR_AGENT_WEB_MAX_OUTPUT_TOKENS", 1000)
    chat_reasoning_effort: str = os.getenv("LUNAR_AGENT_CHAT_REASONING_EFFORT", "low")
    chat_max_completion_tokens: int = _env_int("LUNAR_AGENT_CHAT_MAX_COMPLETION_TOKENS", 1200)
    chat_legacy_max_tokens: int = _env_int("LUNAR_AGENT_CHAT_LEGACY_MAX_TOKENS", 900)
    max_tool_rounds: int = _env_int("LUNAR_AGENT_MAX_TOOL_ROUNDS", 5)
    max_tool_calls_per_turn: int = _env_int("LUNAR_AGENT_MAX_TOOL_CALLS_PER_TURN", 8)
    max_tool_result_chars: int = _env_int("LUNAR_AGENT_MAX_TOOL_RESULT_CHARS", 9000)
    reply_max_chars: int = _env_int("LUNAR_AGENT_REPLY_MAX_CHARS", 4800)
    total_turn_timeout: int = _env_int("LUNAR_AGENT_TOTAL_TURN_TIMEOUT", 125)
    max_parallel_tool_calls: int = _env_int("LUNAR_AGENT_MAX_PARALLEL_TOOL_CALLS", 3)
    max_concurrent_requests: int = _env_int("LUNAR_AGENT_MAX_CONCURRENT_REQUESTS", 8)
    quota_requests_per_hour: int = _env_int("LUNAR_AGENT_QUOTA_REQUESTS_PER_HOUR", 60)
    quota_requests_per_day: int = _env_int("LUNAR_AGENT_QUOTA_REQUESTS_PER_DAY", 300)
    area_risk_web_research_enabled: bool = _env_bool("LUNAR_AGENT_AREA_RISK_WEB_RESEARCH_ENABLED", True)
    area_risk_model: str = os.getenv("LUNAR_AGENT_AREA_RISK_MODEL", "gpt-5.1")
    area_risk_search_context_size: str = os.getenv("LUNAR_AGENT_AREA_RISK_SEARCH_CONTEXT_SIZE", "medium")
    area_risk_reasoning_effort: str = os.getenv("LUNAR_AGENT_AREA_RISK_REASONING_EFFORT", "low")
    area_risk_max_output_tokens: int = _env_int("LUNAR_AGENT_AREA_RISK_MAX_OUTPUT_TOKENS", 700)
    area_risk_max_evidence_items: int = _env_int("LUNAR_AGENT_AREA_RISK_MAX_EVIDENCE_ITEMS", 12)
    area_risk_max_zones_per_request: int = _env_int("LUNAR_AGENT_AREA_RISK_MAX_ZONES", 6)
    area_risk_fallback_on_empty_web: bool = _env_bool("LUNAR_AGENT_AREA_RISK_FALLBACK_ON_EMPTY_WEB", False)
    area_risk_fallback_on_web_error: bool = _env_bool("LUNAR_AGENT_AREA_RISK_FALLBACK_ON_WEB_ERROR", True)
    shared_token: str = os.getenv("LUNAR_AGENT_SHARED_TOKEN", "")
    backend_base_url: str = os.getenv("LUNAR_AGENT_BACKEND_BASE_URL", "")
    backend_shared_token: str = os.getenv("LUNAR_AGENT_BACKEND_SHARED_TOKEN", "")
    backend_http_timeout: int = _env_int("LUNAR_AGENT_BACKEND_HTTP_TIMEOUT", 45)
    codex_agent_enabled: bool = _env_bool("LUNAR_AGENT_CODEX_ENABLED", True)
    codex_agent_model: str = os.getenv("LUNAR_AGENT_CODEX_MODEL", "gpt-5.6-sol")
    codex_agent_reasoning_effort: str = os.getenv(
        "LUNAR_AGENT_CODEX_REASONING_EFFORT",
        "medium",
    )
    codex_agent_timeout: int = _env_int("LUNAR_AGENT_CODEX_TIMEOUT", 900)
    codex_agent_max_concurrent_requests: int = _env_int(
        "LUNAR_AGENT_CODEX_MAX_CONCURRENT_REQUESTS",
        2,
    )
    codex_home: str = os.getenv("CODEX_HOME", "")
    codex_cli_path: str = os.getenv("CODEX_CLI_PATH", "")
    codex_node_binary: str = os.getenv("CODEX_NODE_BINARY", "node")
    codex_agent_workspace_root: str = os.getenv(
        "LUNAR_AGENT_CODEX_WORKSPACE_ROOT",
        "/tmp/lunar-agent-workspaces",
    )
settings = Settings()
