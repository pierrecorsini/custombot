"""
src/diagnose.py — Self-service diagnostic checks for custombot.

Runs a series of checks (config, LLM connectivity, workspace integrity,
disk space, dependencies) and outputs a structured report so users can
troubleshoot issues before filing bugs.

Usage:
    python main.py diagnose
    python main.py diagnose --config my_config.json
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.constants import HEALTH_DISK_FREE_THRESHOLD_MB, WORKSPACE_DIR
from src.ui.cli_output import cli as cli_output


@dataclass
class CheckResult:
    """Outcome of a single diagnostic check."""

    name: str
    passed: bool
    message: str
    details: dict[str, Any] | None = None


@dataclass
class DiagnoseReport:
    """Aggregated diagnostic report."""

    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if not c.passed)

    @property
    def total(self) -> int:
        return len(self.checks)

    @property
    def all_passed(self) -> bool:
        return self.failed == 0 and self.total > 0


# ─────────────────────────────────────────────────────────────────────────────
# Individual checks
# ─────────────────────────────────────────────────────────────────────────────


def check_config_file(config_path: Path) -> CheckResult:
    """Check that config file exists and parses as valid JSON."""
    if not config_path.exists():
        return CheckResult(
            name="Config file",
            passed=False,
            message=f"Not found: {config_path}",
            details={"hint": "Run 'python main.py options' to create one"},
        )

    try:
        with open(config_path, encoding="utf-8") as fh:
            json.load(fh)
    except json.JSONDecodeError as exc:
        return CheckResult(
            name="Config file",
            passed=False,
            message=f"Invalid JSON: {exc}",
        )

    return CheckResult(
        name="Config file",
        passed=True,
        message=f"Valid JSON ({config_path.stat().st_size} bytes)",
    )


def check_config_schema(config_path: Path) -> CheckResult:
    """Validate config against the JSON schema."""
    if not config_path.exists():
        return CheckResult(
            name="Config schema",
            passed=False,
            message="Skipped — config file not found",
        )

    try:
        from src.config.config_schema import validate_config_dict

        with open(config_path, encoding="utf-8") as fh:
            data = json.load(fh)

        result = validate_config_dict(data)
        if result["valid"]:
            return CheckResult(
                name="Config schema",
                passed=True,
                message="All fields valid",
            )

        errors_summary = "; ".join(
            f"{e['path']}: {e['message']}" for e in result["errors"][:5]
        )
        return CheckResult(
            name="Config schema",
            passed=False,
            message=f"{len(result['errors'])} validation error(s): {errors_summary}",
            details={"error_count": len(result["errors"])},
        )
    except Exception as exc:
        return CheckResult(
            name="Config schema",
            passed=False,
            message=f"Schema check failed: {exc}",
        )


def check_config_load(config_path: Path) -> CheckResult:
    """Try to load config into a Config dataclass."""
    if not config_path.exists():
        return CheckResult(
            name="Config load",
            passed=False,
            message="Skipped — config file not found",
        )

    try:
        from src.config import load_config

        cfg = load_config(config_path)
        api_key_set = bool(cfg.llm.api_key) and not cfg.llm.api_key.startswith("sk-your")
        return CheckResult(
            name="Config load",
            passed=True,
            message=f"model={cfg.llm.model}, base_url={cfg.llm.base_url}",
            details={
                "model": cfg.llm.model,
                "base_url": cfg.llm.base_url,
                "api_key_set": api_key_set,
                "allow_all": cfg.whatsapp.allow_all,
                "allowed_numbers_count": len(cfg.whatsapp.allowed_numbers),
            },
        )
    except Exception as exc:
        return CheckResult(
            name="Config load",
            passed=False,
            message=f"Failed: {exc}",
        )


def check_api_key(config_path: Path) -> CheckResult:
    """Check that API key is present (not placeholder)."""
    try:
        from src.config import load_config

        cfg = load_config(config_path)
    except Exception:
        # Check env var fallback
        env_key = os.environ.get("OPENAI_API_KEY", "")
        if env_key:
            return CheckResult(
                name="API key",
                passed=True,
                message="Set via OPENAI_API_KEY env var",
            )
        return CheckResult(
            name="API key",
            passed=False,
            message="Cannot load config to check API key",
        )

    # Check env var override first
    env_key = os.environ.get("OPENAI_API_KEY", "")
    if env_key:
        return CheckResult(
            name="API key",
            passed=True,
            message="Set via OPENAI_API_KEY env var",
        )

    key = cfg.llm.api_key
    if not key:
        return CheckResult(
            name="API key",
            passed=False,
            message="API key is empty",
            details={"hint": "Set llm.api_key in config or OPENAI_API_KEY env var"},
        )

    if key.startswith("sk-your"):
        return CheckResult(
            name="API key",
            passed=False,
            message="API key is still the placeholder value",
            details={"hint": "Replace with your actual API key"},
        )

    return CheckResult(
        name="API key",
        passed=True,
        message=f"Configured ({len(key)} chars)",
    )


async def check_llm_connectivity(config_path: Path) -> CheckResult:
    """Test LLM API connectivity with a lightweight models.list() call."""
    try:
        from src.config import load_config

        cfg = load_config(config_path)
    except Exception as exc:
        return CheckResult(
            name="LLM connectivity",
            passed=False,
            message=f"Cannot load config: {exc}",
        )

    api_key = os.environ.get("OPENAI_API_KEY", cfg.llm.api_key)
    base_url = os.environ.get("OPENAI_BASE_URL", cfg.llm.base_url)

    if not api_key or api_key.startswith("sk-your"):
        return CheckResult(
            name="LLM connectivity",
            passed=False,
            message="Skipped — no valid API key",
        )

    import time

    from openai import AsyncOpenAI

    client = None
    start = time.perf_counter()
    try:
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        await asyncio.wait_for(client.models.list(), timeout=10.0)
        latency_ms = (time.perf_counter() - start) * 1000
        return CheckResult(
            name="LLM connectivity",
            passed=True,
            message="LLM API reachable",
            details={"latency_ms": round(latency_ms, 2)},
        )
    except TimeoutError:
        latency_ms = (time.perf_counter() - start) * 1000
        return CheckResult(
            name="LLM connectivity",
            passed=False,
            message="LLM API timeout after 10s",
            details={"latency_ms": round(latency_ms, 2)},
        )
    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        error_msg = str(exc).lower()
        if "401" in error_msg or "unauthorized" in error_msg or "invalid" in error_msg:
            return CheckResult(
                name="LLM connectivity",
                passed=False,
                message="Invalid API credentials",
            )
        return CheckResult(
            name="LLM connectivity",
            passed=False,
            message=f"Failed: {type(exc).__name__}",
        )
    finally:
        if client is not None:
            await client.close()


async def check_embedding_model(config_path: Path) -> CheckResult:
    """Test embedding API reachability with a single embedding call."""
    try:
        from src.config import load_config

        cfg = load_config(config_path)
    except Exception as exc:
        return CheckResult(
            name="Embedding model",
            passed=False,
            message=f"Cannot load config: {exc}",
        )

    api_key = os.environ.get("OPENAI_API_KEY", cfg.llm.api_key)
    base_url = os.environ.get("OPENAI_BASE_URL", cfg.llm.base_url)

    if not api_key or api_key.startswith("sk-your"):
        return CheckResult(
            name="Embedding model",
            passed=False,
            message="Skipped — no valid API key",
        )

    import time

    from openai import AsyncOpenAI

    client = None
    start = time.perf_counter()
    try:
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        response = await asyncio.wait_for(
            client.embeddings.create(
                model=cfg.llm.embedding_model,
                input="health",
            ),
            timeout=10.0,
        )
        latency_ms = (time.perf_counter() - start) * 1000
        dims = len(response.data[0].embedding) if response.data else 0
        return CheckResult(
            name="Embedding model",
            passed=True,
            message=f"Embedding API reachable (model={cfg.llm.embedding_model}, dims={dims})",
            details={"latency_ms": round(latency_ms, 2), "dimensions": dims},
        )
    except TimeoutError:
        latency_ms = (time.perf_counter() - start) * 1000
        return CheckResult(
            name="Embedding model",
            passed=False,
            message="Embedding API timeout after 10s",
            details={"latency_ms": round(latency_ms, 2)},
        )
    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        error_msg = str(exc).lower()
        if "401" in error_msg or "unauthorized" in error_msg or "invalid" in error_msg:
            return CheckResult(
                name="Embedding model",
                passed=False,
                message="Invalid API credentials",
            )
        return CheckResult(
            name="Embedding model",
            passed=False,
            message=f"Failed: {type(exc).__name__}",
        )
    finally:
        if client is not None:
            await client.close()


def check_workspace_dir(config_path: Path) -> CheckResult:
    """Check workspace directory exists and has expected structure."""
    workspace = Path(WORKSPACE_DIR)

    if not workspace.exists():
        return CheckResult(
            name="Workspace directory",
            passed=False,
            message=f"Not found: {workspace}",
            details={"hint": "Will be created on first 'python main.py start'"},
        )

    # Check key subdirectories
    data_dir = workspace / ".data"
    missing = []
    if not data_dir.exists():
        missing.append(".data/")

    whatsapp_data = workspace / "whatsapp_data"
    if not whatsapp_data.exists():
        missing.append("whatsapp_data/")

    if missing:
        return CheckResult(
            name="Workspace directory",
            passed=True,
            message=f"Exists but missing subdirs: {', '.join(missing)}",
            details={"hint": "These will be created on first run"},
        )

    return CheckResult(
        name="Workspace directory",
        passed=True,
        message=f"Structure OK ({workspace})",
    )


def check_disk_space(config_path: Path) -> CheckResult:
    """Check available disk space on the workspace partition."""
    workspace = Path(WORKSPACE_DIR)

    # Fall back to cwd if workspace doesn't exist yet
    check_path = workspace if workspace.exists() else Path(".")

    try:
        usage = shutil.disk_usage(str(check_path))
        free_mb = usage.free / (1024 * 1024)
        total_mb = usage.total / (1024 * 1024)
        used_pct = (usage.used / usage.total) * 100 if usage.total else 0

        passed = free_mb >= HEALTH_DISK_FREE_THRESHOLD_MB
        return CheckResult(
            name="Disk space",
            passed=passed,
            message=f"{free_mb:.0f} MB free / {total_mb:.0f} MB total ({used_pct:.1f}% used)",
            details={
                "free_mb": round(free_mb, 1),
                "total_mb": round(total_mb, 1),
                "used_percent": round(used_pct, 1),
                "threshold_mb": HEALTH_DISK_FREE_THRESHOLD_MB,
            },
        )
    except OSError as exc:
        return CheckResult(
            name="Disk space",
            passed=False,
            message=f"Cannot check: {exc}",
        )


def check_workspace_integrity(config_path: Path) -> CheckResult:
    """Check workspace files for obvious corruption."""
    workspace = Path(WORKSPACE_DIR)
    if not workspace.exists():
        return CheckResult(
            name="Workspace integrity",
            passed=True,
            message="Skipped — workspace not yet created",
        )

    issues: list[str] = []

    # Check config.json in workspace is valid JSON if it exists
    ws_config = workspace / "config.json"
    if ws_config.exists():
        try:
            json.loads(ws_config.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            issues.append("workspace/config.json: invalid JSON")

    # Check message queue is readable if it exists
    queue_file = workspace / ".data" / "message_queue.jsonl"
    if queue_file.exists():
        malformed = 0
        with open(queue_file, encoding="utf-8") as fh:
            for _, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
        if malformed:
            issues.append(f"message_queue.jsonl: {malformed} malformed line(s)")

    if issues:
        return CheckResult(
            name="Workspace integrity",
            passed=False,
            message=f"{len(issues)} issue(s) found",
            details={"issues": issues},
        )

    return CheckResult(
        name="Workspace integrity",
        passed=True,
        message="No corruption detected",
    )


def check_orphaned_workspace_dirs(config_path: Path) -> CheckResult:
    """Scan workspace/whatsapp_data/ for orphaned chat directories.

    An orphaned directory is one where either:
      - The ``.chat_id`` origin file is missing (incomplete workspace setup),
      - The corresponding JSONL message file in ``.data/messages/`` is empty
        or missing (crashed or interrupted session).

    This runs purely against the filesystem — no Database connection needed.
    """
    workspace = Path(WORKSPACE_DIR)
    whatsapp_data = workspace / "whatsapp_data"

    if not workspace.exists() or not whatsapp_data.exists():
        return CheckResult(
            name="Orphaned workspace dirs",
            passed=True,
            message="Skipped — workspace or whatsapp_data/ not yet created",
        )

    messages_dir = workspace / ".data" / "messages"
    orphans: list[str] = []
    scanned = 0

    try:
        entries = list(whatsapp_data.iterdir())
    except OSError as exc:
        return CheckResult(
            name="Orphaned workspace dirs",
            passed=False,
            message=f"Cannot scan whatsapp_data/: {exc}",
        )

    for entry in sorted(entries):
        if not entry.is_dir():
            continue

        scanned += 1
        dirname = entry.name
        issues_for_dir: list[str] = []

        # Check 1: .chat_id origin file must exist
        origin_file = entry / ".chat_id"
        if not origin_file.exists():
            issues_for_dir.append("missing .chat_id")

        # Check 2: Corresponding JSONL should exist and not be empty
        jsonl_path = messages_dir / f"{dirname}.jsonl"
        if not jsonl_path.exists():
            issues_for_dir.append("no corresponding JSONL")
        elif jsonl_path.stat().st_size == 0:
            issues_for_dir.append("empty JSONL")

        if issues_for_dir:
            detail = f"{dirname} ({', '.join(issues_for_dir)})"
            orphans.append(detail)

    if scanned == 0:
        return CheckResult(
            name="Orphaned workspace dirs",
            passed=True,
            message="No chat directories to scan",
        )

    if orphans:
        # Report as passed=false when orphans are found (actionable)
        # but cap details at 10 to avoid overwhelming output
        shown = orphans[:10]
        return CheckResult(
            name="Orphaned workspace dirs",
            passed=False,
            message=f"{len(orphans)} orphaned director{'y' if len(orphans) == 1 else 'ies'} found "
                    f"(scanned {scanned})",
            details={
                "orphans": shown,
                "total_orphans": len(orphans),
                "hint": "Orphaned directories can be safely deleted if the session is no longer needed",
            },
        )

    return CheckResult(
        name="Orphaned workspace dirs",
        passed=True,
        message=f"All {scanned} director{'y' if scanned == 1 else 'ies'} healthy",
    )


def check_python_env(config_path: Path) -> CheckResult:
    """Check Python version and platform info."""
    py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    min_required = (3, 11)
    passed = sys.version_info >= min_required

    return CheckResult(
        name="Python environment",
        passed=passed,
        message=f"Python {py_version} on {platform.system()} {platform.release()}",
        details={
            "version": py_version,
            "platform": platform.platform(),
            "implementation": platform.python_implementation(),
        },
    )


def check_dependencies(config_path: Path) -> CheckResult:
    """Check that required packages are installed."""
    from src.dependency_check import check_dependencies

    try:
        ok = check_dependencies(auto_update=False, critical_only=False)
        return CheckResult(
            name="Dependencies",
            passed=ok,
            message="All packages installed" if ok else "Some packages missing or outdated",
        )
    except Exception as exc:
        return CheckResult(
            name="Dependencies",
            passed=False,
            message=f"Check failed: {exc}",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Report runner
# ─────────────────────────────────────────────────────────────────────────────


async def run_diagnose(config_path: Path) -> DiagnoseReport:
    """Run all diagnostic checks and return a report."""
    report = DiagnoseReport()

    sync_checks = [
        check_config_file,
        check_config_schema,
        check_config_load,
        check_api_key,
        check_workspace_dir,
        check_disk_space,
        check_workspace_integrity,
        check_orphaned_workspace_dirs,
        check_python_env,
        check_dependencies,
    ]

    for check_fn in sync_checks:
        report.checks.append(check_fn(config_path))

    # Async checks
    report.checks.append(await check_llm_connectivity(config_path))
    report.checks.append(await check_embedding_model(config_path))

    return report


def print_report(report: DiagnoseReport) -> None:
    """Print a formatted diagnostic report to the console."""
    cli_output.rule("CustomBot Diagnostics")

    for check in report.checks:
        icon = "✅" if check.passed else "❌"
        cli_output.raw(f"  {icon} [bold]{check.name}[/bold]: {check.message}")

        if check.details and not check.passed:
            for key, value in check.details.items():
                if key == "hint":
                    cli_output.dim(f"       💡 {value}")
                elif key == "issues":
                    for issue in value:
                        cli_output.dim(f"       • {issue}")

    cli_output.separator()
    if report.all_passed:
        cli_output.success(
            f"All {report.total} checks passed — everything looks good!"
        )
    else:
        cli_output.warning(
            f"{report.passed}/{report.total} checks passed, "
            f"{report.failed} issue(s) found"
        )
        cli_output.dim(
            "Fix the issues above, then re-run 'python main.py diagnose' to verify."
        )


def run_diagnose_cli(config_path: Path) -> None:
    """Entry point for the CLI diagnose command."""
    report = asyncio.run(run_diagnose(config_path))
    print_report(report)
    sys.exit(0 if report.all_passed else 1)
