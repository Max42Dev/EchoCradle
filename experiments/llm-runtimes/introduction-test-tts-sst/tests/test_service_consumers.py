"""Application ServiceClient contracts, with no models or device I/O."""

from __future__ import annotations

import importlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

import compare_interviews
import main as app
import service_smoke
from config_schema import CONFIG_SCHEMA
from consumer_results import json_value
from interview import Interview
from orchestrator.client import ServiceClientError
from orchestrator.client_types import TextResult, ToolOutcome
from orchestrator.errors import OrchestratorError
from orchestrator.tools import JsonConfigTool, ToolRegistry


class ResultClient:
    def __init__(self, *, state="cancelled", error=False) -> None:
        self.state = state
        self.error = error
        self.requests = 0
        self.result = None

    def chat_with_tools(self, messages, registry, *, max_tokens=512, temperature=0.7):
        return self.stream_chat_with_tools(messages, registry, max_tokens=max_tokens,
                                           temperature=temperature)

    def stream_chat_with_tools(
        self, messages, registry, *, max_tokens=512, temperature=0.7,
        on_text=None, cancellation=None,
    ):
        self.requests += 1
        assert cancellation is None or isinstance(cancellation, threading.Event)
        arguments = {"field": "ai_name", "value": "Vex"}
        value = registry.invoke("config_set", arguments)
        call = ToolOutcome("model-id", str(uuid4()), "config_set", arguments, value,
                           "unknown", False, True)
        text = "Hello traveller. An unfinished continuation"
        self.result = TextResult(self.state, text, str(uuid4()), str(uuid4()), (call,))
        if on_text:
            on_text(text)
        if self.error:
            raise ServiceClientError("TRANSPORT_ERROR", "Socket lost",
                                     partial_result=self.result)
        return self.result


def make_interview(client):
    tool = JsonConfigTool(CONFIG_SCHEMA, data={"username": "Ada", "style": "forest"})
    registry = ToolRegistry()
    tool.register_into(registry)
    return Interview(client, tool, registry), tool


@pytest.mark.parametrize("error", [False, True])
def test_remote_cancel_preserves_side_effects_but_never_flushes_fragment(tmp_path, error):
    client = ResultClient(error=error)
    interview, tool = make_interview(client)
    spoken = []
    turn, interrupted = interview.respond_streaming("Call yourself Vex.", spoken.append)
    assert interrupted  # server cancellation is authoritative even without a set Event
    assert spoken == ["Hello traveller."]
    assert turn.say == interview.history[-1]["content"] == "Hello traveller."
    assert turn.raw == client.result.text
    assert turn.done and tool.data["ai_name"] == "Vex"
    assert turn.tool_calls == list(client.result.calls)
    assert turn.tool_calls[0].executed_locally and not turn.tool_calls[0].result_delivered
    # Unknown delivery does not roll back a locally executed config action.
    tool.save(tmp_path / "config.json")
    assert json.loads((tmp_path / "config.json").read_text())["ai_name"] == "Vex"


def test_cancelled_empty_nonstreaming_result_has_no_retry_or_fallback():
    class Cancelled:
        def chat_with_tools(self, messages, registry, **kwargs):
            return TextResult("cancelled", "", str(uuid4()), None)

    interview, _ = make_interview(Cancelled())
    turn = interview.opening()
    assert not turn.say and not turn.repaired


def test_partial_failure_does_not_repeat_unknown_tool_side_effects():
    client = ResultClient(state="failed", error=True)
    interview, tool = make_interview(client)
    turn = interview.opening()
    assert client.requests == 1
    assert turn.tool_calls == list(client.result.calls)
    assert tool.data["ai_name"] == "Vex"


def test_frozen_snapshots_are_reportable_without_deepcopy(capsys, tmp_path):
    call = ToolOutcome("native", str(uuid4()), "config_set",
                       {"nested": {"values": [1, {"name": "Ada"}]}},
                       {"ok": True}, "completed", True, True)
    result = TextResult("succeeded", "Hi.", str(uuid4()), str(uuid4()), (call,))
    data = json_value(result)
    assert data["calls"][0]["arguments"]["nested"]["values"][1]["name"] == "Ada"
    data["calls"][0]["arguments"]["nested"]["values"].append(2)
    assert len(call.arguments["nested"]["values"]) == 2
    compare_interviews.save(tmp_path / "comparison.json", result)
    service_smoke.write_json(tmp_path / "smoke.json", result)
    assert json.loads((tmp_path / "smoke.json").read_text())["calls"][0]["model_call_id"] \
        == "native"
    app._report_turn(SimpleNamespace(tool_calls=[call], validation_errors=[]))
    assert "delivered=True" in capsys.readouterr().out


