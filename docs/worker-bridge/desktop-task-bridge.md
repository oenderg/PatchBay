# Codex Desktop task bridge (experimental)

PatchBay can optionally continue a pre-existing Codex Desktop task through a
durable local `codex exec resume --json` job. The public tools are
`codex_desktop_task_start` and `codex_desktop_task_status`. They accept a
human alias and a caller-owned `receipt_id`; the configured session id, model,
workspace, CLI path, and job id stay in private runtime state.

The bridge is disabled unless `desktop_tasks.enabled: true` and an absolute,
existing regular mode-0600 `desktop_tasks.targets_file` containing valid JSON
targets are configured. Each underlying Desktop task id may appear only once
in that file. A target entry contains the private Desktop task id and may pin its cwd, model, reasoning effort,
sandbox, profile, and `skip_git_repo_check`. Do not commit the targets file.

Targets default to `output_format: structured`. Set that private field to
`markdown` when the Desktop transcript should show the model's ordinary
Markdown final response. Markdown mode keeps `--json` lifecycle events but
does not pass PatchBay's `--output-schema`; the parser takes the final
`agent_message` as the semantic report and never publishes the raw JSONL
stream. Structured mode keeps the existing schema-constrained command.

## Desktop handoff

Desktop remains the owner of archive state and the transcript viewer. Before a
turn, use this sequence in Desktop:

1. Archive the task in Desktop.
2. Unarchive the task in Desktop.
3. Leave it idle and unloaded.
4. Call `codex_desktop_task_start` with a new `receipt_id`.

PatchBay never edits session files and never archives or unarchives a task.
Opening or navigating to the task for inspection is allowed, but sending a
native Desktop message can reclaim the writer while the CLI turn is running.

If the start receipt fails with `active_writer`, wait for the Desktop writer
to settle, perform the Desktop archive/unarchive sequence again, leave the task
idle/unloaded, and retry with a **new** `receipt_id`. If it fails with
`archived_thread`, refresh or navigate in Desktop, perform that same recovery
there, and retry with a new receipt. Do not attempt archive/unarchive through
the CLI: a CLI-only change can move the rollout file while Desktop's task
index remains stale.

If a previous turn completed while its process supervisor could not prove
detached-child ownership, a later start may clear that terminal barrier only
after fresh local observations prove that the recorded supervisor, its
uncertainty sentinel, process group, and marked descendants are all gone. If
the sentinel itself is the only process carrying the job marker, has its own
isolated session/process group, and tracked descendants are proven absent,
PatchBay may terminate that matching orphan sentinel and repeat the absence
checks. The private uncertainty proof is retained as recovery evidence.
Unknown or live observations, ambiguous marker membership, mismatched process
structure, or incomplete scans remain blocked; PatchBay never kills an
unidentified process or manufactures cleanup proof.

## Receipt lifecycle

`codex_desktop_task_start` returns promptly with `queued` or `running`. Read
progress with `codex_desktop_task_status`; status is local process/job
monitoring and does not ask a model to poll. A receipt becomes `completed` when
PatchBay has persisted a semantic Codex answer, including the case where a
wrapper exits nonzero after `turn.completed` or an equivalent session terminal
event. That result includes a `wrapper_exit_after_answer` warning. If process
cleanup remains blocked by an untrusted identity, status preserves the answer
and adds `cleanup_pending: true` plus a machine-readable cleanup warning;
starting another turn remains fail-closed until local ownership is recovered.
Other process failures become `failed` with bounded recovery guidance.

For a completed receipt, `report` is the sanitized durable report chunk.
`report_format` is `structured` or `markdown`, `report_total_length` is the
stored sanitized length, `report_offset` is the character offset returned,
`report_next_offset` is the next offset or `null`, and `report_complete`
indicates whether that response reached the end. `report_capped` indicates
that the report exceeded the absolute 200,000-character storage cap. Each
status response accepts an optional `report_offset` and `report_limit` up to
12,000 characters. Paths below the configured target cwd become repository
relative; other local paths, configured private values, secret-like content,
and internal UUIDs are redacted while Markdown formatting is retained.

One target has at most one active turn. Reusing a receipt with the same
request is idempotent; changing its prompt or options is rejected. A failed or
completed receipt is retained for the configured bounded period (default 24
hours, capped at seven days), then its private record and result are eligible
for cleanup. Restarting PatchBay reloads the durable receipt and result; an
in-flight process is reconciled by the local executor.

## Private local trial

Create a mode-0600 targets file outside the repository, then start PatchBay
with the normal local configuration and worker mode:

```bash
chmod 600 /private/path/to/desktop-task-targets.json
patchbay start --config /private/path/to/config.yaml --root /private/path/to/repo --tool-mode worker
```

In ChatGPT, call `codex_desktop_task_start` with the configured alias, a fresh
receipt such as `desktop-trial-1`, and the bounded prompt. Then call
`codex_desktop_task_status` with the same alias and receipt until it reaches a
terminal state. Do not place a real task id, local path, prompt, or runtime
log in tracked documentation.
