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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.constants import HEALTH_DISK_FREE_THRESHOLD_MB, WORKSPACE_DIR
from src.ui.cli_output import cli as cli_output

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@dataclass(slots=True)
class CheckResult:
    """Outcome of a single diagnostic check."""

    name: str
    passed: bool
    message: str
    details: dict[str, Any] | None = None


@dataclass(slots=True)
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


@dataclass(slots=True)
class _ProbeSuccess:
    """Internal result type returned by probe callables."""

    message: str
    details: dict[str, Any] | None = None


async def _probe_api_endpoint(
    config_path: Path,
    *,
    check_name: str,
    probe_fn: Callable[..., Awaitable[_ProbeSuccess]],
    timeout_label: str,
    timeout_seconds: float = 10.0,
) -> CheckResult:
    """Shared helper for API connectivity probes.

    Handles config loading, credential resolution, OpenAI client lifecycle,
    timing, timeout handling, and error classification so that individual
    checks only need to supply the actual API call and a success message.
    """
    import time

    from openai import AsyncOpenAI

    try:
        from src.config import load_config

        cfg = load_config(config_path)
    except Exception as exc:
        return CheckResult(
            name=check_name,
            passed=False,
            message=f"Cannot load config: {exc}",
        )

    api_key = os.environ.get("OPENAI_API_KEY", cfg.llm.api_key)
    base_url = os.environ.get("OPENAI_BASE_URL", cfg.llm.base_url)

    if not api_key or api_key.startswith("sk-your"):
        return CheckResult(
            name=check_name,
            passed=False,
            message="Skipped — no valid API key",
        )

    client: AsyncOpenAI | None = None
    start = time.perf_counter()
    try:
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        result = await asyncio.wait_for(probe_fn(client, cfg), timeout=timeout_seconds)
        latency_ms = (time.perf_counter() - start) * 1000
        details: dict[str, Any] = {"latency_ms": round(latency_ms, 2)}
        if result.details:
            details.update(result.details)
        return CheckResult(
            name=check_name,
            passed=True,
            message=result.message,
            details=details,
        )
    except TimeoutError:
        latency_ms = (time.perf_counter() - start) * 1000
        return CheckResult(
            name=check_name,
            passed=False,
            message=f"{timeout_label} timeout after {timeout_seconds:.0f}s",
            details={"latency_ms": round(latency_ms, 2)},
        )
    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        error_msg = str(exc).lower()
        if "401" in error_msg or "unauthorized" in error_msg or "invalid" in error_msg:
            return CheckResult(
                name=check_name,
                passed=False,
                message="Invalid API credentials",
            )
        return CheckResult(
            name=check_name,
            passed=False,
            message=f"Failed: {type(exc).__name__}",
        )
    finally:
        if client is not None:
            await client.close()


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
        from src.config.config_schema_defs import validate_config_dict

        with open(config_path, encoding="utf-8") as fh:
            data = json.load(fh)

        result = validate_config_dict(data)
        if result["valid"]:
            return CheckResult(
                name="Config schema",
                passed=True,
                message="All fields valid",
            )

        errors_summary = "; ".join(f"{e['path']}: {e['message']}" for e in result["errors"][:5])
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


def _validate_json_content_type(content_type: str | None) -> str | None:
    """Return an error message if *content_type* is not JSON, else ``None``.

    A misconfigured reverse proxy returning HTML (e.g. a login page) will
    be caught here instead of producing confusing JSON parse errors.
    """
    if content_type is None:
        return "No Content-Type header in API response"
    mime = content_type.split(";")[0].strip().lower()
    if "json" not in mime:
        return f"Unexpected Content-Type: {content_type} (expected JSON)"
    return None


