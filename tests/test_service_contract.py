import asyncio
import json

from lunar_agent import service as service_module
from lunar_agent.service import build_prompt_messages, normalize_model_response, normalize_response_payload


def test_normalize_model_response_accepts_fenced_json_and_sanitizes_actions():
    raw = """```json
{
  "reply": "  Analysts should prioritize report-backed claims.  ",
  "actions": [
    {"type": "focus_country", "countryName": "Germany", "reason": "Requested location focus"},
    {"type": "apply_module_filter", "moduleKeys": ["module-maritime", "Maritime module", ""]},
    {"type": "delete_everything"},
    {"type": "clear_module_filters", "label": "Reset modules"},
    {"type": "open_map", "label": "Open map"}
  ],
  "follow_ups": ["Which reports changed?", "Which reports changed?", "Show indicators"]
}
```"""

    payload = normalize_model_response(raw)

    assert payload == {
        "reply": "Analysts should prioritize report-backed claims.",
        "actions": [
            {
                "type": "focus_country",
                "label": "Focus Germany",
                "reason": "Requested location focus",
                "countryName": "Germany",
            },
            {
                "type": "apply_module_filter",
                "label": "Filter to Maritime",
                "moduleKeys": ["module-maritime"],
            },
            {"type": "clear_module_filters", "label": "Reset modules"},
        ],
        "followUps": ["Which reports changed?", "Show indicators"],
    }


def test_normalize_response_payload_supports_camel_case_followups_and_rejects_incomplete_actions():
    payload = normalize_response_payload(
        {
            "reply": "",
            "actions": [
                {"type": "focus_country"},
                {"type": "apply_module_filter", "moduleKeys": []},
                {"type": "clear_country_focus"},
            ],
            "followUps": "- Compare reports\n- List sources",
        }
    )

    assert payload["reply"] == "I couldn't produce a structured answer for this query yet."
    assert payload["actions"] == [{"type": "clear_country_focus", "label": "Clear location focus"}]
    assert payload["followUps"] == ["Compare reports", "List sources"]


def test_normalize_model_response_falls_back_to_plain_text_for_unstructured_output():
    payload = normalize_model_response("No structured JSON was returned.")

    assert payload == {
        "reply": "No structured JSON was returned.",
        "actions": [],
        "followUps": [],
    }


def test_build_prompt_messages_keeps_ui_actions_disabled_until_allowed():
    messages = build_prompt_messages(
        session_id="session-1",
        allow_ui_actions=False,
        conversation_history=[{"role": "user", "content": "What matters?"}],
        query_preview="FOR doc IN reports RETURN doc",
        summary={"reports": 2},
        context={"scope": "demo"},
        user_message="Focus the map on Germany",
    )

    assert messages[0]["role"] == "system"
    prompt_payload = json.loads(messages[1]["content"])

    assert prompt_payload["allowUiActions"] is False
    assert prompt_payload["allowedActions"] == []
    assert prompt_payload["responseShape"]["actions"] == []
    assert prompt_payload["conversationHistory"] == [{"role": "user", "content": "What matters?"}]
    assert any("allowUiActions=false" in instruction for instruction in prompt_payload["instructions"])


def test_build_prompt_messages_declares_allowed_actions_when_enabled():
    messages = build_prompt_messages(
        session_id=None,
        allow_ui_actions=True,
        conversation_history=[],
        query_preview="query",
        summary={},
        context={},
        user_message="Open the map",
    )

    prompt_payload = json.loads(messages[1]["content"])

    assert prompt_payload["allowUiActions"] is True
    assert [action["type"] for action in prompt_payload["allowedActions"]] == [
        "focus_country",
        "clear_country_focus",
        "apply_module_filter",
        "clear_module_filters",
        "open_map",
    ]


def test_area_risk_web_prompt_uses_bounded_evidence_and_zone_caps(monkeypatch):
    monkeypatch.setattr(service_module.settings, "area_risk_max_evidence_items", 3)
    monkeypatch.setattr(service_module.settings, "area_risk_max_zones_per_request", 4)
    evidence = [
        {
            "title": f"Risk report {index}",
            "url": f"https://example.test/{index}",
            "source": "Example",
            "snippet": "A" * 900,
        }
        for index in range(8)
    ]

    prompt = service_module.build_safe_route_area_risk_web_prompt(
        session_id="session-1",
        aoi={"bounds": {"minLat": 0, "minLon": 0, "maxLat": 1, "maxLon": 1}},
        evidence=evidence,
        max_zones=12,
    )

    payload = json.loads(prompt.splitlines()[-1])
    assert payload["maxZones"] == 4
    assert len(payload["seedEvidence"]) == 3
    assert len(payload["seedEvidence"][0]["snippet"]) <= 420


