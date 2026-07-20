import asyncio
import json

import pytest

from lunar_agent import service as service_module
from lunar_agent.models import ExplorerAgentRespondRequest, SafeRouteAreaRiskResearchRequest
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


def test_normalize_response_payload_accepts_answer_message_and_content_aliases():
    assert normalize_model_response(json.dumps({"answer": "Answer alias."}))["reply"] == "Answer alias."
    assert normalize_model_response(json.dumps({"message": "Message alias."}))["reply"] == "Message alias."
    assert normalize_model_response(json.dumps({"content": "Content alias."}))["reply"] == "Content alias."


def test_normalize_response_payload_unwraps_json_reply_text_and_embedded_controls():
    payload = normalize_response_payload(
        {
            "reply": json.dumps(
                {
                    "reply": "Summary of intelligence in current scope.",
                    "actions": [{"type": "open_map", "label": "Open map"}],
                    "follow_ups": ["Which reports changed?"],
                }
            )
        }
    )

    assert payload == {
        "reply": "Summary of intelligence in current scope.",
        "actions": [{"type": "open_map", "label": "Open map"}],
        "followUps": ["Which reports changed?"],
    }


def test_normalize_response_payload_accepts_graph_scope_action():
    payload = normalize_response_payload(
        {
            "reply": "Found graph-wide intelligence.",
            "actions": [
                {
                    "type": "apply_graph_query_scope",
                    "label": "Scope Explorer to this investigation",
                    "compiledAql": "FOR doc IN nodes_vertex_collection RETURN doc",
                    "queryPreview": "Graph investigation: South Africa",
                }
            ],
        }
    )

    assert payload["actions"] == [
        {
            "type": "apply_graph_query_scope",
            "label": "Scope Explorer to this investigation",
            "compiledAql": "FOR doc IN nodes_vertex_collection RETURN doc",
            "queryPreview": "Graph investigation: South Africa",
        }
    ]


def test_normalize_response_payload_accepts_save_and_apply_graph_scope_action():
    payload = normalize_response_payload(
        {
            "reply": "Prepared a saved query.",
            "actions": [
                {
                    "type": "save_and_apply_graph_query_scope",
                    "label": "Save query and scope Explorer",
                    "compiledAql": "FOR doc IN nodes_vertex_collection RETURN doc",
                    "queryPreview": "Graph investigation: South Africa",
                    "savedQueryName": "South Africa risk watch",
                    "savedQueryDescription": "Created from a natural-language Explorer Agent prompt.",
                    "alertingEnabled": False,
                }
            ],
        }
    )

    assert payload["actions"] == [
        {
            "type": "save_and_apply_graph_query_scope",
            "label": "Save query and scope Explorer",
            "compiledAql": "FOR doc IN nodes_vertex_collection RETURN doc",
            "queryPreview": "Graph investigation: South Africa",
            "savedQueryName": "South Africa risk watch",
            "savedQueryDescription": "Created from a natural-language Explorer Agent prompt.",
            "alertingEnabled": False,
        }
    ]


def test_tool_backed_fallback_returns_structured_graph_scope_summary():
    raw = service_module._synthesize_tool_backed_response(
        [
            {
                "role": "tool",
                "content": json.dumps(
                    {
                        "summary": {
                            "reportCount": 2,
                            "resultRows": 1,
                            "topLocations": [{"name": "South Africa", "count": 2}],
                        },
                        "reports": [{"name": "Report A"}, {"name": "Report B"}],
                        "explorerScope": {
                            "compiledAql": "FOR doc IN nodes_vertex_collection RETURN doc",
                            "queryPreview": "Graph investigation: South Africa",
                        },
                    }
                ),
            }
        ]
    )

    payload = json.loads(raw)

    assert "From the intelligence I inspected" in payload["reply"]
    assert "Report A" in payload["reply"]
    assert payload["actions"] == [
        {
            "type": "apply_graph_query_scope",
            "label": "Scope Explorer to this investigation",
            "reason": "Inspect the reports and entities returned by the graph-wide lookup.",
            "compiledAql": "FOR doc IN nodes_vertex_collection RETURN doc",
            "queryPreview": "Graph investigation: South Africa",
        }
    ]


def test_tool_backed_fallback_answers_individual_followup_from_facets():
    raw = service_module._synthesize_tool_backed_response(
        [
            {
                "role": "user",
                "content": json.dumps({"currentUserMessage": "Okay, what individuals are implicated?"}),
            },
            {
                "role": "tool",
                "content": json.dumps(
                    {
                        "reports": [{"name": "Report A", "entities": ["Fallback Entity"]}],
                        "facets": {
                            "possibleIndividuals": [{"name": "Jane Doe", "count": 2}],
                            "possibleActors": [{"name": "Operation Example", "count": 1}],
                        },
                    }
                ),
            },
        ]
    )

    payload = json.loads(raw)

    assert "Jane Doe" in payload["reply"]
    assert "no usable intelligence" not in payload["reply"].lower()


def test_tool_backed_fallback_answers_risk_area_followup_from_facets():
    raw = service_module._synthesize_tool_backed_response(
        [
            {
                "role": "user",
                "content": json.dumps({"currentUserMessage": "what are the risk areas for the events on the 30th?"}),
            },
            {
                "role": "tool",
                "content": json.dumps(
                    {
                        "reports": [{"name": "Report A"}],
                        "facets": {
                            "possibleRiskAreas": [{"name": "Johannesburg", "count": 2}],
                        },
                    }
                ),
            },
        ]
    )

    payload = json.loads(raw)

    assert "Johannesburg" in payload["reply"]
    assert "risk areas" in payload["reply"].lower()


