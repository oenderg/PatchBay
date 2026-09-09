import asyncio
import json
from pathlib import Path
import time

import pytest

from patchbay.desktop_task_tool_surface import (
    DESKTOP_TASK_START_TOOL_NAME,
    DESKTOP_TASK_STATUS_TOOL_NAME,
)
from patchbay.desktop_tasks import (
    DesktopTaskClient,
    DesktopTaskError,
    DesktopTaskOptions,
    DesktopTaskTarget,
    MAX_REPORT_LENGTH,
    build_desktop_resume_command,
    desktop_tasks_enabled,
    load_desktop_targets,
)
from patchbay.jobs.manager import JobManager, JobState
from patchbay.protocol.mcp import (
    PUBLIC_TOOL_DESCRIPTORS,
    PUBLIC_TOOL_NAMES,
    tool_descriptors_for_mode,
    tool_is_available,
    validate_public_tool_arguments,
)


PRIVATE_THREAD_ID = "00000000-0000-7000-8000-000000000001"


def make_target(**overrides):
    values = {
        "alias": "MTP Luna",
        "thread_id": PRIVATE_THREAD_ID,
        "cwd": "/private/tmp/visible-task",
        "model": "gpt-5.6-luna",
        "reasoning_effort": "medium",
        "sandbox": "workspace-write",
        "profile": "desktop-test",
        "skip_git_repo_check": True,
    }
    values.update(overrides)
    return DesktopTaskTarget(**values)


def make_config(tmp_path: Path, *, enabled: bool = True, retention_hours: int = 24):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return {
        "server": {
            "max_concurrent_jobs": 4,
            "queue_enabled": True,
            "job_timeout_seconds": 0,
            "job_cleanup_after_hours": 24,
        },
        "repositories": {"default": str(repo), "allowed": [str(repo)]},
        "security": {
            "require_git_repo": False,
            "default_sandbox": "read-only",
            "allowed_env_keys": ["PATH"],
            "allowed_config_override_prefixes": [],
            "blocked_globs": [".env", ".git", ".git/**", "**/.git/**"],
        },
        "power_tools": {"direct_write": False, "bash_mode": "off", "codex_session_read": False},
        "logging": {
            "job_logs_dir": str(tmp_path / "logs" / "jobs"),
            "job_state_dir": str(tmp_path / "logs" / "jobs" / "state"),
        },
        "desktop_tasks": {
            "enabled": enabled,
            "targets_file": str(tmp_path / "targets.json"),
            "codex_bin": "codex",
            "timeout_ms": 120_000,
            "retention_hours": retention_hours,
        },
    }


def write_targets(path: Path, *, thread_id: str = PRIVATE_THREAD_ID, aliases=None):
    aliases = aliases or {"MTP Luna": {"thread_id": thread_id}}
    path.write_text(json.dumps({"targets": aliases}), encoding="utf-8")
    path.chmod(0o600)


class RecordingExecutor:
    def __init__(self):
        self.scheduled = []

    def schedule_job(self, job_id):
        self.scheduled.append(job_id)


def make_client(tmp_path: Path, *, retention_hours: int = 24):
    config = make_config(tmp_path, retention_hours=retention_hours)
    manager = JobManager(config)
    executor = RecordingExecutor()
    client = DesktopTaskClient(
        config,
        manager,
        executor,
        {"MTP Luna": make_target()},
        timeout_ms=120_000,
        retention_hours=retention_hours,
    )
    return config, manager, executor, client


