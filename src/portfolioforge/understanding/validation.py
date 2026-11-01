"""Optional local validation: the only code in PortfolioForge that runs a project.

Nothing here happens by itself. A plan shows the exact commands. You approve that exact
plan by typing a confirmation. Each approval allows one execution, and approving one action
never authorizes another. Before anything runs, the plan's hash, its commands and every source
file's hash are checked again. The project's inspector describes the commands, but cannot widen
what is allowed: tools must be on its list, commands must match what it rebuilds, and the core
applies its own path checks. A project with no inspector gets no plan.

What protects you:

* the program runs from a temporary copy of the approved sources, deleted afterwards, so
  your originals are not touched by this process;
* a reduced environment (no API keys, nothing that configures the tool), no shell, no input;
* a time limit, and capped, redacted output.

**This is not a sandbox.** The program runs with your user account's permissions: it can
read your files and use the network if its code does so. Only approve code you are willing
to run. :data:`EXECUTION_NOTICE` says this at the point of approval.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Callable

from ..grouping.scoring import stable_id
from ..paths import ensure_safe_output_dir
from .inspectors import DEFAULT_REGISTRY, MANUAL_REVIEW_REASON, InspectorRegistry, ProjectInspector, SourceFile
from .models import (
    BuildInspection,
    FileApprovalRecord,
    PlannedFile,
    ValidationApproval,
    ValidationPlan,
    ValidationResult,
    ValidationStep,
    to_jsonable,
)
from .reader import ApprovedFileReader, ReadRefused
from .redact import redact

#: Actions any inspector may offer: "compile" prepares the code; "run" runs it.
ACTIONS = ("compile", "run")
DEFAULT_RUN_TIMEOUT = 30
COMPILE_TIMEOUT = 120
MAX_TIMEOUT = 300
MAX_PLANNED_FILES = 500
MAX_RETAINED_OUTPUT_BYTES = 64_000
MAX_STORED_OUTPUT_CHARS = 4_000

EXECUTION_NOTICE = (
    "THIS RUNS THE PROJECT'S CODE, AND THIS IS NOT A SANDBOX. It runs from a temporary copy with a time limit "
    "and a reduced environment, but with your account's permissions: it could read or change your files or use "
    "the network if its code does so. Only approve code you are willing to run."
)


class ValidationRefused(PermissionError):
    """Raised when something would run without a matching approval. Never includes paths."""


class ValidationUnavailable(ValueError):
    """Raised when no plan can be made (nothing inspected, no inspector, ambiguous entry point, ...)."""


# --- planning (runs nothing) -------------------------------------------------------------------


def _plan_digest(plan: ValidationPlan) -> str:
    body = to_jsonable(plan)
    for volatile in ("id", "plan_sha256", "created_at"):
        body.pop(volatile)
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _safe_relative(path: str) -> bool:
    """The core's own rule, whatever an inspector says: stays inside the temporary folder."""
    pure = PurePosixPath(path)
    return bool(path) and not pure.is_absolute() and ".." not in pure.parts and not path.startswith("-") \
        and ":" not in path and "\\" not in path


def _safe_copy_path(path: str, inspector: ProjectInspector) -> bool:
    return _safe_relative(path) and inspector.is_safe_copy_path(path)


def _choose_entry_point(inspection: BuildInspection, requested: str | None) -> str:
    runnable = [e for e in inspection.entry_points if e.found_in_source]
    if requested:
        if not any(e.name == requested for e in runnable):
            raise ValidationUnavailable("That entry point was not found in the inspected sources. Choose one of: "
                                        + (", ".join(e.name for e in runnable) or "(none found)"))
        return requested
    confirmed = [e for e in runnable if e.status == "confirmed"]
    if len(confirmed) == 1:
        return confirmed[0].name
    if len(runnable) == 1:
        return runnable[0].name
    if not runnable:
        raise ValidationUnavailable("No entry point was found in the inspected sources, so there is nothing to run.")
    raise ValidationUnavailable("More than one entry point was found. Choose with --entry: "
                                + ", ".join(e.name for e in runnable))