def test_tool_aware_analysis_synthesizes_response_after_tool_use_without_final_text(monkeypatch):
    monkeypatch.setattr(service_module.settings, "backend_base_url", "https://api.example.test")

    calls = []

    async def fake_chat_completion(messages, tools=None, *, model=None):
        calls.append({"messages": list(messages), "tools": tools, "model": model})
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "search_intelligence_graph",
                                        "arguments": json.dumps({"query": "South Africa xenophobic events"}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        return {"choices": [{"message": {"content": ""}}]}

    async def fake_execute_tool(tool_name, arguments, session_id):
        assert tool_name == "search_intelligence_graph"
        assert session_id == "session-1"
        return {
            "summary": {"reportCount": 1, "resultRows": 1},
            "reports": [{"name": "Graph report"}],
            "explorerScope": {
                "compiledAql": "FOR doc IN nodes_vertex_collection RETURN doc",
                "queryPreview": "Graph investigation",
            },
        }

    async def fail_openai_analysis(*_args, **_kwargs):  # pragma: no cover - regression guard
        raise AssertionError("should synthesize from tool output instead of generic fallback")

    monkeypatch.setattr(service_module, "_chat_completion_request", fake_chat_completion)
    monkeypatch.setattr(service_module, "_execute_tool_call", fake_execute_tool)
    monkeypatch.setattr(service_module, "run_openai_analysis", fail_openai_analysis)

    raw = asyncio.run(service_module.run_tool_aware_analysis([{"role": "user", "content": "Investigate"}], "session-1"))
    payload = json.loads(raw)

    assert "From the intelligence I inspected" in payload["reply"]
    assert payload["actions"][0]["type"] == "apply_graph_query_scope"


def test_tool_calls_run_with_bounded_parallelism(monkeypatch):
    monkeypatch.setattr(service_module.settings, "backend_base_url", "https://api.example.test")
    monkeypatch.setattr(service_module.settings, "max_parallel_tool_calls", 3)
    chat_calls = 0
    active = 0
    max_active = 0

    async def fake_chat_completion(messages, tools=None, *, model=None):
        nonlocal chat_calls
        chat_calls += 1
        if chat_calls == 1:
            return {"choices": [{"message": {"content": "", "tool_calls": [
                {
                    "id": f"scope-{index}",
                    "type": "function",
                    "function": {"name": "list_scope_reports", "arguments": '{"limit":1}'},
                }
                for index in range(3)
            ]}}]}
        return {"choices": [{"message": {"content": '{"reply":"done","actions":[],"follow_ups":[]}'}}]}

    async def fake_execute_tool(*_args):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return {"reports": [{"name": "Scoped report"}]}

    monkeypatch.setattr(service_module, "_chat_completion_request", fake_chat_completion)
    monkeypatch.setattr(service_module, "_execute_tool_call", fake_execute_tool)

    result = asyncio.run(service_module.run_tool_aware_analysis(
        [{"role": "user", "content": "What does the current scope say?"}],
        "session-1",
    ))

    assert json.loads(result)["reply"] == "done"
    assert max_active == 3


def test_tool_aware_analysis_rescues_schema_only_tool_turn(monkeypatch):
    monkeypatch.setattr(service_module.settings, "backend_base_url", "https://api.example.test")

    calls = []
    executed_tools = []

    async def fake_chat_completion(messages, tools=None, *, model=None):
        calls.append({"messages": list(messages), "tools": tools, "model": model})
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-schema",
                                    "type": "function",
                                    "function": {"name": "graph_schema_context", "arguments": "{}"},
                                }
                            ],
                        }
                    }
                ]
            }
        return {"choices": [{"message": {"content": ""}}]}

    async def fake_execute_tool(tool_name, arguments, session_id):
        executed_tools.append((tool_name, arguments, session_id))
        if tool_name == "graph_schema_context":
            return {"schema": {"graphName": "lunargraph_graph"}}
        assert tool_name == "search_intelligence_graph"
        return {
            "reports": [{"name": "Rescued report"}],
            "facets": {"possibleRiskAreas": [{"name": "Johannesburg", "count": 1}]},
            "explorerScope": {
                "compiledAql": "FOR doc IN nodes_vertex_collection RETURN doc",
                "queryPreview": "Graph investigation",
            },
        }

    async def fail_openai_analysis(*_args, **_kwargs):  # pragma: no cover - regression guard
        raise AssertionError("should not fall back to a generic model call")

    monkeypatch.setattr(service_module, "_chat_completion_request", fake_chat_completion)
    monkeypatch.setattr(service_module, "_execute_tool_call", fake_execute_tool)
    monkeypatch.setattr(service_module, "run_openai_analysis", fail_openai_analysis)

    messages = build_prompt_messages(
        allow_ui_actions=False,
        conversation_history=[
            {"role": "user", "content": "What is happening on the 30th in SA?"},
            {"role": "assistant", "content": "Most relevant reports: Article: June 30 shutdown."},
        ],
        query_preview="query",
        summary={},
        context={},
        user_message="what are the risk areas for the events on the 30th?",
    )
    raw = asyncio.run(service_module.run_tool_aware_analysis(messages, "session-1"))
    payload = json.loads(raw)

    assert any(tool_name == "search_intelligence_graph" for tool_name, *_ in executed_tools)
    assert "Johannesburg" in payload["reply"]


def test_normalize_model_response_falls_back_to_plain_text_for_unstructured_output():
    payload = normalize_model_response("No structured JSON was returned.")

    assert payload == {
        "reply": "No structured JSON was returned.",
        "actions": [],
        "followUps": [],
    }


def test_normalize_model_response_strips_leaked_followups_from_plain_text():
    payload = normalize_model_response(
        'No, I did not write KQL for that.\n\nfollow_ups: [ "Show me the AQL logic?", "Search wider graph?" ]'
    )

    assert payload == {
        "reply": "No, I did not write KQL for that.",
        "actions": [],
        "followUps": [],
    }


