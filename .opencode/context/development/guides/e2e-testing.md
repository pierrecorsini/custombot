<!-- Context: development/guides | Priority: medium | Version: 1.0 | Updated: 2026-03-20 -->

# Guide: E2E Testing with pytest

**Purpose**: End-to-end tests that exercise the CLI interface with mocked dependencies.

---

## Core Idea

E2E tests validate the full application flow through CLI commands, using mocked LLM and external services. Tests run offline and follow AAA pattern.

---

## Test Structure

```
tests/
├── conftest.py          # Shared fixtures
├── e2e/
│   ├── test_chat_flow.py
│   ├── test_status_command.py
│   ├── test_onboard_command.py
│   └── test_whatsapp_qr.py
└── test_routing.py      # Unit tests
```

---

## Key Patterns

### 1. Mock External Services

```python
with patch("llm.AsyncOpenAI") as mock_openai:
    mock_client = AsyncMock()
    mock_openai.return_value = mock_client
    mock_client.chat.completions.create = AsyncMock(
        return_value=MockChatCompletion()
    )
```

### 2. AAA Pattern

```python
def test_chat_command():
    # Arrange
    config_path = tmp_path / "config.json"
    _create_test_config(config_path)
    
    # Act
    result = cli_runner.invoke(cli, ["chat", "-m", "hello"])
    
    # Assert
    assert result.exit_code == 0
```

### 3. Isolated Workspaces

```python
@pytest.fixture
def tmp_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    yield workspace
    # Cleanup automatic
```

---

## Test Categories

| Category | File | What it Tests |
|----------|------|---------------|
| Chat Flow | `test_chat_flow.py` | CLI chat command |
| Status | `test_status_command.py` | Config display |
| Onboard | `test_onboard_command.py` | Setup wizard |
| WhatsApp | `test_whatsapp_qr.py` | QR code retrieval |
| Routing | `test_routing.py` | Routing engine |

---

## Running Tests

```bash
# All tests
python -m pytest tests/ -v

# Specific file
python -m pytest tests/e2e/test_chat_flow.py -v

# With coverage
python -m pytest tests/ --cov=. --cov-report=html
```

---

## Related

- `tests/conftest.py` - Fixtures
- `pytest.ini` - Configuration