async def check_llm_connectivity(config_path: Path) -> CheckResult:
    """Test LLM API connectivity with a lightweight GET /models request.

    Uses ``httpx`` directly so we can validate the ``Content-Type`` header
    before attempting to parse the body.  A misconfigured reverse proxy
    returning HTML will produce a clear error instead of a confusing
    ``json.JSONDecodeError``.
    """
    import httpx

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

    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            headers={"Authorization": f"Bearer {api_key}"},
        ) as http:
            response = await http.get(f"{base_url.rstrip('/')}/models")

        # Validate Content-Type before any parsing
        ct_error = _validate_json_content_type(response.headers.get("content-type"))
        if ct_error:
            return CheckResult(
                name="LLM connectivity",
                passed=False,
                message=ct_error,
                details={
                    "status_code": response.status_code,
                    "url": str(response.url),
                },
            )

        if response.status_code >= 400:
            error_msg = response.text[:200].lower()
            if (
                response.status_code == 401
                or "unauthorized" in error_msg
                or "invalid" in error_msg
            ):
                return CheckResult(
                    name="LLM connectivity",
                    passed=False,
                    message="Invalid API credentials",
                )
            return CheckResult(
                name="LLM connectivity",
                passed=False,
                message=f"HTTP {response.status_code}",
            )

        latency_ms = (time.perf_counter() - start) * 1000
        return CheckResult(
            name="LLM connectivity",
            passed=True,
            message="LLM API reachable",
            details={"latency_ms": round(latency_ms, 2)},
        )
    except httpx.TimeoutException:
        latency_ms = (time.perf_counter() - start) * 1000
        return CheckResult(
            name="LLM connectivity",
            passed=False,
            message="LLM API timeout after 10s",
            details={"latency_ms": round(latency_ms, 2)},
        )
    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        return CheckResult(
            name="LLM connectivity",
            passed=False,
            message=f"Failed: {type(exc).__name__}",
        )


async def check_embedding_model(config_path: Path) -> CheckResult:
    """Test embedding API reachability with a single embedding call.

    Uses the dedicated embedding base_url and api_key when configured,
    falling back to the shared LLM credentials otherwise.  Validates the
    ``Content-Type`` header on the response to detect misconfigured
    reverse proxies early.
    """
    import httpx

    try:
        from src.config import load_config

        cfg = load_config(config_path)
    except Exception as exc:
        return CheckResult(
            name="Embedding model",
            passed=False,
            message=f"Cannot load config: {exc}",
        )

    # Resolve embedding-specific credentials
    embed_base_url = cfg.llm.embedding_base_url or cfg.llm.base_url
    embed_api_key = cfg.llm.embedding_api_key or os.environ.get(
        "OPENAI_API_KEY", cfg.llm.api_key
    )

    if not embed_api_key or embed_api_key.startswith("sk-your"):
        return CheckResult(
            name="Embedding model",
            passed=False,
            message="Skipped — no valid API key for embeddings",
        )

    start = time.perf_counter()
    try:
        payload = {
            "model": cfg.llm.embedding_model,
            "input": "health",
            "encoding_format": "float",
        }
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            headers={"Authorization": f"Bearer {embed_api_key}"},
        ) as http:
            response = await http.post(
                f"{embed_base_url.rstrip('/')}/embeddings",
                json=payload,
            )

        # Validate Content-Type before any parsing
        ct_error = _validate_json_content_type(response.headers.get("content-type"))
        if ct_error:
            return CheckResult(
                name="Embedding model",
                passed=False,
                message=ct_error,
                details={
                    "status_code": response.status_code,
                    "url": str(response.url),
                },
            )

        if response.status_code >= 400:
            error_msg = response.text[:200].lower()
            if (
                response.status_code == 401
                or "unauthorized" in error_msg
                or "invalid" in error_msg
            ):
                return CheckResult(
                    name="Embedding model",
                    passed=False,
                    message="Invalid API credentials for embeddings",
                )
            return CheckResult(
                name="Embedding model",
                passed=False,
                message=f"HTTP {response.status_code}: {response.text[:100]}",
            )

        data = response.json()
        embeddings = data.get("data", [])
        if not embeddings:
            return CheckResult(
                name="Embedding model",
                passed=False,
                message="Empty response from embedding API",
            )

        dims = len(embeddings[0].get("embedding", []))
        latency_ms = (time.perf_counter() - start) * 1000
        return CheckResult(
            name="Embedding model",
            passed=True,
            message=f"Embedding API reachable (model={cfg.llm.embedding_model}, dims={dims})",
            details={"dimensions": dims, "latency_ms": round(latency_ms, 2)},
        )
    except httpx.TimeoutException:
        return CheckResult(
            name="Embedding model",
            passed=False,
            message="Embedding API timeout after 10s",
        )
    except Exception as exc:
        return CheckResult(
            name="Embedding model",
            passed=False,
            message=f"Failed: {type(exc).__name__}",
        )


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
                "hint": "Run 'python main.py diagnose --cleanup' to remove orphaned directories",
            },
        )

    return CheckResult(
        name="Orphaned workspace dirs",
        passed=True,
        message=f"All {scanned} director{'y' if scanned == 1 else 'ies'} healthy",
    )