def test_build_prompt_messages_keeps_ui_actions_disabled_until_allowed():
    messages = build_prompt_messages(
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
    assert prompt_payload["alwaysAllowedOptInActions"][0]["type"] == "apply_graph_query_scope"
    assert "apply_graph_query_scope" in prompt_payload["responseShape"]["actions"][0]["type"]
    assert "save_and_apply_graph_query_scope" in prompt_payload["responseShape"]["actions"][0]["type"]
    assert prompt_payload["conversationHistory"] == [{"role": "user", "content": "What matters?"}]
    assert any("allowUiActions=false" in instruction for instruction in prompt_payload["instructions"])
    assert any("KQL" in item and "AQL" in item for item in prompt_payload["toolPolicy"])
    assert any("full autonomy" in item for item in prompt_payload["toolPolicy"])
    assert any("do not ask permission" in item for item in prompt_payload["instructions"])


def test_broad_current_request_is_not_scope_limited_by_history():
    messages = build_prompt_messages(
        allow_ui_actions=False,
        conversation_history=[
            {"role": "user", "content": "What does the current scope say?"},
            {"role": "assistant", "content": "I inspected these scoped reports."},
        ],
        query_preview="FOR doc IN reports RETURN doc",
        summary={},
        context={},
        user_message="What's happening in South Africa today?",
    )

    assert service_module._request_wants_public_web_context(messages) is True
    assert service_module._request_wants_graph_wide_context(messages) is True


def test_latest_scope_limited_request_still_stays_in_scope():
    messages = build_prompt_messages(
        allow_ui_actions=False,
        conversation_history=[
            {"role": "user", "content": "What's happening in South Africa today?"},
            {"role": "assistant", "content": "I searched the wider graph and web."},
        ],
        query_preview="FOR doc IN reports RETURN doc",
        summary={},
        context={},
        user_message="What do these current scope reports say?",
    )

    assert service_module._request_wants_public_web_context(messages) is False
    assert service_module._request_wants_graph_wide_context(messages) is False


def test_build_prompt_messages_declares_allowed_actions_when_enabled():
    messages = build_prompt_messages(
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
        "apply_graph_query_scope",
        "save_and_apply_graph_query_scope",
    ]
    assert any("search_intelligence_graph" in item for item in prompt_payload["toolPolicy"])
    assert any("search_public_web" in item for item in prompt_payload["toolPolicy"])
    assert any("When unsure between scoped data and the full graph" in item for item in prompt_payload["toolPolicy"])


def test_tool_specs_include_public_web_search():
    tool_names = [tool["function"]["name"] for tool in service_module._tool_specs()]

    assert "search_public_web" in tool_names


def test_safe_http_url_rejects_internal_and_special_hosts():
    assert service_module._safe_http_url("http://[::1]/") == ""
    assert service_module._safe_http_url("http://169.254.169.254/latest") == ""
    assert service_module._safe_http_url("http://0.0.0.0/") == ""
    assert service_module._safe_http_url("http://2130706433/") == ""
    assert service_module._safe_http_url("http://service.localhost/") == ""
    assert service_module._safe_http_url("https://example.test/report") == "https://example.test/report"


def test_request_models_reject_oversized_context_and_evidence_payloads():
    with pytest.raises(ValueError):
        ExplorerAgentRespondRequest(
            sessionId="session-1",
            allowUiActions=False,
            conversationHistory=[],
            queryPreview="FOR doc IN reports RETURN doc",
            queryContext={"blob": "x" * 25000},
            querySummary={},
            currentUserMessage="What matters?",
        )


def test_safe_route_request_strips_sensitive_aoi_metadata():
    request = SafeRouteAreaRiskResearchRequest(
        sessionId="internal-session",
        aoi={
            "bounds": {"minLat": -34.2, "minLon": 18.2, "maxLat": -33.5, "maxLon": 19.0},
            "center": {"lat": -33.9, "lon": 18.6},
            "countryHints": ["South Africa"],
            "tenantId": "tenant-secret",
            "clientId": "client-secret",
            "route": {"waypoints": ["protected"]},
            "labelContext": {
                "place": "Cape Town",
                "queryPreview": "Public safety watch",
                "userEmail": "private@example.test",
            },
        },
        evidence=[],
    )

    serialized = json.dumps(request.aoi)
    assert "tenant-secret" not in serialized
    assert "client-secret" not in serialized
    assert "protected" not in serialized
    assert "private@example.test" not in serialized
    assert request.aoi["labelContext"]["place"] == "Cape Town"

    prompt = service_module.build_safe_route_area_risk_web_prompt(
        aoi=request.aoi,
        evidence=[],
        max_zones=3,
    )
    assert "internal-session" not in prompt

    with pytest.raises(ValueError):
        SafeRouteAreaRiskResearchRequest(
            sessionId="session-1",
            aoi={"bounds": {"minLat": 0, "minLon": 0, "maxLat": 1, "maxLon": 1}},
            evidence=[{"title": f"Evidence {index}", "url": "https://example.test"} for index in range(80)],
            maxZones=8,
        )


def test_explorer_request_strips_private_tenant_context_before_prompting():
    request = ExplorerAgentRespondRequest(
        sessionId="session-1",
        conversationHistory=[],
        queryPreview="Current scope",
        queryContext={
            "clientId": "client-secret",
            "clientName": "Sensitive Client",
            "selectedCountry": "South Africa",
            "nested": {"userEmail": "analyst@example.test", "moduleScope": ["situational"]},
        },
        querySummary={},
        currentUserMessage="What is happening?",
    )

    serialized = json.dumps(request.queryContext)
    assert "client-secret" not in serialized
    assert "Sensitive Client" not in serialized
    assert "analyst@example.test" not in serialized
    assert request.queryContext["selectedCountry"] == "South Africa"
    assert request.queryContext["nested"]["moduleScope"] == ["situational"]


def test_respond_filters_ordinary_ui_actions_when_ui_actions_disabled(monkeypatch):
    async def fake_tool_aware_analysis(messages, session_id=None):
        return json.dumps(
            {
                "reply": "Found graph-wide intelligence.",
                "actions": [
                    {"type": "open_map", "label": "Open map"},
                    {"type": "focus_country", "label": "Focus Germany", "countryName": "Germany"},
                    {
                        "type": "apply_graph_query_scope",
                        "label": "Scope Explorer to this investigation",
                        "compiledAql": "FOR doc IN nodes_vertex_collection RETURN doc",
                        "queryPreview": "Graph investigation",
                    },
                    {
                        "type": "save_and_apply_graph_query_scope",
                        "label": "Save query and scope Explorer",
                        "compiledAql": "FOR doc IN nodes_vertex_collection RETURN doc",
                        "queryPreview": "Graph investigation",
                        "savedQueryName": "Unexpected saved query",
                    },
                ],
                "follow_ups": ["Should be kept"],
            }
        )

    monkeypatch.setattr(service_module, "run_tool_aware_analysis", fake_tool_aware_analysis)

    payload = asyncio.run(
        service_module.respond(
            session_id="session-1",
            allow_ui_actions=False,
            conversation_history=[],
            query_preview="FOR doc IN reports RETURN doc",
            summary={},
            context={},
            user_message="Investigate South Africa",
        )
    )

    assert payload["actions"] == []


def test_respond_allows_save_scope_action_when_user_explicitly_asks_to_save(monkeypatch):
    async def fake_tool_aware_analysis(messages, session_id=None):
        return json.dumps(
            {
                "reply": "Prepared the saved query.",
                "actions": [
                    {
                        "type": "save_and_apply_graph_query_scope",
                        "label": "Save query and scope Explorer",
                        "compiledAql": "FOR doc IN nodes_vertex_collection RETURN doc",
                        "queryPreview": "Graph investigation",
                        "savedQueryName": "South Africa watch",
                    }
                ],
                "follow_ups": [],
            }
        )

    monkeypatch.setattr(service_module, "run_tool_aware_analysis", fake_tool_aware_analysis)

    payload = asyncio.run(
        service_module.respond(
            session_id="session-1",
            allow_ui_actions=False,
            conversation_history=[],
            query_preview="FOR doc IN reports RETURN doc",
            summary={},
            context={},
            user_message="Save this South Africa investigation as a query and scope Explorer to it",
        )
    )

    assert payload["actions"] == []


def test_tool_aware_analysis_allows_verified_graph_scope_action(monkeypatch):
    monkeypatch.setattr(service_module.settings, "backend_base_url", "https://api.example.test")

    async def fake_chat_completion(messages, tools=None, *, model=None):
        if not service_module._tool_was_called(messages, "search_intelligence_graph"):
            return {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-graph",
                                    "type": "function",
                                    "function": {
                                        "name": "search_intelligence_graph",
                                        "arguments": json.dumps({"query": "South Africa"}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "reply": "Found graph-wide intelligence.",
                                "actions": [
                                    {
                                        "type": "apply_graph_query_scope",
                                        "label": "Scope Explorer to this investigation",
                                        "compiledAql": "FOR doc IN nodes_vertex_collection RETURN doc",
                                        "queryPreview": "Graph investigation",
                                    }
                                ],
                                "follow_ups": [],
                            }
                        )
                    }
                }
            ]
        }

    async def fake_execute_tool(tool_name, arguments, session_id):
        assert tool_name == "search_intelligence_graph"
        return {
            "reports": [{"name": "Graph report"}],
            "explorerScope": {
                "compiledAql": "FOR doc IN nodes_vertex_collection RETURN doc",
                "queryPreview": "Graph investigation",
            },
        }

    monkeypatch.setattr(service_module, "_chat_completion_request", fake_chat_completion)
    monkeypatch.setattr(service_module, "_execute_tool_call", fake_execute_tool)

    raw = asyncio.run(
        service_module.run_tool_aware_analysis(
            [{"role": "user", "content": "Investigate South Africa"}],
            "session-1",
        )
    )
    payload = service_module.normalize_model_response(raw)
    payload["actions"] = service_module._filter_response_actions_for_ui_policy(
        payload["actions"],
        allow_ui_actions=False,
        user_message="Investigate South Africa",
    )

    assert payload["actions"] == [
        {
            "type": "apply_graph_query_scope",
            "label": "Scope Explorer to this investigation",
            "compiledAql": "FOR doc IN nodes_vertex_collection RETURN doc",
            "queryPreview": "Graph investigation",
        }
    ]


def test_tool_aware_analysis_strips_unverified_graph_scope_action(monkeypatch):
    monkeypatch.setattr(service_module.settings, "backend_base_url", "https://api.example.test")

    async def fake_chat_completion(messages, tools=None, *, model=None):
        if not service_module._tool_was_called(messages, "search_intelligence_graph"):
            return {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-graph",
                                    "type": "function",
                                    "function": {
                                        "name": "search_intelligence_graph",
                                        "arguments": json.dumps({"query": "South Africa"}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "reply": "Found graph-wide intelligence.",
                                "actions": [
                                    {
                                        "type": "apply_graph_query_scope",
                                        "label": "Scope Explorer to this investigation",
                                        "compiledAql": "FOR doc IN other_collection RETURN doc",
                                        "queryPreview": "Hallucinated scope",
                                    }
                                ],
                                "follow_ups": [],
                            }
                        )
                    }
                }
            ]
        }

    async def fake_execute_tool(tool_name, arguments, session_id):
        assert tool_name == "search_intelligence_graph"
        return {
            "reports": [{"name": "Graph report"}],
            "explorerScope": {
                "compiledAql": "FOR doc IN nodes_vertex_collection RETURN doc",
                "queryPreview": "Graph investigation",
            },
        }

    monkeypatch.setattr(service_module, "_chat_completion_request", fake_chat_completion)
    monkeypatch.setattr(service_module, "_execute_tool_call", fake_execute_tool)

    raw = asyncio.run(
        service_module.run_tool_aware_analysis(
            [{"role": "user", "content": "Investigate South Africa"}],
            "session-1",
        )
    )
    payload = service_module.normalize_model_response(raw)

    assert payload["actions"] == []


