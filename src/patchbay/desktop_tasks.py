"""Opt-in durable bridge for continuing a handed-off Codex Desktop task.

The public API uses a private operator allowlist and human aliases. The actual
Codex session id, execution paths, and job id remain private runtime state.
Desktop owns archive/unarchive and writer handoff; this module only starts and
reads bounded local CLI jobs through PatchBay's durable executor.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
from pathlib import Path
import re
import stat
import threading
import time
from typing import Any, Mapping, Optional

from patchbay.jobs.manager import (
    JobInfo,
    JobState,
    terminal_cleanup_pending,
    terminal_cleanup_recovery_required,
)
from patchbay.security import redact_local_paths, redact_text
from patchbay.workers.model_options import build_reasoning_config_override


MAX_ALIAS_LENGTH = 80
MAX_RECEIPT_LENGTH = 128
MAX_PROMPT_LENGTH = 4_000
MAX_ANSWER_LENGTH = 12_000
MAX_TIMEOUT_MS = 24 * 60 * 60 * 1_000
DEFAULT_TIMEOUT_MS = 30 * 60 * 1_000
DEFAULT_RETENTION_HOURS = 24
MAX_RETENTION_HOURS = 7 * 24

DESKTOP_TASK_MARKER = "_desktop_task"
DESKTOP_TASK_ALIAS_OPTION = "_desktop_task_alias"
DESKTOP_TASK_RECEIPT_OPTION = "_desktop_task_receipt_id"
DESKTOP_TASK_DIGEST_OPTION = "_desktop_task_request_digest"
DESKTOP_TASK_TIMEOUT_OPTION = "_desktop_task_timeout_ms"
DESKTOP_TASK_CODEX_BIN_OPTION = "_desktop_task_codex_bin"

_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.: -]{0,79}$")
_RECEIPT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,159}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,159}$")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$")
_CODEX_BIN_RE = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9._/+:\-]{0,255}|/[A-Za-z0-9._/+:\-]{1,255})$")
_SESSION_ID_RE = re.compile(
    r"(?<![A-Za-z0-9])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![A-Za-z0-9])"
)
_REASONING = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
_SANDBOXES = frozenset({"read-only", "workspace-write", "danger-full-access"})


class DesktopTaskError(ValueError):
    """Validation/configuration error safe to report to the MCP caller."""


@dataclasses.dataclass(frozen=True)
class DesktopTaskTarget:
    alias: str
    thread_id: str
    cwd: str = ""
    model: str = ""
    reasoning_effort: str = ""
    sandbox: str = ""
    profile: str = ""
    skip_git_repo_check: bool = False


@dataclasses.dataclass(frozen=True)
class DesktopTaskOptions:
    cwd: str = ""
    model: str = ""
    reasoning_effort: str = ""
    sandbox: str = ""
    profile: str = ""
    skip_git_repo_check: bool = False


def _text(value: Any, *, field: str, maximum: int, required: bool = True) -> str:
    if not isinstance(value, str):
        raise DesktopTaskError(f"{field} must be text")
    if required and not value:
        raise DesktopTaskError(f"{field} is required")
    if len(value) > maximum:
        raise DesktopTaskError(f"{field} is too long")
    return value


def _alias(value: Any) -> str:
    result = " ".join(_text(value, field="target", maximum=MAX_ALIAS_LENGTH).split())
    if not _ALIAS_RE.fullmatch(result) or _THREAD_ID_RE.fullmatch(result):
        raise DesktopTaskError("target must be an allowlisted logical alias")
    return result


def _receipt(value: Any) -> str:
    result = _text(value, field="receipt_id", maximum=MAX_RECEIPT_LENGTH)
    if not _RECEIPT_RE.fullmatch(result):
        raise DesktopTaskError("receipt_id contains unsupported characters")
    return result


def _thread_id(value: Any) -> str:
    result = _text(value, field="thread id", maximum=160)
    if not _THREAD_ID_RE.fullmatch(result):
        raise DesktopTaskError("allowlist contains an invalid thread id")
    return result


def _model(value: Any, field: str = "model") -> str:
    result = _text(value, field=field, maximum=160, required=False).strip()
    if result and not _MODEL_RE.fullmatch(result):
        raise DesktopTaskError(f"{field} contains unsupported characters")
    return result


def _reasoning(value: Any) -> str:
    result = _text(value, field="reasoning_effort", maximum=16, required=False).strip().lower()
    if result and result not in _REASONING:
        raise DesktopTaskError("reasoning_effort is unsupported")
    return result


def _sandbox(value: Any) -> str:
    result = _text(value, field="sandbox", maximum=32, required=False).strip().lower()
    if result and result not in _SANDBOXES:
        raise DesktopTaskError("sandbox is unsupported")
    return result


def _profile(value: Any) -> str:
    result = _text(value, field="profile", maximum=120, required=False).strip()
    if result and not _PROFILE_RE.fullmatch(result):
        raise DesktopTaskError("profile contains unsupported characters")
    return result


def _cwd(value: Any) -> str:
    result = _text(value, field="cwd", maximum=1_024, required=False).strip()
    if not result:
        return ""
    path = Path(result).expanduser()
    if not path.is_absolute():
        raise DesktopTaskError("cwd must be an absolute path")
    return str(path)


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise DesktopTaskError(f"{field} must be a boolean")
    return value


def _codex_bin(value: Any) -> str:
    result = _text(value, field="codex_bin", maximum=256, required=False).strip()
    if not result:
        return "codex"
    if not _CODEX_BIN_RE.fullmatch(result) or ".." in Path(result).parts:
        raise DesktopTaskError("codex_bin contains unsupported characters")
    return result


def _private_path(value: Any, *, field: str) -> Path:
    """Resolve a private configuration path without accepting cwd-relative data."""
    text = _text(value, field=field, maximum=1_024).strip()
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise DesktopTaskError(f"{field} must be an absolute path")
    return path


def _validated_targets_path(value: Any) -> Path:
    """Validate the private targets file before catalog or runtime use."""
    if isinstance(value, Path):
        value = str(value)
    path = _private_path(value, field="desktop_tasks.targets_file")
    try:
        metadata = path.stat()
    except OSError as exc:
        raise DesktopTaskError("desktop task targets file must exist") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise DesktopTaskError("desktop task targets file must be a regular file")
    if metadata.st_mode & 0o077:
        raise DesktopTaskError("desktop task targets file must be private (mode 0600 or stricter)")
    return path


def _target(alias: str, value: Any) -> DesktopTaskTarget:
    if isinstance(value, str):
        return DesktopTaskTarget(alias=alias, thread_id=_thread_id(value))
    if not isinstance(value, Mapping):
        raise DesktopTaskError("allowlist targets must be thread ids or objects")
    return DesktopTaskTarget(
        alias=alias,
        thread_id=_thread_id(value.get("thread_id", value.get("session_id"))),
        cwd=_cwd(value.get("cwd", "")),
        model=_model(value.get("model", "")),
        reasoning_effort=_reasoning(value.get("reasoning_effort", "")),
        sandbox=_sandbox(value.get("sandbox", "")),
        profile=_profile(value.get("profile", "")),
        skip_git_repo_check=_bool(value.get("skip_git_repo_check", False), "skip_git_repo_check"),
    )


def load_desktop_targets(path: Path) -> dict[str, DesktopTaskTarget]:
    path = _validated_targets_path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except DesktopTaskError:
        raise
    except (OSError, ValueError) as exc:
        raise DesktopTaskError("unable to read desktop task targets file") from exc
    raw_targets = payload.get("targets") if isinstance(payload, dict) else None
    if not isinstance(raw_targets, dict) or not raw_targets:
        raise DesktopTaskError("desktop task targets file must contain targets")
    targets: dict[str, DesktopTaskTarget] = {}
    thread_ids: set[str] = set()
    for raw_alias, raw_target in raw_targets.items():
        alias = _alias(raw_alias)
        if alias in targets:
            raise DesktopTaskError("desktop task targets contain a duplicate alias")
        target = _target(alias, raw_target)
        if target.thread_id in thread_ids:
            raise DesktopTaskError("desktop task targets contain a duplicate thread id")
        thread_ids.add(target.thread_id)
        targets[alias] = target
    return targets


def desktop_tasks_enabled(config: Mapping[str, Any]) -> bool:
    settings = config.get("desktop_tasks")
    if not isinstance(settings, Mapping) or settings.get("enabled") is not True:
        return False
    try:
        _bounded_int(
            settings.get("timeout_ms"),
            field="desktop_tasks.timeout_ms",
            default=DEFAULT_TIMEOUT_MS,
            minimum=1_000,
            maximum=MAX_TIMEOUT_MS,
        )
        _bounded_int(
            settings.get("retention_hours"),
            field="desktop_tasks.retention_hours",
            default=DEFAULT_RETENTION_HOURS,
            minimum=1,
            maximum=MAX_RETENTION_HOURS,
        )
        _codex_bin(settings.get("codex_bin", "codex"))
        load_desktop_targets(_validated_targets_path(settings.get("targets_file")))
    except DesktopTaskError:
        return False
    return True


def _bounded_int(value: Any, *, field: str, default: int, minimum: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise DesktopTaskError(f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise DesktopTaskError(f"{field} must be an integer") from exc
    return max(minimum, min(parsed, maximum))


def build_desktop_resume_command(
    codex_bin: str,
    target: DesktopTaskTarget,
    options: DesktopTaskOptions,
) -> list[str]:
    """Build the private CLI argv used by the durable executor."""
    command = [codex_bin, "exec"]
    if options.sandbox:
        command.extend(["--sandbox", options.sandbox])
    if options.cwd:
        command.extend(["--cd", options.cwd])
    if options.profile:
        command.extend(["--profile", options.profile])
    if options.skip_git_repo_check:
        command.append("--skip-git-repo-check")
    command.append("--json")
    if options.model:
        command.extend(["--model", options.model])
    if options.reasoning_effort:
        command.extend(["-c", build_reasoning_config_override(options.reasoning_effort)])
    command.extend(["resume", target.thread_id, "-"])
    return command


def _request_digest(
    alias: str,
    prompt: str,
    options: DesktopTaskOptions,
    timeout_ms: int,
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "target": alias,
                "prompt": prompt,
                "options": dataclasses.asdict(options),
                "timeout_ms": timeout_ms,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _state_for_job(job: JobInfo) -> str:
    if job.state == JobState.PENDING:
        return "queued"
    if job.state == JobState.RUNNING:
        return "running"
    return "completed" if job.state == JobState.COMPLETED else "failed"


def _error_code(job: JobInfo) -> str:
    result = job.result if isinstance(job.result, dict) else {}
    diagnostic = result.get("failure_diagnostic") if isinstance(result.get("failure_diagnostic"), dict) else {}
    category = str(diagnostic.get("category") or "").strip()
    if category:
        return category
    message = str(job.error or "").lower()
    if "active writer" in message or "active_writer" in message:
        return "active_writer"
    if "archived" in message:
        return "archived_thread"
    if "timed out" in message or "timeout" in message:
        return "timeout"
    if job.state == JobState.CANCELLED:
        return "cancelled"
    return "desktop_task_failed"


def _private_answer(
    value: Any,
    target: DesktopTaskTarget,
    private_values: tuple[str, ...] = (),
) -> tuple[str, bool]:
    if isinstance(value, dict):
        selected = None
        for key in ("answer", "detailed_report", "summary", "message"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                selected = candidate
                break
        # Do not serialize an arbitrary result object: it may contain private
        # session/path fields that are not part of the public Desktop receipt.
        value = selected or ""
    answer = str(value or "").strip()
    for private_value in (
        target.thread_id,
        target.cwd,
        target.model,
        target.profile,
        *private_values,
    ):
        if private_value and len(private_value) > 2:
            answer = answer.replace(private_value, "[private]")
    answer = redact_text(redact_local_paths(answer))
    answer = _SESSION_ID_RE.sub("[private-session]", answer)
    truncated = len(answer) > MAX_ANSWER_LENGTH
    return answer[:MAX_ANSWER_LENGTH], truncated


def _public_error(code: str) -> str:
    """Return a bounded, path-free error for the public receipt surface."""
    messages = {
        "active_writer": (
            "Desktop still owns the task writer. Recover ownership in Desktop, "
            "then retry with a new receipt_id."
        ),
        "archived_thread": (
            "Desktop still indexes the task as archived. Recover archive state in Desktop, "
            "then retry with a new receipt_id."
        ),
        "timeout": "Codex did not complete before the configured timeout.",
        "cancelled": "PatchBay stopped the Desktop task before completion.",
        "codex_auth_refresh_failed": "Codex authentication failed before the Desktop task could run.",
        "codex_model_unavailable": "Codex rejected the selected model before the Desktop task could run.",
        "codex_workspace_trust_failed": "Codex rejected the Desktop task workspace trust configuration.",
        "codex_usage_limit": "Codex could not run the Desktop task because its current usage quota is exhausted.",
    }
    return messages.get(
        code,
        "The Desktop task did not complete; inspect the task in Desktop and local PatchBay diagnostics before retrying.",
    )


class DesktopTaskClient:
    """Durable alias-only facade over PatchBay's local Codex executor."""

    def __init__(
        self,
        config: Mapping[str, Any],
        job_manager: Any,
        job_executor: Any,
        targets: Mapping[str, DesktopTaskTarget],
        *,
        codex_bin: str = "codex",
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        retention_hours: int = DEFAULT_RETENTION_HOURS,
    ):
        self.config = config
        self.job_manager = job_manager
        self.job_executor = job_executor
        self.targets = dict(targets)
        thread_ids: set[str] = set()
        for target in self.targets.values():
            if target.thread_id in thread_ids:
                raise DesktopTaskError("desktop task targets contain a duplicate thread id")
            thread_ids.add(target.thread_id)
        self.codex_bin = _codex_bin(codex_bin)
        self.timeout_ms = _bounded_int(
            timeout_ms,
            field="desktop_tasks.timeout_ms",
            default=DEFAULT_TIMEOUT_MS,
            minimum=1_000,
            maximum=MAX_TIMEOUT_MS,
        )
        self.retention_hours = _bounded_int(
            retention_hours,
            field="desktop_tasks.retention_hours",
            default=DEFAULT_RETENTION_HOURS,
            minimum=1,
            maximum=MAX_RETENTION_HOURS,
        )
        configured_repo = (config.get("repositories") or {}).get("default")
        self._private_output_values = tuple(
            value
            for value in (
                str(configured_repo or ""),
                self.codex_bin if "/" in self.codex_bin else "",
            )
            if value
        )
        self._lock = threading.RLock()

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        job_manager: Any,
        job_executor: Any,
    ) -> Optional["DesktopTaskClient"]:
        settings = config.get("desktop_tasks")
        if not isinstance(settings, Mapping) or settings.get("enabled") is not True:
            return None
        timeout_ms = _bounded_int(
            settings.get("timeout_ms"),
            field="desktop_tasks.timeout_ms",
            default=DEFAULT_TIMEOUT_MS,
            minimum=1_000,
            maximum=MAX_TIMEOUT_MS,
        )
        retention_hours = _bounded_int(
            settings.get("retention_hours"),
            field="desktop_tasks.retention_hours",
            default=DEFAULT_RETENTION_HOURS,
            minimum=1,
            maximum=MAX_RETENTION_HOURS,
        )
        client = cls(
            config,
            job_manager,
            job_executor,
            load_desktop_targets(_validated_targets_path(settings.get("targets_file"))),
            codex_bin=_codex_bin(settings.get("codex_bin", "codex")),
            timeout_ms=timeout_ms,
            retention_hours=retention_hours,
        )
        client.prune_expired()
        return client

    def _jobs(self) -> list[JobInfo]:
        lock = getattr(self.job_manager, "_state_lock", None)
        if lock is None:
            return [
                job
                for job in list(getattr(self.job_manager, "jobs", {}).values())
                if bool((job.options or {}).get(DESKTOP_TASK_MARKER))
            ]
        with lock:
            return [
                job
                for job in list(getattr(self.job_manager, "jobs", {}).values())
                if bool((job.options or {}).get(DESKTOP_TASK_MARKER))
            ]

    def _job_for_receipt(self, receipt_id: str) -> Optional[JobInfo]:
        matches = [
            job
            for job in self._jobs()
            if str((job.options or {}).get(DESKTOP_TASK_RECEIPT_OPTION) or "") == receipt_id
        ]
        if not matches:
            return None
        return sorted(matches, key=lambda item: float(item.started_at or item.completed_at or 0))[-1]

    def _public(self, job: JobInfo, target: DesktopTaskTarget) -> dict[str, Any]:
        state = _state_for_job(job)
        result: dict[str, Any] = {
            "ok": state == "completed",
            "target": target.alias,
            "receipt_id": str((job.options or {}).get(DESKTOP_TASK_RECEIPT_OPTION) or ""),
            "state": state,
        }
        if job.event_count:
            result["event_count"] = int(job.event_count)
        if terminal_cleanup_pending(job.wrapper_cleanup_outcome):
            result["cleanup_pending"] = True
            if terminal_cleanup_recovery_required(job.wrapper_cleanup_outcome):
                result["cleanup_warning_code"] = str(job.wrapper_cleanup_outcome)
                result["cleanup_warning"] = (
                    "PatchBay retained a fail-closed cleanup barrier; recover local process ownership "
                    "before starting another Desktop task turn."
                )
        if state == "completed" and isinstance(job.result, dict):
            answer, truncated = _private_answer(job.result, target, self._private_output_values)
            if answer:
                result["answer"] = answer
                result["answer_truncated"] = truncated
        if state == "failed":
            result["error_code"] = _error_code(job)
            result["error"] = _public_error(result["error_code"])
        if state == "completed" and job.exit_code not in (None, 0):
            result["warning_code"] = "wrapper_exit_after_answer"
            result["warning"] = "Codex persisted a final answer before its wrapper exited nonzero; the answer was retained."
        return result

    def _options(self, target: DesktopTaskTarget, alias: str, receipt_id: str, digest: str, timeout_ms: int) -> dict[str, Any]:
        overrides = []
        if target.reasoning_effort:
            overrides.append(build_reasoning_config_override(target.reasoning_effort))
        return {
            "resume_session_id": target.thread_id,
            "_codex_cwd": target.cwd,
            "model": target.model,
            "sandbox": target.sandbox,
            "profile": target.profile,
            "structured_output": True,
            "json_events": True,
            "skip_git_repo_check": target.skip_git_repo_check,
            "config_overrides": overrides,
            DESKTOP_TASK_MARKER: True,
            DESKTOP_TASK_ALIAS_OPTION: alias,
            DESKTOP_TASK_RECEIPT_OPTION: receipt_id,
            DESKTOP_TASK_DIGEST_OPTION: digest,
            DESKTOP_TASK_TIMEOUT_OPTION: timeout_ms,
            DESKTOP_TASK_CODEX_BIN_OPTION: self.codex_bin,
        }

    def _create_job(self, alias: str, receipt_id: str, prompt: str, target: DesktopTaskTarget, timeout_ms: int) -> JobInfo:
        options = DesktopTaskOptions(
            cwd=target.cwd,
            model=target.model,
            reasoning_effort=target.reasoning_effort,
            sandbox=target.sandbox,
            profile=target.profile,
            skip_git_repo_check=target.skip_git_repo_check,
        )
        digest = _request_digest(alias, prompt, options, timeout_ms)
        with self._lock:
            self.prune_expired()
            existing = self._job_for_receipt(receipt_id)
            if existing is not None:
                if str((existing.options or {}).get(DESKTOP_TASK_DIGEST_OPTION) or "") != digest:
                    raise DesktopTaskError("receipt_id was already used for another request")
                return existing
            for job in self._jobs():
                job_alias = str((job.options or {}).get(DESKTOP_TASK_ALIAS_OPTION) or "")
                job_thread_id = str((job.options or {}).get("resume_session_id") or "")
                active = job.state in {JobState.PENDING, JobState.RUNNING}
                active = active or terminal_cleanup_pending(job.wrapper_cleanup_outcome)
                if (job_alias == alias or job_thread_id == target.thread_id) and active:
                    reconcile = getattr(
                        self.job_executor,
                        "reconcile_stale_terminal_cleanup",
                        None,
                    )
                    if callable(reconcile) and reconcile(job.job_id):
                        refreshed = self.job_manager.get_job(job.job_id)
                        if refreshed is not None:
                            job = refreshed
                            active = job.state in {JobState.PENDING, JobState.RUNNING}
                            active = active or terminal_cleanup_pending(
                                job.wrapper_cleanup_outcome
                            )
                    if not active:
                        continue
                    raise DesktopTaskError("this Desktop task already has an active turn; wait for its receipt")
            repo_path = str((self.config.get("repositories") or {}).get("default") or "")
            if not repo_path:
                raise DesktopTaskError("PatchBay has no default workspace for the Desktop task job")
            try:
                job_id = self.job_manager.create_job(
                    "resume",
                    prompt,
                    repo_path,
                    self._options(target, alias, receipt_id, digest, timeout_ms),
                )
            except RuntimeError as exc:
                raise DesktopTaskError("PatchBay's local job capacity is full; wait and retry with a new receipt_id") from exc
            except ValueError as exc:
                raise DesktopTaskError("PatchBay could not accept the configured Desktop task workspace") from exc
            job = self.job_manager.get_job(job_id)
            if job is None:
                raise DesktopTaskError("PatchBay could not persist the Desktop task receipt")
            return job

    async def start(self, *, target: Any, receipt_id: Any, prompt: Any, timeout_ms: Any = None) -> dict[str, Any]:
        alias = _alias(target)
        receipt = _receipt(receipt_id)
        message = _text(prompt, field="prompt", maximum=MAX_PROMPT_LENGTH)
        target_record = self.targets.get(alias)
        if target_record is None:
            raise DesktopTaskError("target alias is not allowlisted")
        bounded_timeout = _bounded_int(
            timeout_ms,
            field="timeout_ms",
            default=self.timeout_ms,
            minimum=1_000,
            maximum=self.timeout_ms,
        )
        job = await asyncio.to_thread(
            self._create_job,
            alias,
            receipt,
            message,
            target_record,
            bounded_timeout,
        )
        try:
            self.job_executor.schedule_job(job.job_id)
        except Exception as exc:
            self.job_manager.update_job_state(job.job_id, JobState.FAILED, error="PatchBay could not schedule the Desktop task turn.")
            raise DesktopTaskError("PatchBay could not schedule the Desktop task turn") from exc
        return self._public(job, target_record)

    async def status(self, *, target: Any, receipt_id: Any) -> dict[str, Any]:
        alias = _alias(target)
        receipt = _receipt(receipt_id)
        target_record = self.targets.get(alias)
        if target_record is None:
            raise DesktopTaskError("target alias is not allowlisted")
        await asyncio.to_thread(self.prune_expired)
        job = self._job_for_receipt(receipt)
        if job is None or str((job.options or {}).get(DESKTOP_TASK_ALIAS_OPTION) or "") != alias:
            raise DesktopTaskError("receipt_id was not found for this target or has expired")
        return self._public(job, target_record)

    def prune_expired(self) -> int:
        cutoff = time.time() - self.retention_hours * 3600
        removed = 0
        for job in self._jobs():
            if job.state not in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}:
                continue
            if terminal_cleanup_pending(job.wrapper_cleanup_outcome):
                continue
            if job.completed_at is None or float(job.completed_at) >= cutoff:
                continue
            try:
                if self.job_manager.cleanup_job(job.job_id):
                    removed += 1
            except Exception:
                continue
        return removed


__all__ = [
    "DESKTOP_TASK_MARKER",
    "DesktopTaskClient",
    "DesktopTaskError",
    "DesktopTaskOptions",
    "DesktopTaskTarget",
    "MAX_ANSWER_LENGTH",
    "MAX_ALIAS_LENGTH",
    "MAX_PROMPT_LENGTH",
    "MAX_RECEIPT_LENGTH",
    "build_desktop_resume_command",
    "desktop_tasks_enabled",
    "load_desktop_targets",
]
