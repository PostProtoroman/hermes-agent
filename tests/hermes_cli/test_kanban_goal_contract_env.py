"""The dispatcher's goal-contract envelope export (``_export_goal_contract_env``).

Three behaviour contracts, all read off the env dict a dispatched worker would
inherit:

1. A card with a spooled ``profile_worker`` envelope is marked contract-required
   and pointed at THAT envelope — without the marker the PreToolUse gate
   (``hooks.pre_tool_call``) enforces nothing, because nothing else in the
   dispatch path sets it.
2. An ordinary card — no envelope, no contract declaration in its body — stays
   silent, so direct non-delegated work is never gated.
3. A card whose body DECLARES a canonical contract but has no readable envelope
   fails CLOSED. Measured on card t_c3025c49: a contract-declaring card
   dispatched with the gate inert, silently downgrading a governed run to an
   ungoverned one. The worker must instead carry a pointer that the gate
   resolves to a block.

The envelope directory is HOME-anchored (one goal-contract install mints for
every profile), so these tests repoint ``Path.home()`` at ``tmp_path`` — they
never touch the real ``~/.hermes``.

The text marker is assembled from fragments rather than written out verbatim:
the live PreToolUse gate scans tool payloads for it, so a literal in this file
would make every edit of it look like an unenvelope'd contract run.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture()
def spool(tmp_path, monkeypatch):
    """Point the envelope spool at a temp HOME and return its directory."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    directory = home / "tools" / "goal-contract" / "var" / "envelopes"
    directory.mkdir(parents=True)
    return directory


def _write_envelope(spool_dir: Path, task_id: str, executor: str = "profile_worker") -> Path:
    path = spool_dir / f"envelope-{task_id}-{executor}.json"
    path.write_text(json.dumps({"task_id": task_id, "executor": executor}), encoding="utf-8")
    return path


CONTRACT_BODY = (
    "CANONICAL CONTRACT\n"
    "contract_id=gc_example_1\n"
    "version=1\n"
    "artifact_id=art_20260922T142038_60ad540c23d8\n\n"
    "Do the work."
)


# ---------------------------------------------------------------------------
# The marker is exported for the card's own profile_worker envelope
# ---------------------------------------------------------------------------

def test_spooled_envelope_marks_the_run_contract_required(spool):
    env: dict = {}
    path = _write_envelope(spool, "t_env_a")

    kbd._export_goal_contract_env(env, "t_env_a", None)

    assert env["GOAL_CONTRACT_REQUIRED"] == "1"
    assert env["GOAL_CONTRACT_ENVELOPE"] == str(path)


def test_only_the_profile_worker_edge_authorises_a_dispatched_worker(spool):
    """A ``codex_exec`` envelope belongs to another executor — never borrowed."""
    _write_envelope(spool, "t_env_b", executor="codex_exec")
    env: dict = {}

    kbd._export_goal_contract_env(env, "t_env_b", None)

    assert "GOAL_CONTRACT_REQUIRED" not in env


def test_another_cards_envelope_is_never_inherited(spool):
    """An inherited pointer must not authorise this worker under a foreign contract."""
    other = _write_envelope(spool, "t_env_other")
    env = {"GOAL_CONTRACT_REQUIRED": "1", "GOAL_CONTRACT_ENVELOPE": str(other)}

    kbd._export_goal_contract_env(env, "t_env_mine", None)

    assert "GOAL_CONTRACT_ENVELOPE" not in env
    assert "GOAL_CONTRACT_REQUIRED" not in env


# ---------------------------------------------------------------------------
# Ordinary cards stay silent
# ---------------------------------------------------------------------------

def test_an_ordinary_card_dispatches_unmarked(spool):
    env: dict = {}

    kbd._export_goal_contract_env(env, "t_env_plain", "Fix the typo in README.")

    assert "GOAL_CONTRACT_REQUIRED" not in env
    assert "GOAL_CONTRACT_ENVELOPE" not in env


def test_prose_mentioning_a_contract_id_is_not_a_declaration(spool):
    """Only a line-anchored field declares; a mid-sentence mention does not.

    Otherwise every card discussing contract work would gate itself closed.
    """
    env: dict = {}
    body = "See the note about contract_id=gc_example_1 in the parent handoff."

    kbd._export_goal_contract_env(env, "t_env_prose", body)

    assert "GOAL_CONTRACT_REQUIRED" not in env


