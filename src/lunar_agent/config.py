import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False)

    project_name: str = "LunarAgent"
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    model: str = os.getenv("LUNAR_AGENT_MODEL", "gpt-5-mini")
    http_timeout: int = int(os.getenv("LUNAR_AGENT_HTTP_TIMEOUT", "60"))
    area_risk_web_research_enabled: bool = os.getenv("LUNAR_AGENT_AREA_RISK_WEB_RESEARCH_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    area_risk_model: str = os.getenv("LUNAR_AGENT_AREA_RISK_MODEL", "gpt-5.4-mini")
    area_risk_search_context_size: str = os.getenv("LUNAR_AGENT_AREA_RISK_SEARCH_CONTEXT_SIZE", "medium")
    area_risk_reasoning_effort: str = os.getenv("LUNAR_AGENT_AREA_RISK_REASONING_EFFORT", "low")
    area_risk_max_output_tokens: int = int(os.getenv("LUNAR_AGENT_AREA_RISK_MAX_OUTPUT_TOKENS", "700"))
    area_risk_max_evidence_items: int = int(os.getenv("LUNAR_AGENT_AREA_RISK_MAX_EVIDENCE_ITEMS", "12"))
    area_risk_max_zones_per_request: int = int(os.getenv("LUNAR_AGENT_AREA_RISK_MAX_ZONES", "6"))
    area_risk_fallback_on_empty_web: bool = os.getenv("LUNAR_AGENT_AREA_RISK_FALLBACK_ON_EMPTY_WEB", "false").strip().lower() in {"1", "true", "yes", "on"}
    area_risk_fallback_on_web_error: bool = os.getenv("LUNAR_AGENT_AREA_RISK_FALLBACK_ON_WEB_ERROR", "true").strip().lower() in {"1", "true", "yes", "on"}
    shared_token: str = os.getenv("LUNAR_AGENT_SHARED_TOKEN", "")
    backend_base_url: str = os.getenv("LUNAR_AGENT_BACKEND_BASE_URL", "")
    backend_shared_token: str = os.getenv("LUNAR_AGENT_BACKEND_SHARED_TOKEN", "")
    backend_http_timeout: int = int(os.getenv("LUNAR_AGENT_BACKEND_HTTP_TIMEOUT", "45"))


settings = Settings()