def test_public_web_error_or_disabled_payloads_are_not_evidence():
    assert service_module._tool_payloads_have_public_web_evidence([
        {
            "role": "tool",
            "content": json.dumps(
                {
                    "tool": "search_public_web",
                    "status": "error",
                    "summary": "Public web research failed for this turn.",
                    "findings": [],
                    "sources": [],
                }
            ),
        }
    ]) is False
    assert service_module._tool_payloads_have_public_web_evidence([
        {
            "role": "tool",
            "content": json.dumps(
                {
                    "tool": "search_public_web",
                    "status": "disabled",
                    "summary": "Public web research is disabled.",
                    "findings": [],
                    "sources": [],
                }
            ),
        }
    ]) is False
    assert service_module._tool_payloads_have_public_web_evidence([
        {
            "role": "tool",
            "content": json.dumps(
                {
                    "tool": "search_public_web",
                    "status": "success",
                    "summary": "Current public reporting adds context.",
                    "findings": [{"claim": "A public source reported an update.", "url": "https://example.test/update"}],
                    "sources": [],
                    "verifiedSourceUrls": ["https://example.test/update"],
                }
            ),
        }
    ]) is True


def test_public_web_summary_without_sources_is_not_evidence():
    payload = service_module.normalize_public_web_search_payload(
        {
            "summary": "Uncited public web summary.",
            "findings": [{"claim": "Uncited claim"}],
            "sources": [{"title": "Source without URL"}],
        },
        query="South Africa",
    )

    assert payload["findings"] == []
    assert payload["sources"] == []
    assert service_module._tool_payloads_have_public_web_evidence([
        {"role": "tool", "content": json.dumps({**payload, "status": "success"})}
    ]) is False


def test_model_supplied_web_urls_without_search_annotations_are_rejected():
    payload = service_module.normalize_public_web_search_payload(
        {
            "summary": "Model-only claim.",
            "findings": [{"claim": "Invented finding", "url": "https://hallucinated.example/story"}],
            "sources": [{"title": "Invented source", "url": "https://hallucinated.example/story"}],
        },
        query="test",
    )

    assert payload["findings"] == []
    assert payload["sources"] == []
    assert payload["verifiedSourceUrls"] == []


def test_tool_aware_analysis_refuses_broad_answer_without_backend_tools(monkeypatch):
    monkeypatch.setattr(service_module.settings, "backend_base_url", "")

    async def fail_openai_analysis(*_args, **_kwargs):  # pragma: no cover - regression guard
        raise AssertionError("broad/current requests should not fall back to generic model answers without tools")

    monkeypatch.setattr(service_module, "run_openai_analysis", fail_openai_analysis)

    raw = asyncio.run(
        service_module.run_tool_aware_analysis(
            [{"role": "user", "content": "What's happening in South Africa today?"}],
            "session-1",
        )
    )
    payload = json.loads(raw)

    assert payload["actions"] == []
    assert "tool session is not available" in payload["reply"]


def test_tool_aware_analysis_refuses_broad_answer_when_tool_orchestration_fails(monkeypatch):
    monkeypatch.setattr(service_module.settings, "backend_base_url", "https://api.example.test")

    async def fail_chat_completion(*_args, **_kwargs):
        raise RuntimeError("provider outage")

    async def fail_openai_analysis(*_args, **_kwargs):  # pragma: no cover - regression guard
        raise AssertionError("broad/current requests should not fall back to generic model answers after tool failure")

    monkeypatch.setattr(service_module, "_chat_completion_request", fail_chat_completion)
    monkeypatch.setattr(service_module, "run_openai_analysis", fail_openai_analysis)

    raw = asyncio.run(
        service_module.run_tool_aware_analysis(
            [{"role": "user", "content": "What's happening in South Africa today?"}],
            "session-1",
        )
    )
    payload = json.loads(raw)

    assert payload["actions"] == []
    assert "could not complete the required Intelligence Graph/web lookup" in payload["reply"]


def test_tool_aware_analysis_rejects_model_answer_when_graph_and_web_tools_both_fail(monkeypatch):
    monkeypatch.setattr(service_module.settings, "backend_base_url", "https://api.example.test")
    monkeypatch.setattr(service_module.settings, "web_research_enabled", True)
    calls = 0

    async def fake_chat_completion(messages, tools=None, *, model=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"choices": [{"message": {"content": "", "tool_calls": [{
                "id": "graph", "type": "function",
                "function": {"name": "search_intelligence_graph", "arguments": '{"query":"SA today"}'},
            }]}}]}
        if calls == 2:
            return {"choices": [{"message": {"content": '{"reply":"premature","actions":[]}'}}]}
        if calls == 3:
            return {"choices": [{"message": {"content": "", "tool_calls": [{
                "id": "web", "type": "function",
                "function": {"name": "search_public_web", "arguments": '{"query":"SA today"}'},
            }]}}]}
        return {"choices": [{"message": {"content": '{"reply":"Ungrounded generic answer","actions":[]}'}}]}

    async def failed_tool(tool_name, *_args):
        return {"tool": tool_name, "status": "error", "error": "lookup unavailable"}

    monkeypatch.setattr(service_module, "_chat_completion_request", fake_chat_completion)
    monkeypatch.setattr(service_module, "_execute_tool_call", failed_tool)

    payload = json.loads(asyncio.run(service_module.run_tool_aware_analysis(
        [{"role": "user", "content": "What's happening in South Africa today?"}],
        "session-1",
    )))
    assert "Ungrounded" not in payload["reply"]
    assert "could not complete the required Intelligence Graph/web lookup" in payload["reply"]


def test_tool_aware_analysis_rejects_ungrounded_current_scope_answer(monkeypatch):
    monkeypatch.setattr(service_module.settings, "backend_base_url", "https://api.example.test")
    calls = 0

    async def fake_chat_completion(messages, tools=None, *, model=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"choices": [{"message": {"content": "", "tool_calls": [{
                "id": "scope", "type": "function",
                "function": {"name": "list_scope_reports", "arguments": "{}"},
            }]}}]}
        return {"choices": [{"message": {"content": '{"reply":"Generic scope answer","actions":[]}'}}]}

    async def failed_scope_tool(*_args):
        return {"tool": "list_scope_reports", "status": "error", "error": "scope unavailable"}

    monkeypatch.setattr(service_module, "_chat_completion_request", fake_chat_completion)
    monkeypatch.setattr(service_module, "_execute_tool_call", failed_scope_tool)

    payload = json.loads(asyncio.run(service_module.run_tool_aware_analysis(
        [{"role": "user", "content": "What does the current scope say?"}],
        "session-1",
    )))
    assert "Generic scope answer" not in payload["reply"]
    assert "could not complete" in payload["reply"]