def cleanup_orphaned_workspace_dirs() -> int:
    """Find and remove truly orphaned workspace directories after user confirmation.

    Only deletes directories where BOTH conditions hold:
      - Missing the ``.chat_id`` origin file, AND
      - No corresponding JSONL message file exists.

    Returns the number of directories removed.
    """
    from rich.prompt import Confirm

    workspace = Path(WORKSPACE_DIR)
    whatsapp_data = workspace / "whatsapp_data"

    if not whatsapp_data.exists():
        cli_output.dim("No workspace/whatsapp_data/ directory found — nothing to clean up.")
        return 0

    messages_dir = workspace / ".data" / "messages"
    orphans: list[Path] = []

    for entry in sorted(whatsapp_data.iterdir()):
        if not entry.is_dir():
            continue
        has_chat_id = (entry / ".chat_id").exists()
        has_jsonl = (messages_dir / f"{entry.name}.jsonl").exists()
        if not has_chat_id and not has_jsonl:
            orphans.append(entry)

    if not orphans:
        cli_output.success("No orphaned directories to clean up.")
        return 0

    cli_output.warning(
        f"Found {len(orphans)} orphaned director{'y' if len(orphans) == 1 else 'ies'}:"
    )
    for orphan in orphans[:20]:
        cli_output.dim(f"  • {orphan.name}")
    if len(orphans) > 20:
        cli_output.dim(f"  ... and {len(orphans) - 20} more")

    if not Confirm.ask("Delete these directories?", default=False):
        cli_output.dim("Cleanup cancelled.")
        return 0

    removed = 0
    for orphan in orphans:
        try:
            shutil.rmtree(orphan)
            removed += 1
        except OSError as exc:
            cli_output.warning(f"Failed to remove {orphan.name}: {exc}")

    cli_output.success(f"Removed {removed} orphaned director{'y' if removed == 1 else 'ies'}.")
    return removed