def build_plan(*, inspection: BuildInspection, source_approval: FileApprovalRecord, reader: ApprovedFileReader,
               action: str, entry_point: str | None = None, timeout_seconds: int | None = None,
               registry: InspectorRegistry = DEFAULT_REGISTRY, now: datetime | None = None) -> ValidationPlan:
    """A plan you can read and approve. Reads the approved sources to hash them. Runs nothing."""
    inspector = registry.get(inspection.inspector)
    if inspector is None:
        raise ValidationUnavailable(f"Manual review needed. {inspection.manual_review_reason or MANUAL_REVIEW_REASON}")
    if action not in inspector.validation_actions():
        raise ValidationUnavailable("This project type does not offer that validation action.")
    if inspection.source_approval_id != source_approval.id:
        raise ValidationUnavailable("The file approval changed after this inspection. Run inspect again.")
    contents: dict[str, tuple[str, bytes]] = {}
    sources: list[SourceFile] = []
    for approved in source_approval.files:
        if not inspector.is_validation_source(approved.language, approved.project_path):
            continue
        try:
            data = reader.read_bytes(approved.file_id)
        except ReadRefused as exc:
            raise ValidationUnavailable(f"An approved source could not be read: {exc}") from None
        contents[approved.project_path] = (approved.file_id, data)
        sources.append(SourceFile(approved.file_id, approved.project_path, approved.language,
                                  data.decode("utf-8", errors="replace")))
    if not sources:
        raise ValidationUnavailable("No approved sources to work with. Approve them with approve-files first.")
    chosen = _choose_entry_point(inspection, entry_point) if inspector.needs_entry_point(action) else None
    if chosen is not None and not inspector.is_safe_entry_point(chosen):
        raise ValidationUnavailable("The entry point is not a legal name for this project type.")
    spec = inspector.prepare_validation(action, tuple(sources), inspection, chosen)
    if not spec.files:
        raise ValidationUnavailable("No approved sources to work with. Approve them with approve-files first.")
    if len(spec.files) > MAX_PLANNED_FILES:
        raise ValidationUnavailable(f"This plan would need more than {MAX_PLANNED_FILES} source files.")
    planned: list[PlannedFile] = []
    for path in spec.files:
        if path not in contents or not _safe_copy_path(path, inspector):
            raise ValidationUnavailable("An approved source has a path that cannot be copied safely.")
        file_id, data = contents[path]
        planned.append(PlannedFile(file_id, path, hashlib.sha256(data).hexdigest(), len(data)))
    if not inspector.options_are_safe(spec.options):
        raise ValidationUnavailable("The inspector returned settings that are not safe to use.")
    limit = timeout_seconds if timeout_seconds is not None else (DEFAULT_RUN_TIMEOUT if action == "run" else COMPILE_TIMEOUT)
    if not 1 <= limit <= MAX_TIMEOUT:
        raise ValidationUnavailable(f"The time limit must be between 1 and {MAX_TIMEOUT} seconds.")

    warnings = [EXECUTION_NOTICE] if inspector.runs_project_code(action) else []
    warnings += list(spec.warnings)
    warnings.append("Only the listed source files are copied: data files, libraries and resources are not, so a "
                    "failure may only mean one of those is missing.")
    if inspection.requirements:
        declared = ", ".join(f"{r.setting}={r.value}" for r in inspection.requirements)
        warnings.append(f"The project declares {declared}. The installed tools are used as they are, so a difference "
                        "can change the result.")

    created = now or datetime.now(UTC)
    files = tuple(planned)
    draft = ValidationPlan(
        id="", inventory_key=inspection.inventory_key, group_id=inspection.group_id, inspection_id=inspection.id,
        source_approval_id=source_approval.id, inspector=inspector.name, action=action, entry_point=spec.entry_point,
        options=dict(spec.options), files=files,
        commands=inspector.build_commands(action, tuple(f.project_path for f in files), spec.entry_point, spec.options),
        timeout_seconds=limit, warnings=tuple(warnings), runs_project_code=inspector.runs_project_code(action),
        created_at=created, plan_sha256="",
    )
    digest = _plan_digest(draft)
    return replace(draft, id=stable_id("vp", digest, created.isoformat()), plan_sha256=digest)


def format_plan(plan: ValidationPlan, *, redact_names: bool = False) -> str:
    lines = [f"Validation plan {plan.id} ({plan.action})",
             f"  Plan SHA-256: {plan.plan_sha256}",
             f"  Time limit:   {plan.timeout_seconds} seconds per command",
             f"  Files copied to a temporary folder: {len(plan.files)} (deleted afterwards)"]
    if redact_names:
        lines.append(f"  Commands: {len(plan.commands)} (hidden by --redact)")
        if plan.runs_project_code:
            lines.append(f"  {EXECUTION_NOTICE}")
    else:
        lines += [f"    {f.project_path}" for f in plan.files]
        lines.append("  Exact commands, run in that temporary folder without a shell:")
        lines += ["    " + " ".join(c) for c in plan.commands]
        lines.append("  Before you approve:")
        lines += [f"    - {w}" for w in plan.warnings]
    return "\n".join(lines)