def test_area_risk_payload_normalization_tolerates_malformed_numeric_fields():
    payload = service_module.normalize_safe_route_area_risk_payload(
        {
            "zones": [
                {
                    "label": "Khayelitsha",
                    "severity": "critical",
                    "risk_score": "high",
                    "lat": "not-a-lat",
                    "lng": "18.6732",
                    "radius_m": "wide",
                    "coordinates": [
                        {"lat": "-33.0392", "lng": "18.6732"},
                        {"lat": "outside", "lng": "18.7"},
                    ],
                },
                {
                    "label": "Manenberg",
                    "riskScore": "104.7",
                    "lat": "-33.989",
                    "lon": "18.559",
                    "radiusM": "1250",
                },
            ],
            "notes": "Normalized model result.",
        },
        max_zones=8,
    )

    assert payload["zones"][0]["risk_score"] == 45
    assert payload["zones"][0]["lat"] is None
    assert payload["zones"][0]["lon"] == 18.6732
    assert payload["zones"][0]["radius_m"] is None
    assert payload["zones"][0]["coordinates"] == [{"lat": -33.0392, "lon": 18.6732}]
    assert payload["zones"][1]["risk_score"] == 100
    assert payload["zones"][1]["lat"] == -33.989
    assert payload["zones"][1]["lon"] == 18.559
    assert payload["zones"][1]["radius_m"] == 1250.0


def test_area_risk_empty_web_result_does_not_double_call_model(monkeypatch):
    monkeypatch.setattr(service_module.settings, "area_risk_web_research_enabled", True)
    monkeypatch.setattr(service_module.settings, "area_risk_fallback_on_empty_web", False)
    monkeypatch.setattr(service_module.settings, "area_risk_model", "gpt-5.4-mini")

    async def fake_web_research(*_args, **_kwargs):
        return '{"zones":[]}'

    async def fail_analysis(*_args, **_kwargs):  # pragma: no cover - only runs on regression
        raise AssertionError("fallback analysis should not run for an empty successful web result")

    monkeypatch.setattr(service_module, "run_openai_web_research", fake_web_research)
    monkeypatch.setattr(service_module, "run_openai_analysis", fail_analysis)

    result = asyncio.run(
        service_module.research_safe_route_area_risk(
            session_id="session-1",
            aoi={"bounds": {"minLat": 0, "minLon": 0, "maxLat": 1, "maxLon": 1}},
            evidence=[],
            max_zones=8,
        )
    )

    assert result == {
        "zones": [],
        "model": "gpt-5.4-mini",
        "notes": "Dynamic web research returned no named locality zones.",
    }


def test_area_risk_web_error_fallback_hides_provider_detail(monkeypatch):
    monkeypatch.setattr(service_module.settings, "area_risk_web_research_enabled", True)
    monkeypatch.setattr(service_module.settings, "area_risk_fallback_on_web_error", True)
    monkeypatch.setattr(service_module.settings, "area_risk_model", "gpt-5.4-mini")

    async def fail_web_research(*_args, **_kwargs):
        raise RuntimeError("OpenAI returned HTTP 500: provider-secret-token")

    async def fallback_analysis(*_args, **_kwargs):
        return '{"zones":[]}'

    monkeypatch.setattr(service_module, "run_openai_web_research", fail_web_research)
    monkeypatch.setattr(service_module, "run_openai_analysis", fallback_analysis)

    result = asyncio.run(
        service_module.research_safe_route_area_risk(
            session_id="session-1",
            aoi={"bounds": {"minLat": 0, "minLon": 0, "maxLat": 1, "maxLon": 1}},
            evidence=[],
            max_zones=8,
        )
    )

    assert result == {
        "zones": [],
        "model": "gpt-5.4-mini",
        "notes": "Dynamic web research failed; fell back to supplied evidence only.",
    }
    assert "provider-secret-token" not in json.dumps(result)
