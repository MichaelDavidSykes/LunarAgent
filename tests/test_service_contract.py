import json

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