def test_execute_public_web_search_tool_normalizes_web_research(monkeypatch):
    monkeypatch.setattr(service_module.settings, "web_research_enabled", True)
    monkeypatch.setattr(service_module.settings, "model", "gpt-5")

    captured = {}

    async def fake_web_research(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        return json.dumps(
            {
                "summary": "Public reporting says protests and coalition pressure are driving the situation.",
                "findings": [
                    {
                        "claim": "Authorities reported protest activity in Johannesburg.",
                        "source": "Example News",
                        "url": "https://example.test/jhb",
                        "date": "2026-06-28",
                    }
                ],
                "sources": [{"title": "Johannesburg update", "url": "https://example.test/jhb"}],
                "verifiedSourceUrls": ["https://example.test/jhb"],
            }
        )

    monkeypatch.setattr(service_module, "run_openai_web_research", fake_web_research)

    result = asyncio.run(
        service_module._execute_tool_call(
            "search_public_web",
            {"query": "what is happening in South Africa", "region": "South Africa", "max_sources": 4},
            session_id=None,
        )
    )

    assert result["tool"] == "search_public_web"
    assert result["status"] == "success"
    assert result["findings"][0]["url"] == "https://example.test/jhb"
    assert captured["kwargs"]["model"] == "gpt-5"
    assert captured["kwargs"]["context_size"] == service_module.settings.web_search_context_size
    assert "what is happening in South Africa" in captured["prompt"]


def test_run_graph_read_query_rejects_write_aql_before_backend(monkeypatch):
    async def fail_backend_call(*_args, **_kwargs):  # pragma: no cover - regression guard
        raise AssertionError("unsafe write AQL should not reach the backend")

    monkeypatch.setattr(service_module, "_call_backend_tool", fail_backend_call)

    result = asyncio.run(
        service_module._execute_tool_call(
            "run_graph_read_query",
            {"query": "FOR doc IN nodes_vertex_collection REMOVE doc IN nodes_vertex_collection RETURN OLD"},
            session_id="session-1",
        )
    )

    assert result["tool"] == "run_graph_read_query"
    assert result["status"] == "error"
    assert "read-only" in result["error"]


def test_backend_tool_errors_do_not_expose_response_body(monkeypatch):
    monkeypatch.setattr(service_module.settings, "backend_base_url", "https://backend.example.test")

    class FakeResponse:
        status_code = 500
        text = "private tenant token=secret"
        content = b"private tenant token=secret"

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(service_module.httpx, "AsyncClient", FakeClient)

    with pytest.raises(RuntimeError) as error:
        asyncio.run(service_module._call_backend_tool("/internal", {"session_id": "session-1"}))
    assert "private tenant" not in str(error.value)
    assert "token=secret" not in str(error.value)
    assert "HTTP 500" in str(error.value)


def test_public_web_research_preserves_response_annotation_sources(monkeypatch):
    monkeypatch.setattr(service_module.settings, "openai_api_key", "test-key")

    class FakeResponse:
        status_code = 200
        text = ""
        content = b"{}"

        def json(self):
            return {
                "output_text": json.dumps({"summary": "Public reporting adds context.", "findings": [], "sources": []}),
                "output": [
                    {
                        "content": [
                            {
                                "text": {
                                    "value": "Public reporting adds context.",
                                    "annotations": [
                                        {
                                            "type": "url_citation",
                                            "title": "Official update",
                                            "url": "https://example.test/official-update",
                                        }
                                    ],
                                }
                            }
                        ]
                    }
                ],
            }

    async def fake_post_responses_request(_client, *, headers, payload):
        assert headers["Authorization"] == "Bearer test-key"
        assert payload["tools"]
        return FakeResponse()

    monkeypatch.setattr(service_module, "_post_responses_request", fake_post_responses_request)

    raw = asyncio.run(service_module.run_openai_web_research("Find current public context."))
    payload = service_module.normalize_public_web_search_payload(json.loads(raw), query="South Africa")

    assert payload["sources"] == [
        {"title": "Official update", "url": "https://example.test/official-update"}
    ]
    assert service_module._public_web_payload_has_evidence({**payload, "status": "success"}) is True


def test_tool_aware_analysis_requires_web_after_graph_for_current_public_context(monkeypatch):
    monkeypatch.setattr(service_module.settings, "backend_base_url", "https://api.example.test")
    monkeypatch.setattr(service_module.settings, "web_research_enabled", True)

    calls = []
    executed_tools = []

    async def fake_chat_completion(messages, tools=None, *, model=None):
        calls.append({"messages": list(messages), "tools": tools, "model": model})
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-graph",
                                    "type": "function",
                                    "function": {
                                        "name": "search_intelligence_graph",
                                        "arguments": json.dumps({"query": "South Africa"}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        if len(calls) == 2:
            return {"choices": [{"message": {"content": "{\"reply\":\"graph-only\",\"actions\":[],\"follow_ups\":[]}"}}]}
        if len(calls) == 3:
            last_system = calls[-1]["messages"][-1]["content"]
            assert "search_public_web" in last_system
            return {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-web",
                                    "type": "function",
                                    "function": {
                                        "name": "search_public_web",
                                        "arguments": json.dumps({"query": "South Africa latest developments"}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "reply": "Graph evidence and public web reporting both indicate a developing South Africa situation.",
                                "actions": [],
                                "follow_ups": [],
                            }
                        )
                    }
                }
            ]
        }

    async def fake_execute_tool(tool_name, arguments, session_id):
        executed_tools.append((tool_name, arguments, session_id))
        if tool_name == "search_intelligence_graph":
            return {
                "summary": {"reportCount": 1},
                "reports": [{"name": "Graph report", "contentSnippet": "Graph-backed incident context."}],
                "explorerScope": {"compiledAql": "FOR doc IN nodes_vertex_collection RETURN doc"},
            }
        assert tool_name == "search_public_web"
        return {
            "tool": "search_public_web",
            "status": "success",
            "summary": "Public web reporting adds current context.",
            "findings": [{"claim": "A current public development was reported.", "url": "https://example.test"}],
            "sources": [],
            "verifiedSourceUrls": ["https://example.test"],
        }

    monkeypatch.setattr(service_module, "_chat_completion_request", fake_chat_completion)
    monkeypatch.setattr(service_module, "_execute_tool_call", fake_execute_tool)

    raw = asyncio.run(
        service_module.run_tool_aware_analysis(
            [{"role": "user", "content": "What's happening in South Africa?"}],
            "session-1",
        )
    )
    payload = json.loads(raw)

    assert [tool_name for tool_name, *_ in executed_tools] == ["search_intelligence_graph", "search_public_web"]
    assert "public web" in payload["reply"].lower()


def test_tool_aware_analysis_allows_final_answer_after_failed_web_attempt(monkeypatch):
    monkeypatch.setattr(service_module.settings, "backend_base_url", "https://api.example.test")
    monkeypatch.setattr(service_module.settings, "web_research_enabled", True)

    calls = []
    executed_tools = []

    async def fake_chat_completion(messages, tools=None, *, model=None):
        calls.append({"messages": list(messages), "tools": tools, "model": model})
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-graph",
                                    "type": "function",
                                    "function": {
                                        "name": "search_intelligence_graph",
                                        "arguments": json.dumps({"query": "South Africa"}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        if len(calls) == 2:
            return {"choices": [{"message": {"content": "{\"reply\":\"premature graph-only\",\"actions\":[],\"follow_ups\":[]}"}}]}
        if len(calls) == 3:
            last_system = calls[-1]["messages"][-1]["content"]
            assert "search_public_web" in last_system
            return {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-web",
                                    "type": "function",
                                    "function": {
                                        "name": "search_public_web",
                                        "arguments": json.dumps({"query": "South Africa latest developments"}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "reply": "Graph evidence is available, but public web research failed this turn.",
                                "actions": [],
                                "follow_ups": [],
                            }
                        )
                    }
                }
            ]
        }

    async def fake_execute_tool(tool_name, arguments, session_id):
        executed_tools.append((tool_name, arguments, session_id))
        if tool_name == "search_intelligence_graph":
            return {
                "summary": {"reportCount": 1},
                "reports": [{"name": "Graph report", "contentSnippet": "Graph-backed incident context."}],
                "explorerScope": {"compiledAql": "FOR doc IN nodes_vertex_collection RETURN doc"},
            }
        assert tool_name == "search_public_web"
        return {
            "tool": "search_public_web",
            "status": "error",
            "summary": "Public web research failed for this turn.",
            "findings": [],
            "sources": [],
        }

    monkeypatch.setattr(service_module, "_chat_completion_request", fake_chat_completion)
    monkeypatch.setattr(service_module, "_execute_tool_call", fake_execute_tool)

    raw = asyncio.run(
        service_module.run_tool_aware_analysis(
            [{"role": "user", "content": "What's happening in South Africa today?"}],
            "session-1",
        )
    )
    payload = json.loads(raw)

    assert [tool_name for tool_name, *_ in executed_tools] == ["search_intelligence_graph", "search_public_web"]
    assert "web research failed" in payload["reply"]