def approve_plan(plan: ValidationPlan, now: datetime | None = None) -> ValidationApproval:
    created = now or datetime.now(UTC)
    return ValidationApproval(stable_id("va", plan.id, created.isoformat()), plan.id, plan.plan_sha256, plan.action,
                              created)


# --- running a command (bounded) ---------------------------------------------------------------


@dataclass(frozen=True)
class CommandOutcome:
    exit_code: int | None
    timed_out: bool
    stdout: bytes
    stderr: bytes
    duration: float


Runner = Callable[[list[str], str, dict[str, str], float], CommandOutcome]


def _drain(stream, keep: bytearray) -> None:
    """Read everything so the program never blocks, keeping only the start."""
    while chunk := stream.read(65536):
        room = MAX_RETAINED_OUTPUT_BYTES - len(keep)
        if room > 0:
            keep.extend(chunk[:room])


def _kill_tree(process: subprocess.Popen) -> None:
    if sys.platform == "win32":
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        subprocess.run([os.path.join(system_root, "System32", "taskkill.exe"), "/T", "/F", "/PID", str(process.pid)],
                       capture_output=True, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
    process.kill()


def run_bounded(argv: list[str], cwd: str, env: dict[str, str], timeout: float) -> CommandOutcome:
    """Run without a shell or input. Output is capped; the process tree is killed at the limit."""
    started = time.monotonic()
    options: dict = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" \
        else {"start_new_session": True}
    process = subprocess.Popen(argv, cwd=cwd, env=env, shell=False, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
    out, err = bytearray(), bytearray()
    drains = [threading.Thread(target=_drain, args=(process.stdout, out), daemon=True),
              threading.Thread(target=_drain, args=(process.stderr, err), daemon=True)]
    for thread in drains:
        thread.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(process)
        process.wait()
    for thread in drains:
        thread.join(timeout=5)
    return CommandOutcome(None if timed_out else process.returncode, timed_out, bytes(out), bytes(err),
                          time.monotonic() - started)


def _minimal_environment(work_dir: str, tool_dir: str) -> dict[str, str]:
    """Only what a tool needs. Nothing inherited can configure it or be read by the program."""
    env = {"PATH": tool_dir, "HOME": work_dir, "USERPROFILE": work_dir, "TEMP": work_dir, "TMP": work_dir,
           "TMPDIR": work_dir, "LC_ALL": "C.UTF-8"}
    for name in ("SystemRoot", "WINDIR"):  # Windows cannot start most programs without these
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def _excerpt(data: bytes, work_dir: str) -> tuple[str, dict[str, int]]:
    text = data.decode("utf-8", errors="replace").replace(work_dir, "<temporary copy>")
    clean, counts = redact(text)
    if len(clean) > MAX_STORED_OUTPUT_CHARS:
        clean = clean[:MAX_STORED_OUTPUT_CHARS] + "\n[output truncated]"
    return clean, counts


def _tool_version(runner: Runner, tool_path: str, args: tuple[str, ...], work_dir: str, env: dict[str, str]) -> str:
    """The tool's own version line. Runs only the tool, and only after approval."""
    outcome = runner([tool_path, *args], work_dir, env, 30)
    text = (outcome.stderr + b"\n" + outcome.stdout).decode("utf-8", errors="replace")
    line = next((l.strip() for l in text.splitlines() if l.strip()), "unknown")
    return redact(line[:80])[0]


# --- execution -----------------------------------------------------------------------------------


def execute_plan(plan: ValidationPlan, approval: ValidationApproval | None, reader: ApprovedFileReader, *,
                 runner: Runner = run_bounded, which: Callable[[str], str | None] | None = None,
                 registry: InspectorRegistry = DEFAULT_REGISTRY,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> ValidationResult:
    """Check every gate again, then run the approved commands on a temporary copy."""
    if approval is None or approval.plan_id != plan.id:
        raise ValidationRefused("This validation plan has not been approved. Nothing was run.")
    if approval.used_at is not None:
        raise ValidationRefused("This approval was already used. Approve the plan again to run it again.")
    if approval.action != plan.action:
        raise ValidationRefused("This approval is for a different action. Nothing was run.")
    digest = _plan_digest(plan)
    if digest != plan.plan_sha256 or digest != approval.plan_sha256:
        raise ValidationRefused("The plan differs from what was approved. Nothing was run.")
    inspector = registry.get(plan.inspector)
    if inspector is None:
        raise ValidationRefused("No inspector is available for this plan. Nothing was run.")
    if not inspector.options_are_safe(plan.options) or \
            not all(_safe_copy_path(f.project_path, inspector) for f in plan.files):
        raise ValidationRefused("The plan contains an unsafe value. Nothing was run.")
    if inspector.needs_entry_point(plan.action) and not inspector.is_safe_entry_point(plan.entry_point or ""):
        raise ValidationRefused("The plan's entry point is not a legal name. Nothing was run.")
    expected = inspector.build_commands(plan.action, tuple(f.project_path for f in plan.files), plan.entry_point,
                                        plan.options)
    if tuple(tuple(c) for c in plan.commands) != tuple(tuple(c) for c in expected):
        raise ValidationRefused("The plan's commands are not the ones PortfolioForge builds. Nothing was run.")
    if any(c[0] not in inspector.allowed_tools for c in plan.commands):
        raise ValidationRefused("The plan uses a tool this project type does not allow. Nothing was run.")
    if not all(_safe_relative(d) for d in inspector.scratch_dirs):
        raise ValidationRefused("The plan contains an unsafe value. Nothing was run.")

    contents: dict[str, bytes] = {}
    for planned in plan.files:
        try:
            data = reader.read_bytes(planned.file_id)
        except ReadRefused as exc:
            raise ValidationRefused(f"A source could not be read ({exc}). Nothing was run.") from None
        if hashlib.sha256(data).hexdigest() != planned.sha256:
            raise ValidationRefused("A source changed since this plan was made. Make a new plan. Nothing was run.")
        contents[planned.project_path] = data

    def result(outcome: str, steps: tuple[ValidationStep, ...], versions: dict[str, str], notes: list[str],
               removed: bool) -> ValidationResult:
        finished = now()
        return ValidationResult(stable_id("vr", plan.id, approval.id, finished.isoformat()), plan.id, plan.inspection_id,
                                plan.inventory_key, plan.group_id, plan.action, plan.entry_point, outcome, versions,
                                steps, tuple(dict.fromkeys(notes)), removed, finished)

    locate = which or shutil.which
    tools = {c[0]: locate(c[0]) for c in plan.commands}
    missing = sorted(name for name, path in tools.items() if not path)
    if missing:
        return result("tool_missing", (), {}, [f"Not found on this computer: {', '.join(missing)}. Nothing was run."], True)

    work_dir = tempfile.mkdtemp(prefix="pf-validate-")
    steps: list[ValidationStep] = []
    versions: dict[str, str] = {}
    notes: list[str] = []
    outcome = "tool_missing"
    try:
        ensure_safe_output_dir(work_dir)
        for path, data in contents.items():
            target = Path(work_dir, *PurePosixPath(path).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        for folder in inspector.scratch_dirs:
            Path(work_dir, *PurePosixPath(folder).parts).mkdir(parents=True, exist_ok=True)
        env = _minimal_environment(work_dir, os.path.dirname(next(iter(tools.values()))))
        for name, path in tools.items():
            args = inspector.version_args(name)
            if args:
                versions[name] = _tool_version(runner, path, args, work_dir, env)
        for command in plan.commands:
            stage = inspector.stage_of(command)
            is_compile = stage == "compile"
            limit = COMPILE_TIMEOUT if is_compile and plan.action != "compile" else plan.timeout_seconds
            ran = runner([tools[command[0]], *command[1:]], work_dir, env, limit)
            stdout, out_counts = _excerpt(ran.stdout, work_dir)
            stderr, err_counts = _excerpt(ran.stderr, work_dir)
            counts = {k: out_counts.get(k, 0) + err_counts.get(k, 0) for k in {*out_counts, *err_counts}}
            step = ValidationStep(command[0], command, ran.exit_code, ran.timed_out, round(ran.duration, 2), stdout,
                                  stderr, dict(sorted(counts.items())))
            steps.append(step)
            notes += list(inspector.failure_hints(step.stderr))
            if ran.timed_out:
                notes.append("It reached the time limit. That can be normal for an interactive or long-running program.")
                outcome = f"{stage}_timed_out"
                break
            if ran.exit_code != 0:
                outcome = "compile_failed" if is_compile else "run_nonzero"
                break
            outcome = "compile_passed" if is_compile else "run_exit_zero"
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    removed = not os.path.exists(work_dir)
    if not removed:
        notes.append("The temporary copy could not be fully deleted.")
    return result(outcome, tuple(steps), versions, notes, removed)


def describe_outcome(result: ValidationResult) -> str:
    return {
        "compile_passed": "The compile step exited with code 0. The approved sources compile.",
        "compile_failed": "The compile step reported errors.",
        "compile_timed_out": "The compile step reached the time limit.",
        "run_exit_zero": "The program exited with code 0. That does not show it behaved correctly.",
        "run_nonzero": "The program exited with a non-zero code.",
        "run_timed_out": "The program reached the time limit and was stopped.",
        "tool_missing": "A required tool was not found, so nothing was run.",
    }.get(result.outcome, result.outcome)
