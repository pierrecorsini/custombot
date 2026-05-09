<!-- Context: project/guides/cli-reference | Priority: medium | Version: 1.0 | Updated: 2026-04-04 -->

# Guide: CLI Reference

**Purpose**: Complete command-line interface for custombot.

**Source**: `README.md` — CLI Reference section

---

## Commands

### `python main.py start`
Start the bot. First run displays a QR code — scan with WhatsApp (Settings → Linked Devices → Link a Device). Session is saved for future auto-reconnect.

| Flag | Description |
|------|-------------|
| `--config <path>` | Use a custom config file (default: `workspace/config.json`) |
| `--health-port <port>` | Enable HTTP health check endpoint on specified port |
| `--log-llm` | Log LLM requests/responses to files for debugging |
| `--safe` | Prompt Y/N before every outgoing message (testing mode) |

### `python main.py options`
Open interactive TUI for editing configuration (LLM provider, API key, model, WhatsApp settings, etc.).

---

## Global Options (before command)

| Flag | Description |
|------|-------------|
| `-v` | Verbose / debug mode (shorthand) |
| `--verbosity quiet` | Warnings only |
| `--verbosity verbose` | Full debug output |
| `--log-format json` | Structured JSON logs |
| `--version` | Show version |

---

## Usage Examples

```bash
# Standard start
python main.py start

# Debug mode with LLM logging
python main.py -v start --log-llm

# Safe mode for testing (confirm each message)
python main.py start --safe

# Custom config with health endpoint
python main.py start --config my_config.json --health-port 8080

# JSON structured logging
python main.py --log-format json start

# Edit configuration
python main.py options
```

---

## Configuration Alternative

Instead of the TUI, copy and edit manually:
```bash
cp config.example.json workspace/config.json
# Edit config.json with your preferred editor
```

---

## Codebase

- `main.py` — CLI entry point (Click commands)
- `src/options_tui.py` — Configuration editor TUI (Textual)
- `src/cli_output.py` — Terminal output formatting
- `src/config/` — Dataclass config + JSON load/save

## Related

- `lookup/configuration.md` — Full config.json schema
- `lookup/workspace-structure.md` — Where config and session files live