def check_routing_rules(config_path: Path) -> CheckResult:
    """Check that the instructions directory exists and contains routing rules."""
    workspace = Path(WORKSPACE_DIR)
    instructions_dir = workspace / "instructions"

    if not instructions_dir.is_dir():
        return CheckResult(
            name="Routing rules",
            passed=False,
            message=f"Instructions directory missing: {instructions_dir}",
            details={
                "hint": (
                    "Create workspace/instructions/ and add at least a "
                    "'chat.agent.md' with routing frontmatter. "
                    "See src/templates/instructions/ for examples."
                ),
            },
        )

    md_files = list(instructions_dir.glob("*.md"))
    if not md_files:
        return CheckResult(
            name="Routing rules",
            passed=False,
            message="No .md instruction files found",
            details={
                "hint": (
                    "Add at least a 'chat.agent.md' with YAML routing "
                    "frontmatter. See src/templates/instructions/ for examples."
                ),
            },
        )

    # Count files with valid routing frontmatter
    from src.utils.frontmatter import extract_routing_rules, parse_file

    files_with_rules = 0
    total_rules = 0
    for md_file in md_files:
        try:
            parsed = parse_file(md_file)
            rule_dicts = extract_routing_rules(parsed.metadata)
            if rule_dicts:
                files_with_rules += 1
                total_rules += len(rule_dicts)
        except Exception:
            pass

    if total_rules == 0:
        return CheckResult(
            name="Routing rules",
            passed=False,
            message=f"{len(md_files)} .md file(s) found but none have routing rules",
            details={
                "hint": (
                    "Add YAML frontmatter with a 'routing' key to at least "
                    "one .md file. See src/templates/instructions/chat.agent.md "
                    "for an example."
                ),
            },
        )

    return CheckResult(
        name="Routing rules",
        passed=True,
        message=f"{total_rules} rule(s) from {files_with_rules} file(s)",
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


def check_whatsapp_session(config_path: Path) -> CheckResult:
    """Check WhatsApp session DB exists, is valid SQLite, and not stale.

    Validates the neonize/whatsmeow session database that stores the
    device pairing credentials. A missing or corrupted DB means the bot
    cannot connect without re-scanning the QR code.
    """
    import sqlite3

    # Resolve db_path from config if possible
    db_path: Path | None = None
    try:
        from src.config import load_config

        cfg = load_config(config_path)
        db_path = Path(cfg.whatsapp.neonize.db_path)
        # db_path is relative to project root, not workspace/
    except Exception:
        # Fallback: try default location
        db_path = Path("workspace") / "whatsapp_session.db"

    if db_path is None or not db_path.exists():
        return CheckResult(
            name="WhatsApp session",
            passed=False,
            message=f"Session DB not found: {db_path}",
            details={
                "hint": (
                    "Run 'python main.py start' to create a new session "
                    "(requires QR scan from WhatsApp)"
                ),
            },
        )

    # Check file size (empty DB = broken)
    size = db_path.stat().st_size
    if size == 0:
        return CheckResult(
            name="WhatsApp session",
            passed=False,
            message="Session DB is empty (0 bytes)",
            details={"hint": "Delete the empty file and re-run start to re-pair"},
        )

    # Check it's actually readable SQLite
    # NOTE: No PRAGMA journal_mode=WAL here — this opens an external
    # WhatsApp session DB that we don't control; its journal mode is
    # managed by the WhatsApp library, not by custombot.
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            table_names = [t[0] for t in tables]
        finally:
            conn.close()
    except Exception as exc:
        return CheckResult(
            name="WhatsApp session",
            passed=False,
            message=f"Session DB corrupted: {exc}",
            details={
                "hint": (
                    "Delete the corrupted DB and re-run start to re-pair. Backup first if needed."
                ),
            },
        )

    # Check last modification time
    import datetime

    mtime = datetime.datetime.fromtimestamp(db_path.stat().st_mtime, tz=datetime.timezone.utc)
    age_hours = (time.time() - db_path.stat().st_mtime) / 3600

    details: dict[str, Any] = {
        "size_bytes": size,
        "tables": len(table_names),
        "last_modified": mtime.isoformat(),
        "age_hours": round(age_hours, 1),
    }

    # Stale warning: session not modified in 7+ days (WhatsApp may
    # have invalidated the linked device on the phone side)
    if age_hours > 168:
        return CheckResult(
            name="WhatsApp session",
            passed=True,
            message=(
                f"Session DB valid but stale ({age_hours:.0f}h since last write). "
                "WhatsApp may have invalidated this linked device."
            ),
            details={
                **details,
                "hint": (
                    "If the bot connects but receives no messages, the linked "
                    "device may have been removed from your phone. Delete the "
                    "session DB and re-scan the QR code."
                ),
            },
        )

    return CheckResult(
        name="WhatsApp session",
        passed=True,
        message=f"Valid ({size} bytes, {len(table_names)} tables, {age_hours:.1f}h old)",
        details=details,
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
        check_routing_rules,
        check_python_env,
        check_dependencies,
        check_whatsapp_session,
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
        cli_output.success(f"All {report.total} checks passed — everything looks good!")
    else:
        cli_output.warning(
            f"{report.passed}/{report.total} checks passed, {report.failed} issue(s) found"
        )
        cli_output.dim("Fix the issues above, then re-run 'python main.py diagnose' to verify.")


def run_diagnose_cli(config_path: Path, *, cleanup: bool = False) -> None:
    """Entry point for the CLI diagnose command."""
    report = asyncio.run(run_diagnose(config_path))
    print_report(report)

    if cleanup:
        orphan_check = next(
            (c for c in report.checks if c.name == "Orphaned workspace dirs"), None
        )
        if orphan_check and not orphan_check.passed:
            cleanup_orphaned_workspace_dirs()

    sys.exit(0 if report.all_passed else 1)