@pytest.mark.parametrize("attached", [False, True])
def test_entrypoint_selects_explicit_ownership(monkeypatch, tmp_path, attached):
    selected = []
    client = SimpleNamespace(stop=lambda: selected.append("stop"))
    monkeypatch.setattr(app.ServiceClient, "attach",
                        lambda url: selected.append(("attach", url)) or client)
    monkeypatch.setattr(app.ServiceClient, "start_owned",
                        lambda **kw: selected.append(("owned", kw)) or client)
    monkeypatch.setattr(app, "_run", lambda *args: 0)
    arguments = ["--no-tts", "--out", str(tmp_path / "config.json")]
    if attached:
        arguments += ["--service-url", "http://127.0.0.1:5010"]
    assert app.run(app.build_parser().parse_args(arguments)) == 0
    assert selected[0][0] == ("attach" if attached else "owned")
    assert selected[-1] == "stop"
    if not attached:
        assert selected[0][1]["profile"] == "text"


def test_attach_refuses_to_silently_ignore_model_policy(tmp_path):
    args = app.build_parser().parse_args([
        "--service-url", "http://127.0.0.1:5010", "--text-model", "example",
        "--out", str(tmp_path / "config.json"),
    ])
    with pytest.raises(OrchestratorError, match="owned service"):
        app.run(args)


def test_comparison_records_immutable_tool_outcomes(monkeypatch, tmp_path):
    client = ResultClient(state="succeeded")
    scenario = {"name": "Ada", "ai_name": "Vex", "style": "forest",
                "keywords": ["forest"], "story": None, "story_keywords": []}
    monkeypatch.setattr(compare_interviews, "player_reply", lambda *args: "Ada, forest, Vex.")
    report = compare_interviews.run_case(client, object(), scenario, 0, 2,
                                         tmp_path / "case.json")
    assert report["error"] is None
    calls = json.loads((tmp_path / "case.json").read_text())["turns"][0]["tool_calls"]
    assert calls[0]["model_call_id"] == "model-id" and calls[0]["executed_locally"]


def test_smoke_cancellation_uses_returned_identity_and_textresult(monkeypatch):
    identifier = str(uuid4())

    class SmokeClient:
        _lock = threading.Lock()
        _jobs = set()

        def _submit(self, spec):
            return {"job_id": identifier}

        def _cancel(self, job_id):
            return {"state": "cancelled"}

        def stream_chat_with_tools(self, messages, registry, *, on_text, cancellation, **kw):
            on_text("Partial")
            assert isinstance(cancellation, threading.Event) and cancellation.is_set()
            return TextResult("cancelled", "Partial", identifier, str(uuid4()))

        def chat(self, messages, **kw):
            return TextResult("succeeded", "Recovered", str(uuid4()), None)

    snapshots = iter([{"state": "running", "host_pending": True},
                      {"state": "cancelled", "host_pending": False},
                      {"state": "cancelled", "host_pending": False}])
    monkeypatch.setattr(service_smoke, "trace_job", lambda *args: next(snapshots))
    report = {}
    service_smoke.cancellation_smoke(SmokeClient(), report)
    assert report["cancellation"]["returned_text"] == "Partial"
    assert report["cancellation"]["subsequent_text"] == "Recovered"
    assert report["cancellation"]["job_id"] == identifier


@pytest.mark.parametrize("name", [
    "interrupt_calibration", "mic_compare", "review_capture", "speech_smoke", "vad_replay",
])
def test_diagnostics_import_without_legacy_main_exports(name):
    assert importlib.import_module(name)


def test_explicit_local_diagnostic_boundary_keeps_native_predicate_api():
    from local_interview import LocalInterviewText
    from orchestrator.tools import ToolCall

    cancellation = threading.Event()
    received = []

    def stream(messages, registry, *, should_stop, on_text, **options):
        received.append(should_stop)
        on_text("Hi.")
        cancellation.set()
        return "Hi.", [ToolCall("native", "config_set", {"field": "ai_name"}, {"ok": True})]

    native = SimpleNamespace(stream_chat_with_tools=stream)
    deltas = []
    result = LocalInterviewText(native).stream_chat_with_tools(
        [], ToolRegistry(), on_text=deltas.append, cancellation=cancellation,
    )
    assert received[0]() and result.state == "cancelled"
    assert deltas == ["Hi."] and result.text == "Hi."
    assert result.calls[0].model_call_id == "native"
    assert result.calls[0].outcome == "unknown" and not result.calls[0].result_delivered