def test_public_surface_is_desktop_named_and_opt_in(tmp_path: Path):
    disabled = {"app": {"tool_mode": "worker"}}
    targets_path = tmp_path / "targets.json"
    write_targets(targets_path)
    enabled = {
        "app": {"tool_mode": "worker"},
        "desktop_tasks": {"enabled": True, "targets_file": str(targets_path)},
    }

    assert desktop_tasks_enabled(disabled) is False
    assert desktop_tasks_enabled(enabled) is True
    assert tool_is_available(disabled, DESKTOP_TASK_START_TOOL_NAME) is False
    assert tool_is_available(enabled, DESKTOP_TASK_START_TOOL_NAME) is True
    assert tool_is_available(enabled, DESKTOP_TASK_STATUS_TOOL_NAME) is True
    names = {tool["name"] for tool in tool_descriptors_for_mode(enabled)}
    assert {DESKTOP_TASK_START_TOOL_NAME, DESKTOP_TASK_STATUS_TOOL_NAME} <= names
    assert "codex_visible_worker_message" not in PUBLIC_TOOL_NAMES

    by_name = {tool["name"]: tool for tool in PUBLIC_TOOL_DESCRIPTORS}
    assert by_name[DESKTOP_TASK_START_TOOL_NAME]["readOnlyHint"] is False
    assert by_name[DESKTOP_TASK_START_TOOL_NAME]["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": False,
        "openWorldHint": True,
        "idempotentHint": False,
    }
    assert by_name[DESKTOP_TASK_STATUS_TOOL_NAME]["annotations"] == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": False,
        "idempotentHint": True,
    }
    assert "thread_id" not in by_name[DESKTOP_TASK_START_TOOL_NAME]["inputSchema"]["properties"]
    validate_public_tool_arguments(
        DESKTOP_TASK_START_TOOL_NAME,
        {"target": "MTP Luna", "receipt_id": "web-1", "prompt": "Continue."},
    )
    with pytest.raises(ValueError, match="Unknown argument 'thread_id'"):
        validate_public_tool_arguments(
            DESKTOP_TASK_START_TOOL_NAME,
            {
                "target": "MTP Luna",
                "receipt_id": "web-1",
                "prompt": "Continue.",
                "thread_id": PRIVATE_THREAD_ID,
            },
        )


def test_desktop_targets_reject_duplicate_thread_ids(tmp_path: Path):
    path = tmp_path / "targets.json"
    write_targets(
        path,
        aliases={
            "MTP Luna": {"thread_id": PRIVATE_THREAD_ID},
            "MTP Luna Copy": {"thread_id": PRIVATE_THREAD_ID},
        },
    )
    with pytest.raises(DesktopTaskError, match="duplicate thread id"):
        load_desktop_targets(path)


def test_desktop_target_output_format_defaults_and_is_strict(tmp_path: Path):
    path = tmp_path / "targets.json"
    write_targets(path)
    assert load_desktop_targets(path)["MTP Luna"].output_format == "structured"
    path.write_text(
        json.dumps({"targets": {"MTP Luna": {"thread_id": PRIVATE_THREAD_ID, "output_format": "markdown"}}}),
        encoding="utf-8",
    )
    path.chmod(0o600)
    assert load_desktop_targets(path)["MTP Luna"].output_format == "markdown"
    path.write_text(
        json.dumps({"targets": {"MTP Luna": {"thread_id": PRIVATE_THREAD_ID, "output_format": "html"}}}),
        encoding="utf-8",
    )
    path.chmod(0o600)
    with pytest.raises(DesktopTaskError, match="output_format is unsupported"):
        load_desktop_targets(path)


def test_desktop_catalog_requires_a_valid_private_targets_file(tmp_path: Path):
    base = {"desktop_tasks": {"enabled": True}}
    assert desktop_tasks_enabled(base) is False

    relative = {"desktop_tasks": {"enabled": True, "targets_file": "targets.json"}}
    assert desktop_tasks_enabled(relative) is False

    directory = tmp_path / "targets-dir"
    directory.mkdir()
    assert desktop_tasks_enabled(
        {"desktop_tasks": {"enabled": True, "targets_file": str(directory)}}
    ) is False

    invalid = tmp_path / "invalid.json"
    invalid.write_text("not json", encoding="utf-8")
    invalid.chmod(0o600)
    assert desktop_tasks_enabled(
        {"desktop_tasks": {"enabled": True, "targets_file": str(invalid)}}
    ) is False

    public = tmp_path / "public.json"
    write_targets(public)
    public.chmod(0o644)
    assert desktop_tasks_enabled(
        {"desktop_tasks": {"enabled": True, "targets_file": str(public)}}
    ) is False

    valid = tmp_path / "valid.json"
    write_targets(valid)
    assert desktop_tasks_enabled(
        {"desktop_tasks": {"enabled": True, "targets_file": str(valid)}}
    ) is True