def test_tool_aware_analysis_broad_request_does_not_stop_at_current_scope(monkeypatch):
    monkeypatch.setattr(service_module.settings, "backend_base_url", "https://api.example.test")
    monkeypatch.setattr(service_module.settings, "web_research_enabled", False)

    calls = []
    executed_tools = []

    async def fake_chat_completion(messages, tools=None, *, model=None):
        calls.append({"messages": list(messages), "tools": tools, "model": model})
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-scope",
                                    "type": "function",
                                    "function": {
                                        "name": "list_scope_reports",
                                        "arguments": json.dumps({"limit": 4}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        if len(calls) == 2:
            return {"choices": [{"message": {"content": "{\"reply\":\"scope-only\",\"actions\":[],\"follow_ups\":[]}"}}]}
        if len(calls) == 3:
            last_system = calls[-1]["messages"][-1]["content"]
            assert "Do not stop at current-scope evidence" in last_system
            assert "search_intelligence_graph" in last_system
            return {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-graph",
                                    "type": "function",
                                    "function": {
                                        "name": "search_intelligence_graph",
                                        "arguments": json.dumps({"query": "South Africa today"}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        return {"choices": [{"message": {"content": "{\"reply\":\"graph-wide final\",\"actions\":[],\"follow_ups\":[]}"}}]}

    async def fake_execute_tool(tool_name, arguments, session_id):
        executed_tools.append((tool_name, arguments, session_id))
        if tool_name == "list_scope_reports":
            return {"reports": [{"name": "Scoped report", "contentSnippet": "Only the current scope."}]}
        assert tool_name == "search_intelligence_graph"
        return {
            "reports": [{"name": "Wider graph report", "contentSnippet": "Graph-wide South Africa context."}],
            "explorerScope": {"compiledAql": "FOR doc IN nodes_vertex_collection RETURN doc"},
        }

    monkeypatch.setattr(service_module, "_chat_completion_request", fake_chat_completion)
    monkeypatch.setattr(service_module, "_execute_tool_call", fake_execute_tool)

    raw = asyncio.run(
        service_module.run_tool_aware_analysis(
            [{"role": "user", "content": "What's happening in South Africa today?"}],
            "session-1",
        )
    )
    payload = json.loads(raw)

    assert [tool_name for tool_name, *_ in executed_tools] == ["list_scope_reports", "search_intelligence_graph"]
    assert payload["reply"] == "graph-wide final"


def test_tool_aware_analysis_allows_web_after_attempted_empty_graph_search(monkeypatch):
    monkeypatch.setattr(service_module.settings, "backend_base_url", "https://api.example.test")
    monkeypatch.setattr(service_module.settings, "web_research_enabled", True)

    calls = []
    executed_tools = []

    async def fake_chat_completion(messages, tools=None, *, model=None):
        calls.append({"messages": list(messages), "tools": tools, "model": model})
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-graph",
                                    "type": "function",
                                    "function": {
                                        "name": "search_intelligence_graph",
                                        "arguments": json.dumps({"query": "South Africa"}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        if len(calls) == 2:
            return {"choices": [{"message": {"content": "{\"reply\":\"premature\",\"actions\":[],\"follow_ups\":[]}"}}]}
        if len(calls) == 3:
            return {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-web",
                                    "type": "function",
                                    "function": {
                                        "name": "search_public_web",
                                        "arguments": json.dumps({"query": "South Africa latest developments"}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        return {"choices": [{"message": {"content": "{\"reply\":\"web-backed final\",\"actions\":[],\"follow_ups\":[]}"}}]}

    async def fake_execute_tool(tool_name, arguments, session_id):
        executed_tools.append((tool_name, arguments, session_id))
        if tool_name == "search_intelligence_graph":
            return {"error": "No graph evidence returned."}
        assert tool_name == "search_public_web"
        return {
            "tool": "search_public_web",
            "status": "success",
            "summary": "Public web reporting adds current context.",
            "findings": [{"claim": "A current public development was reported.", "url": "https://example.test"}],
            "sources": [],
            "verifiedSourceUrls": ["https://example.test"],
        }

    monkeypatch.setattr(service_module, "_chat_completion_request", fake_chat_completion)
    monkeypatch.setattr(service_module, "_execute_tool_call", fake_execute_tool)

    raw = asyncio.run(
        service_module.run_tool_aware_analysis(
            [{"role": "user", "content": "What's happening in South Africa today?"}],
            "session-1",
        )
    )
    payload = json.loads(raw)

    assert [tool_name for tool_name, *_ in executed_tools] == ["search_intelligence_graph", "search_public_web"]
    assert payload["reply"] == "web-backed final"


def test_responses_payload_applies_reasoning_and_token_bounds(monkeypatch):
    monkeypatch.setattr(service_module.settings, "area_risk_max_output_tokens", 5000)
    monkeypatch.setattr(service_module.settings, "area_risk_reasoning_effort", "medium")

    payload = service_module._responses_payload("Prompt text", model="gpt-5-mini")

    assert payload == {
        "model": "gpt-5-mini",
        "input": "Prompt text",
        "max_output_tokens": 1400,
        "reasoning": {"effort": "medium"},
    }


def test_post_responses_request_retries_without_unsupported_reasoning():
    class FakeResponse:
        def __init__(self, status_code, text=""):
            self.status_code = status_code
            self.text = text

    class FakeClient:
        def __init__(self):
            self.payloads = []

        async def post(self, _url, *, headers, json):
            self.payloads.append(dict(json))
            if len(self.payloads) == 1:
                return FakeResponse(400, "Unsupported parameter: reasoning")
            return FakeResponse(200)

    client = FakeClient()
    response = asyncio.run(
        service_module._post_responses_request(
            client,
            headers={"Authorization": "Bearer test"},
            payload={"model": "gpt-5-mini", "input": "prompt", "reasoning": {"effort": "low"}},
        )
    )

    assert response.status_code == 200
    assert "reasoning" in client.payloads[0]
    assert "reasoning" not in client.payloads[1]


def test_chat_completion_request_applies_gpt5_reasoning_effort(monkeypatch):
    monkeypatch.setattr(service_module.settings, "openai_api_key", "test-key")
    monkeypatch.setattr(service_module.settings, "chat_reasoning_effort", "low")

    class FakeResponse:
        status_code = 200
        text = ""
        content = b"{}"

        def json(self):
            return {"choices": [{"message": {"content": "{\"reply\":\"ok\",\"actions\":[]}"}}]}

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout
            self.payloads = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, *, headers, json):
            assert headers["Authorization"] == "Bearer test-key"
            self.payloads.append(dict(json))
            return FakeResponse()

    fake_client = FakeClient(timeout=0)
    monkeypatch.setattr(service_module.httpx, "AsyncClient", lambda timeout: fake_client)

    data = asyncio.run(service_module._chat_completion_request([{"role": "user", "content": "Hello"}], model="gpt-5.1"))

    assert data["choices"][0]["message"]["content"]
    assert fake_client.payloads[0]["model"] == "gpt-5.1"
    assert fake_client.payloads[0]["max_completion_tokens"] == service_module._bounded_chat_completion_tokens()
    assert fake_client.payloads[0]["reasoning_effort"] == "low"


def test_chat_completion_request_retries_without_unsupported_reasoning_effort(monkeypatch):
    monkeypatch.setattr(service_module.settings, "openai_api_key", "test-key")
    monkeypatch.setattr(service_module.settings, "chat_reasoning_effort", "minimal")

    class FakeResponse:
        def __init__(self, status_code, text="", payload=None):
            self.status_code = status_code
            self.text = text
            self.content = b"{}"
            self._payload = payload or {"choices": [{"message": {"content": "{\"reply\":\"ok\"}"}}]}

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout
            self.payloads = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, *, headers, json):
            self.payloads.append(dict(json))
            if len(self.payloads) == 1:
                return FakeResponse(400, "Unsupported value: 'reasoning_effort'")
            return FakeResponse(200)

    fake_client = FakeClient(timeout=0)
    monkeypatch.setattr(service_module.httpx, "AsyncClient", lambda timeout: fake_client)

    data = asyncio.run(service_module._chat_completion_request([{"role": "user", "content": "Hello"}], model="gpt-5.1"))

    assert data["choices"][0]["message"]["content"]
    assert "reasoning_effort" in fake_client.payloads[0]
    assert "reasoning_effort" not in fake_client.payloads[1]


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
        aoi={"bounds": {"minLat": 0, "minLon": 0, "maxLat": 1, "maxLon": 1}},
        evidence=evidence,
        max_zones=12,
    )

    payload = json.loads(prompt.splitlines()[-1])
    assert payload["maxZones"] == 4
    assert len(payload["seedEvidence"]) == 3
    assert len(payload["seedEvidence"][0]["snippet"]) <= 420


def test_area_risk_evidence_prompt_uses_bounded_evidence_and_zone_caps(monkeypatch):
    monkeypatch.setattr(service_module.settings, "area_risk_max_evidence_items", 2)
    monkeypatch.setattr(service_module.settings, "area_risk_max_zones_per_request", 3)
    evidence = [
        {
            "title": f"Risk report {index}",
            "url": f"https://example.test/{index}",
            "source": "Example",
            "snippet": "A" * 900,
        }
        for index in range(5)
    ]

    prompt = service_module.build_safe_route_area_risk_evidence_prompt(
        aoi={"bounds": {"minLat": 0, "minLon": 0, "maxLat": 1, "maxLon": 1}},
        evidence=evidence,
        max_zones=12,
    )

    payload = json.loads(prompt.split("USER:\n", 1)[1])
    assert payload["maxZones"] == 3
    assert len(payload["evidence"]) == 2
    assert len(payload["evidence"][0]["snippet"]) <= 420


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
                    "evidence_urls": ["https://example.test/khayelitsha"],
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
                    "evidenceUrls": ["https://example.test/manenberg"],
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


def test_area_risk_payload_normalization_removes_semantic_spatial_duplicates_only():
    payload = service_module.normalize_safe_route_area_risk_payload(
        {
            "zones": [
                {
                    "label": "Central Station robbery hotspot",
                    "lat": -33.925,
                    "lon": 18.424,
                        "radius_m": 1400,
                        "severity": "high",
                        "evidence_urls": ["https://example.test/original"],
                },
                {
                    "label": "Central Station robbery hotspot zone",
                    "lat": -33.9255,
                    "lon": 18.4245,
                    "radius_m": 300,
                    "severity": "high",
                    "notes": "More specific boundary supported by two public reports.",
                    "evidence_urls": ["https://example.test/one", "https://example.test/two"],
                },
                {
                    "label": "Flood-prone underpass",
                    "lat": -33.925,
                    "lon": 18.424,
                        "radius_m": 1400,
                        "severity": "high",
                        "evidence_urls": ["https://example.test/flood"],
                },
            ]
        },
        max_zones=8,
    )

    assert [zone["label"] for zone in payload["zones"]] == [
        "Central Station robbery hotspot zone",
        "Flood-prone underpass",
    ]


def test_area_risk_payload_normalization_accepts_title_as_label():
    payload = service_module.normalize_safe_route_area_risk_payload(
        {
            "zones": [
                {
                    "title": "Nyanga",
                    "risk_score": 72,
                    "evidence_urls": ["https://example.test/nyanga"],
                }
            ]
        },
        max_zones=3,
    )

    assert payload["zones"][0]["label"] == "Nyanga"


def test_area_risk_payload_normalization_drops_sourceless_and_unsafe_url_zones():
    payload = service_module.normalize_safe_route_area_risk_payload(
        {
            "zones": [
                {"label": "Sourceless", "risk_score": 70},
                {
                    "label": "Unsafe URL",
                    "risk_score": 72,
                    "evidence_urls": ["http://169.254.169.254/latest"],
                },
                {
                    "label": "Source backed",
                    "risk_score": 74,
                    "evidence_urls": ["https://example.test/source-backed", "http://localhost/admin"],
                },
            ]
        },
        max_zones=5,
    )

    assert [zone["label"] for zone in payload["zones"]] == ["Source backed"]
    assert payload["zones"][0]["evidence_urls"] == ["https://example.test/source-backed"]


def test_area_risk_normalization_enforces_aoi_and_verified_sources():
    payload = service_module.normalize_safe_route_area_risk_payload(
        {
            "zones": [
                {
                    "label": "Inside AOI",
                    "lat": -33.9,
                    "lon": 18.6,
                    "radius_m": 1200,
                    "evidence_urls": ["https://example.test/verified"],
                },
                {
                    "label": "Outside AOI",
                    "lat": 51.5,
                    "lon": -0.1,
                    "radius_m": 1200,
                    "evidence_urls": ["https://example.test/verified"],
                },
                {
                    "label": "Unverified source",
                    "lat": -33.8,
                    "lon": 18.7,
                    "radius_m": 1200,
                    "evidence_urls": ["https://hallucinated.example/story"],
                },
                {
                    "label": "Oversized zone",
                    "lat": -33.8,
                    "lon": 18.7,
                    "radius_m": 100000,
                    "evidence_urls": ["https://example.test/verified"],
                },
            ]
        },
        max_zones=8,
        aoi={"bounds": {"minLat": -34.2, "minLon": 18.2, "maxLat": -33.5, "maxLon": 19.0}},
        verified_source_urls={"https://example.test/verified"},
    )

    assert [zone["label"] for zone in payload["zones"]] == ["Inside AOI"]


def test_area_risk_evidence_fallback_extracts_source_backed_localities():
    zones = service_module.fallback_safe_route_area_risk_candidates(
        aoi={
            "labelContext": {
                "place": "Cape Town",
                "country": "South Africa",
                "display": "Cape Town, Western Cape, South Africa",
            },
            "countryHints": ["South Africa"],
        },
        evidence=[
            {
                "title": "Crime monitoring highlights Nyanga and Delft robbery hotspots",
                "url": "https://example.test/nyanga-delft",
                "snippet": "Public reports mention Nyanga and Delft robbery and road disruption.",
            },
            {
                "title": "Khayelitsha protest disruption affects major routes",
                "url": "https://example.test/khayelitsha",
                "snippet": "Reports describe protest mobilisation and violence in Khayelitsha.",
            },
        ],
        max_zones=6,
    )

    labels = {zone["label"] for zone in zones}
    assert {"Nyanga", "Delft", "Khayelitsha"}.issubset(labels)
    assert "Cape Town" not in labels
    assert all(zone["evidence_urls"] for zone in zones)


def test_area_risk_evidence_fallback_ignores_unsafe_urls():
    zones = service_module.fallback_safe_route_area_risk_candidates(
        aoi={
            "labelContext": {
                "place": "Cape Town",
                "country": "South Africa",
                "display": "Cape Town, Western Cape, South Africa",
            }
        },
        evidence=[
            {
                "title": "Nyanga robbery hotspot",
                "url": "http://169.254.169.254/latest",
                "snippet": "Nyanga robbery and violence reports affect route safety.",
            },
            {
                "title": "Delft robbery hotspot",
                "url": "https://example.test/delft",
                "snippet": "Delft robbery and violence reports affect route safety.",
            },
        ],
        max_zones=6,
    )

    labels = {zone["label"] for zone in zones}
    assert "Nyanga" not in labels
    assert "Delft" in labels
    assert all(url.startswith("https://example.test/") for zone in zones for url in zone["evidence_urls"])


def test_area_risk_evidence_failure_uses_deterministic_fallback(monkeypatch):
    monkeypatch.setattr(service_module.settings, "area_risk_web_research_enabled", False)
    monkeypatch.setattr(service_module.settings, "area_risk_model", "gpt-5-mini")

    async def fail_analysis(*_args, **_kwargs):
        raise RuntimeError("Responses API returned no text")

    monkeypatch.setattr(service_module, "run_openai_responses_analysis", fail_analysis)

    result = asyncio.run(
        service_module.research_safe_route_area_risk(
            aoi={
                "labelContext": {
                    "place": "Cape Town",
                    "country": "South Africa",
                    "display": "Cape Town, Western Cape, South Africa",
                }
            },
            evidence=[
                {
                    "title": "Nyanga robbery hotspot",
                    "url": "https://example.test/nyanga",
                    "snippet": "Nyanga robbery and violence reports affect route safety.",
                }
            ],
            max_zones=3,
        )
    )

    assert result["model"] == "gpt-5-mini"
    assert result["zones"][0]["label"] == "Nyanga"
    assert result["notes"] == "Used bounded public evidence fallback for named locality candidates."


def test_area_risk_empty_web_result_does_not_double_call_model(monkeypatch):
    monkeypatch.setattr(service_module.settings, "area_risk_web_research_enabled", True)
    monkeypatch.setattr(service_module.settings, "area_risk_fallback_on_empty_web", False)
    monkeypatch.setattr(service_module.settings, "area_risk_model", "gpt-5.4-mini")

    async def fake_web_research(*_args, **_kwargs):
        return '{"zones":[]}'

    async def fail_analysis(*_args, **_kwargs):  # pragma: no cover - only runs on regression
        raise AssertionError("fallback analysis should not run for an empty successful web result")

    monkeypatch.setattr(service_module, "run_openai_web_research", fake_web_research)
    monkeypatch.setattr(service_module, "run_openai_responses_analysis", fail_analysis)

    result = asyncio.run(
        service_module.research_safe_route_area_risk(
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
    monkeypatch.setattr(service_module, "run_openai_responses_analysis", fallback_analysis)

    result = asyncio.run(
        service_module.research_safe_route_area_risk(
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


def test_area_risk_api_quota_failure_uses_chatgpt_codex_account(monkeypatch):
    monkeypatch.setattr(service_module.settings, "area_risk_web_research_enabled", False)
    monkeypatch.setattr(service_module.settings, "area_risk_model", "gpt-5.1")
    monkeypatch.setattr(service_module.settings, "area_risk_codex_model", "gpt-5.6-sol")

    async def fail_api_analysis(*_args, **_kwargs):
        raise RuntimeError("Responses API returned HTTP 429: insufficient_quota")

    captured = {}

    async def codex_account_analysis(prompt, *, max_zones):
        captured["prompt"] = prompt
        captured["max_zones"] = max_zones
        return {
            "model": "gpt-5.6-sol",
            "notes": "Bounded evidence analysis completed.",
            "zones": [
                {
                    "label": "Brixton",
                    "severity": "high",
                    "risk_score": 78,
                    "confidence": "source-backed",
                    "lat": 51.4627,
                    "lon": -0.1145,
                    "radius_m": 1200,
                    "coordinates": [],
                    "display_color": "red",
                    "icon": "warning",
                    "notes": "Recurring robbery reports affect public route safety.",
                    "evidence_urls": ["https://example.test/london-risk"],
                }
            ],
        }

    monkeypatch.setattr(service_module, "run_openai_responses_analysis", fail_api_analysis)
    monkeypatch.setattr(service_module, "run_area_risk_codex_analysis", codex_account_analysis)

    result = asyncio.run(
        service_module.research_safe_route_area_risk(
            aoi={
                "bounds": {
                    "minLat": 51.40,
                    "minLon": -0.30,
                    "maxLat": 51.60,
                    "maxLon": -0.05,
                },
                "labelContext": {
                    "place": "London",
                    "country": "United Kingdom",
                    "display": "Greater London, United Kingdom",
                },
            },
            evidence=[
                {
                    "title": "Police publish public-safety update",
                    "url": "https://example.test/london-risk",
                    "snippet": "Reports describe recurring robbery affecting route safety.",
                }
            ],
            max_zones=3,
        )
    )

    assert result["model"] == "codex-account:gpt-5.6-sol"
    assert [zone["label"] for zone in result["zones"]] == ["Brixton"]
    assert result["zones"][0]["evidence_urls"] == ["https://example.test/london-risk"]
    assert result["notes"] == "Bounded evidence analysis completed."
    assert captured["max_zones"] == 3
    assert "Analyze only the supplied public evidence" in captured["prompt"]
    assert "Web search, graph tools, shell commands, and file access are unavailable" in captured["prompt"]


def test_area_risk_api_and_codex_failure_is_retryable_http_failure(monkeypatch):
    monkeypatch.setattr(service_module.settings, "area_risk_web_research_enabled", False)

    async def fail_api_analysis(*_args, **_kwargs):
        raise RuntimeError("Responses API returned HTTP 429: provider-secret-token")

    async def fail_codex_analysis(*_args, **_kwargs):
        raise RuntimeError("codex account usage limit with private detail")

    monkeypatch.setattr(service_module, "run_openai_responses_analysis", fail_api_analysis)
    monkeypatch.setattr(service_module, "run_area_risk_codex_analysis", fail_codex_analysis)

    with pytest.raises(RuntimeError, match="Area-risk analysis providers are unavailable") as exc_info:
        asyncio.run(
            service_module.research_safe_route_area_risk(
                aoi={
                    "bounds": {
                        "minLat": 51.40,
                        "minLon": -0.30,
                        "maxLat": 51.60,
                        "maxLon": -0.05,
                    }
                },
                evidence=[],
                max_zones=3,
            )
        )

    assert "provider-secret-token" not in str(exc_info.value)
    assert "private detail" not in str(exc_info.value)


def test_area_risk_successful_empty_api_result_does_not_spend_codex_account(monkeypatch):
    monkeypatch.setattr(service_module.settings, "area_risk_web_research_enabled", False)

    async def empty_api_analysis(*_args, **_kwargs):
        return '{"zones":[],"notes":"No supported named localities."}'

    async def fail_if_codex_called(*_args, **_kwargs):  # pragma: no cover - regression only
        raise AssertionError("Codex account fallback must only run after an API failure")

    monkeypatch.setattr(service_module, "run_openai_responses_analysis", empty_api_analysis)
    monkeypatch.setattr(service_module, "run_area_risk_codex_analysis", fail_if_codex_called)

    result = asyncio.run(
        service_module.research_safe_route_area_risk(
            aoi={
                "bounds": {
                    "minLat": 51.40,
                    "minLon": -0.30,
                    "maxLat": 51.60,
                    "maxLon": -0.05,
                }
            },
            evidence=[],
            max_zones=3,
        )
    )

    assert result == {
        "zones": [],
        "model": service_module.settings.area_risk_model,
        "notes": "No supported named localities.",
    }