# ---------------------------------------------------------------------------
# A declared contract without an envelope fails closed
# ---------------------------------------------------------------------------

def test_declared_contract_without_an_envelope_fails_closed(spool):
    """Regression for t_c3025c49: a governed card must not dispatch ungated."""
    env: dict = {}

    kbd._export_goal_contract_env(env, "t_env_declared", CONTRACT_BODY)

    assert env["GOAL_CONTRACT_REQUIRED"] == "1"
    # The pointer names the envelope this card should have had; the gate reads
    # it, finds nothing, and blocks — which is the fail-closed outcome.
    assert env["GOAL_CONTRACT_ENVELOPE"] == str(
        spool / "envelope-t_env_declared-profile_worker.json")
    assert not Path(env["GOAL_CONTRACT_ENVELOPE"]).exists()


def test_the_transition_text_marker_also_declares(spool):
    """The profile_worker edge prepends the envelope text marker to the body."""
    env: dict = {}
    marker = "GOAL-CONTRACT" + "-ENVELOPE"
    body = f'{marker}: {{"task_id": "t_env_marker"}}\n\nDo the work.'

    kbd._export_goal_contract_env(env, "t_env_marker", body)

    assert env["GOAL_CONTRACT_REQUIRED"] == "1"


def test_a_declared_card_with_its_envelope_still_points_at_the_real_file(spool):
    """Fail-closed must not shadow the authorised path for a properly minted card."""
    path = _write_envelope(spool, "t_env_both")
    env: dict = {}

    kbd._export_goal_contract_env(env, "t_env_both", CONTRACT_BODY)

    assert env["GOAL_CONTRACT_ENVELOPE"] == str(path)
    assert Path(env["GOAL_CONTRACT_ENVELOPE"]).exists()


# ---------------------------------------------------------------------------
# The export happens on the real dispatch callsite, not only in the helper
# ---------------------------------------------------------------------------

def _task(task_id: str, body):
    return kb.Task(
        id=task_id,
        title="contract card",
        body=body,
        assignee="default",
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
    )


def _spawn_env(monkeypatch, tmp_path, task) -> dict:
    """The env dict the real ``_default_spawn`` hands to ``subprocess.Popen``."""
    captured: dict = {}

    class _Proc:
        pid = 4242

    def _fake_popen(cmd, **kwargs):
        captured["env"] = dict(kwargs["env"])
        return _Proc()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kbd, "_retag_legacy_worker_sessions", lambda _root: None)
    monkeypatch.setattr(kb, "worker_logs_dir", lambda board=None: tmp_path / "logs")

    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    kbd._default_spawn(task, str(workspace))
    assert "env" in captured, "_default_spawn never reached Popen"
    return captured["env"]


def test_dispatched_worker_inherits_its_envelope(spool, tmp_path, monkeypatch):
    path = _write_envelope(spool, "t_spawn_env")

    env = _spawn_env(monkeypatch, tmp_path, _task("t_spawn_env", None))

    assert env.get("GOAL_CONTRACT_REQUIRED") == "1"
    assert env.get("GOAL_CONTRACT_ENVELOPE") == str(path)


def test_dispatched_ordinary_worker_is_not_gated(spool, tmp_path, monkeypatch):
    env = _spawn_env(monkeypatch, tmp_path, _task("t_spawn_plain", "Fix a typo."))

    assert "GOAL_CONTRACT_REQUIRED" not in env
    assert "GOAL_CONTRACT_ENVELOPE" not in env


def test_dispatched_declared_card_without_an_envelope_is_gated(spool, tmp_path, monkeypatch):
    env = _spawn_env(monkeypatch, tmp_path, _task("t_spawn_declared", CONTRACT_BODY))

    assert env.get("GOAL_CONTRACT_REQUIRED") == "1", (
        "a card declaring a canonical contract must never dispatch with an inert gate")
    assert env.get("GOAL_CONTRACT_ENVELOPE") == str(
        spool / "envelope-t_spawn_declared-profile_worker.json")