def test_private_allowlist_and_command_preserve_options(tmp_path: Path):
    path = tmp_path / "targets.json"
    path.write_text(
        json.dumps(
            {
                "targets": {
                    "MTP Luna": {
                        "thread_id": PRIVATE_THREAD_ID,
                        "cwd": "/private/tmp/visible-task",
                        "model": "gpt-5.6-luna",
                        "reasoning_effort": "medium",
                        "sandbox": "workspace-write",
                        "profile": "desktop-test",
                        "skip_git_repo_check": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    targets = load_desktop_targets(path)
    target = targets["MTP Luna"]
    command = build_desktop_resume_command(
        "codex",
        target,
        DesktopTaskOptions(
            cwd=target.cwd,
            model=target.model,
            reasoning_effort=target.reasoning_effort,
            sandbox=target.sandbox,
            profile=target.profile,
            skip_git_repo_check=target.skip_git_repo_check,
        ),
    )
    assert command[:2] == ["codex", "exec"]
    assert command[-3:] == ["resume", PRIVATE_THREAD_ID, "-"]
    assert "--skip-git-repo-check" in command
    assert command[command.index("--model") + 1] == "gpt-5.6-luna"
    assert 'model_reasoning_effort="medium"' in command
    assert "prompt" not in command

    path.write_text(
        json.dumps({"targets": {"MTP Luna": {"thread_id": PRIVATE_THREAD_ID, "skip_git_repo_check": "true"}}}),
        encoding="utf-8",
    )
    with pytest.raises(DesktopTaskError, match="skip_git_repo_check must be a boolean"):
        load_desktop_targets(path)

    path.chmod(0o644)
    with pytest.raises(DesktopTaskError, match="must be private"):
        load_desktop_targets(path)

    path.chmod(0o600)
    with pytest.raises(DesktopTaskError, match="targets_file must be an absolute path"):
        DesktopTaskClient.from_config(
            {**make_config(tmp_path), "desktop_tasks": {"enabled": True, "targets_file": "targets.json"}},
            JobManager(make_config(tmp_path)),
            RecordingExecutor(),
        )


@pytest.mark.asyncio
async def test_start_returns_queued_quickly_and_enforces_receipt_and_target_idempotency(tmp_path):
    _config, manager, executor, client = make_client(tmp_path)
    started = await client.start(target="MTP Luna", receipt_id="trial-1", prompt="Continue.")
    assert started["state"] == "queued"
    assert started["ok"] is False
    assert len(executor.scheduled) == 1

    again = await client.start(target="MTP Luna", receipt_id="trial-1", prompt="Continue.")
    assert again == started
    assert len(executor.scheduled) == 2  # schedule_job is itself idempotent in the real executor

    with pytest.raises(DesktopTaskError, match="already used"):
        await client.start(target="MTP Luna", receipt_id="trial-1", prompt="Different.")

    with pytest.raises(DesktopTaskError, match="active turn"):
        await client.start(target="MTP Luna", receipt_id="trial-2", prompt="Another.")

    with pytest.raises(DesktopTaskError, match="logical alias"):
        await client.start(target=PRIVATE_THREAD_ID, receipt_id="trial-3", prompt="Raw id.")

    job = manager.get_job(executor.scheduled[0])
    assert job is not None
    assert PRIVATE_THREAD_ID in json.dumps(job.options)


@pytest.mark.asyncio
async def test_start_reconciles_one_proven_stale_terminal_turn_before_new_receipt(
    tmp_path,
):
    _config, manager, _unused_executor, _unused_client = make_client(tmp_path)

    class RepairingExecutor(RecordingExecutor):
        def __init__(self):
            super().__init__()
            self.reconciled = []

        def reconcile_stale_terminal_cleanup(self, job_id):
            self.reconciled.append(job_id)
            manager.update_job_state(
                job_id,
                JobState.COMPLETED,
                result={"summary": "stale supervisor reconciled"},
                wrapper_cleanup_outcome="stale_supervisor_reconciled",
            )
            return True

    executor = RepairingExecutor()
    client = DesktopTaskClient(
        make_config(tmp_path),
        manager,
        executor,
        {"MTP Luna": make_target()},
        timeout_ms=120_000,
        retention_hours=24,
    )

    first = await client.start(
        target="MTP Luna", receipt_id="stale-1", prompt="First turn."
    )
    old_job = manager.get_job(executor.scheduled[0])
    assert old_job is not None
    manager.update_job_state(
        old_job.job_id,
        JobState.COMPLETED,
        result={"summary": "completed with stale cleanup"},
        wrapper_cleanup_outcome="cleanup_blocked_untrusted_process_identity",
    )

    second = await client.start(
        target="MTP Luna", receipt_id="stale-2", prompt="Retry after recovery."
    )

    assert first["state"] == "queued"
    assert second["state"] == "queued"
    assert executor.reconciled == [old_job.job_id]
    assert len(executor.scheduled) == 2


@pytest.mark.asyncio
async def test_status_survives_restart_and_keeps_valid_answer_after_nonzero_wrapper(tmp_path):
    config, manager, executor, client = make_client(tmp_path)
    started = await client.start(
        target="MTP Luna",
        receipt_id="unarchive-1",
        prompt="Return the success marker.",
    )
    job = manager.get_job(executor.scheduled[0])
    assert job is not None
    manager.update_job_state(
        job.job_id,
        JobState.COMPLETED,
        result={"summary": "UNARCHIVED_VISIBLE_TASK_SUCCEEDED"},
        exit_code=1,
        terminal_source="stdout_turn_completed",
    )

    status = await client.status(target="MTP Luna", receipt_id="unarchive-1")
    assert status["ok"] is True
    assert status["state"] == "completed"
    assert status["answer"] == "UNARCHIVED_VISIBLE_TASK_SUCCEEDED"
    assert status["warning_code"] == "wrapper_exit_after_answer"
    assert PRIVATE_THREAD_ID not in json.dumps(status)

    reloaded = JobManager(config)
    restarted_client = DesktopTaskClient(
        config,
        reloaded,
        RecordingExecutor(),
        {"MTP Luna": make_target()},
        retention_hours=24,
    )
    recovered = await restarted_client.status(target="MTP Luna", receipt_id="unarchive-1")
    assert recovered["state"] == "completed"
    assert recovered["answer"] == "UNARCHIVED_VISIBLE_TASK_SUCCEEDED"
    assert started["receipt_id"] == recovered["receipt_id"]


@pytest.mark.asyncio
async def test_public_answer_uses_structured_fields_and_redacts_paths_and_session_ids(tmp_path):
    _config, manager, executor, client = make_client(tmp_path)
    await client.start(target="MTP Luna", receipt_id="redact-1", prompt="Return a report.")
    job = manager.get_job(executor.scheduled[0])
    assert job is not None
    manager.update_job_state(
        job.job_id,
        JobState.COMPLETED,
        result={
            "answer": (
                "Report from /Users/example/MTP at "
                "00000000-0000-7000-8000-000000000099."
            ),
            "raw_output": "RAW_PRIVATE_CLI_OUTPUT /private/secret/not-public",
        },
    )

    status = await client.status(target="MTP Luna", receipt_id="redact-1")
    assert status["answer"] == "Report from [REDACTED_PATH] at [private-session]."
    assert "RAW_PRIVATE_CLI_OUTPUT" not in json.dumps(status)
    assert "/Users/" not in json.dumps(status)
    assert "/private/" not in json.dumps(status)
    assert "00000000-0000-7000-8000-000000000099" not in json.dumps(status)

    await client.start(target="MTP Luna", receipt_id="redact-raw-only", prompt="Return no structured answer.")
    raw_job = manager.get_job(executor.scheduled[-1])
    assert raw_job is not None
    manager.update_job_state(
        raw_job.job_id,
        JobState.COMPLETED,
        result={"raw_output": "RAW_ONLY_MUST_NOT_ESCAPE"},
    )
    raw_status = await client.status(target="MTP Luna", receipt_id="redact-raw-only")
    assert "answer" not in raw_status
    assert "RAW_ONLY_MUST_NOT_ESCAPE" not in json.dumps(raw_status)


@pytest.mark.asyncio
async def test_markdown_report_is_sanitized_without_jsonl_or_private_values(tmp_path):
    _config, manager, executor, client = make_client(tmp_path)
    client.targets["MTP Luna"] = make_target(output_format="markdown")
    await client.start(target="MTP Luna", receipt_id="markdown-1", prompt="Return Markdown.")
    job = manager.get_job(executor.scheduled[0])
    assert job is not None
    manager.update_job_state(
        job.job_id,
        JobState.COMPLETED,
        result={
            "summary": (
                "# Result\n\n- changed `src/file.py`\n- workspace: "
                "/private/tmp/visible-task/src/file.py\n\n"
                "External /Users/example/private.txt token=fixture-value "
                "00000000-0000-7000-8000-000000000099"
            )
        },
    )

    status = await client.status(target="MTP Luna", receipt_id="markdown-1")

    assert status["report_format"] == "markdown"
    assert status["report"] == (
        "# Result\n\n- changed `src/file.py`\n- workspace: src/file.py\n\n"
        "External [REDACTED_PATH] token=[REDACTED_POSSIBLE_SECRET] [private-session]"
    )
    assert "thread.started" not in status["report"]
    assert status["report_complete"] is True
    assert status["report_capped"] is False


@pytest.mark.asyncio
async def test_structured_report_contains_all_schema_fields_and_survives_restart(tmp_path):
    config, manager, executor, client = make_client(tmp_path)
    await client.start(target="MTP Luna", receipt_id="structured-report-1", prompt="Report.")
    job = manager.get_job(executor.scheduled[0])
    assert job is not None
    manager.update_job_state(
        job.job_id,
        JobState.COMPLETED,
        result={
            "summary": "summary marker",
            "detailed_report": "details marker",
            "evidence": ["evidence marker"],
            "files_changed": ["src/change.py"],
            "commands_run": ["pytest -q"],
            "tests_run": ["test_report"],
            "notes": "notes marker",
            "risks": ["risk marker"],
            "open_questions": ["question marker"],
            "next_steps": ["next marker"],
        },
    )

    status = await client.status(target="MTP Luna", receipt_id="structured-report-1")

    for marker in (
        "summary marker", "details marker", "evidence marker", "src/change.py",
        "pytest -q", "test_report", "notes marker", "risk marker",
        "question marker", "next marker",
    ):
        assert marker in status["report"]
    assert status["report_format"] == "structured"
    assert status["report_complete"] is True

    reloaded = JobManager(config)
    restarted = DesktopTaskClient(
        config, reloaded, RecordingExecutor(), {"MTP Luna": make_target()}
    )
    recovered = await restarted.status(target="MTP Luna", receipt_id="structured-report-1")
    assert recovered["report"] == status["report"]
    assert recovered["report_total_length"] == status["report_total_length"]


@pytest.mark.asyncio
async def test_report_chunks_reassemble_and_cap_metadata_is_explicit(tmp_path):
    _config, manager, executor, client = make_client(tmp_path)
    client.targets["MTP Luna"] = make_target(output_format="markdown")
    await client.start(target="MTP Luna", receipt_id="chunks-1", prompt="Long report.")
    job = manager.get_job(executor.scheduled[0])
    assert job is not None
    source = "A" * (MAX_REPORT_LENGTH + 37)
    manager.update_job_state(job.job_id, JobState.COMPLETED, result={"summary": source})

    pieces = []
    offset = 0
    while True:
        status = await client.status(
            target="MTP Luna", receipt_id="chunks-1", report_offset=offset, report_limit=5000
        )
        pieces.append(status["report"])
        if status["report_next_offset"] is None:
            assert status["report_complete"] is True
            assert status["report_capped"] is True
            assert status["report_total_length"] == MAX_REPORT_LENGTH
            break
        assert status["report_next_offset"] > offset
        offset = status["report_next_offset"]
    assert "".join(pieces) == source[:MAX_REPORT_LENGTH]

    with pytest.raises(DesktopTaskError, match="report_offset"):
        await client.status(target="MTP Luna", receipt_id="chunks-1", report_offset=-1)
    with pytest.raises(DesktopTaskError, match="report_limit"):
        await client.status(target="MTP Luna", receipt_id="chunks-1", report_limit=0)
    with pytest.raises(DesktopTaskError, match="report_limit"):
        await client.status(target="MTP Luna", receipt_id="chunks-1", report_limit=MAX_REPORT_LENGTH)
    with pytest.raises(DesktopTaskError, match="report_offset"):
        await client.status(
            target="MTP Luna", receipt_id="chunks-1", report_offset=MAX_REPORT_LENGTH + 1
        )


@pytest.mark.asyncio
async def test_completed_status_reports_cleanup_pending_without_losing_answer(tmp_path):
    _config, manager, executor, client = make_client(tmp_path)
    await client.start(target="MTP Luna", receipt_id="cleanup-1", prompt="Return a marker.")
    job = manager.get_job(executor.scheduled[0])
    assert job is not None
    manager.update_job_state(
        job.job_id,
        JobState.COMPLETED,
        result={"summary": "ANSWER_PERSISTED"},
        wrapper_cleanup_outcome="cleanup_blocked_untrusted_process_identity",
    )

    status = await client.status(target="MTP Luna", receipt_id="cleanup-1")
    assert status["ok"] is True
    assert status["state"] == "completed"
    assert status["answer"] == "ANSWER_PERSISTED"
    assert status["cleanup_pending"] is True
    assert status["cleanup_warning_code"] == "cleanup_blocked_untrusted_process_identity"
    assert "fail-closed" in status["cleanup_warning"]


@pytest.mark.asyncio
async def test_failed_receipt_redacts_process_error_and_classifies_handoff_recovery(tmp_path):
    _config, manager, executor, client = make_client(tmp_path)
    await client.start(target="MTP Luna", receipt_id="handoff-1", prompt="Continue.")
    job = manager.get_job(executor.scheduled[0])
    assert job is not None
    manager.update_job_state(
        job.job_id,
        JobState.FAILED,
        error="active writer for /private/tmp/visible-task/session-secret",
        result={
            "failure_diagnostic": {
                "category": "active_writer",
                "public_message": "private process detail",
            }
        },
    )
    status = await client.status(target="MTP Luna", receipt_id="handoff-1")
    assert status["error_code"] == "active_writer"
    assert "new receipt_id" in status["error"]
    assert "/private/tmp" not in json.dumps(status)


def test_executor_classifies_desktop_writer_and_archive_failures():
    from patchbay.jobs.executor import JobExecutor

    # The classifier is pure and is the boundary that turns CLI diagnostics
    # into safe receipt guidance.
    executor = object.__new__(JobExecutor)
    active = executor._classify_codex_failure(b"", b"already has an active writer", 1)
    archived = executor._classify_codex_failure(b"thread is archived", b"", 1)
    assert active["category"] == "active_writer"
    assert archived["category"] == "archived_thread"
    assert "new receipt_id" in active["manager_guidance"]
    assert "new receipt_id" in archived["manager_guidance"]


@pytest.mark.asyncio
async def test_retention_prunes_terminal_receipts_but_keeps_active_jobs(tmp_path):
    _config, manager, executor, client = make_client(tmp_path, retention_hours=1)
    await client.start(target="MTP Luna", receipt_id="retained-1", prompt="Continue.")
    job = manager.get_job(executor.scheduled[0])
    assert job is not None
    manager.update_job_state(job.job_id, JobState.COMPLETED, result={"summary": "done"})
    job.completed_at = time.time() - 2 * 3600
    manager._persist_job(job)

    assert client.prune_expired() == 1
    with pytest.raises(DesktopTaskError, match="not found"):
        await client.status(target="MTP Luna", receipt_id="retained-1")


@pytest.mark.asyncio
async def test_periodic_manager_cleanup_uses_desktop_retention_bound(tmp_path):
    _config, manager, executor, client = make_client(tmp_path, retention_hours=1)
    await client.start(target="MTP Luna", receipt_id="retained-2", prompt="Continue.")
    job = manager.get_job(executor.scheduled[0])
    assert job is not None
    manager.update_job_state(job.job_id, JobState.COMPLETED, result={"summary": "done"})
    job.completed_at = time.time() - 2 * 3600
    manager._persist_job(job)

    manager.cleanup_old_jobs()
    assert manager.get_job(job.job_id) is None


@pytest.mark.asyncio
async def test_handler_dispatches_new_start_and_status_surface(monkeypatch):
    from patchbay.tools.handler import ToolHandler

    class FakeClient:
        async def start(self, **kwargs):
            return {"ok": False, "state": "queued", "target": kwargs["target"]}

        async def status(self, **kwargs):
            return {"ok": True, "state": "completed", "target": kwargs["target"]}

    async def no_reconciliation():
        return None

    handler = object.__new__(ToolHandler)
    handler.desktop_task_client = FakeClient()
    handler._reconcile_active_jobs = no_reconciliation
    started = await handler.handle_tool_call(
        DESKTOP_TASK_START_TOOL_NAME,
        {"target": "MTP Luna", "receipt_id": "handler-1", "prompt": "Continue."},
    )
    status = await handler.handle_tool_call(
        DESKTOP_TASK_STATUS_TOOL_NAME,
        {"target": "MTP Luna", "receipt_id": "handler-1"},
    )
    assert started["state"] == "queued"
    assert status["state"] == "completed"
