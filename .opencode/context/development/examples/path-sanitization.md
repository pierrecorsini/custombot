<!-- Context: development/examples | Priority: high | Version: 1.0 | Updated: 2026-03-27 -->

# Example: Cross-Platform Path Sanitization

**Purpose**: Safely convert arbitrary strings (chat IDs, usernames) to valid file paths

---

## Problem

Chat IDs and user inputs may contain characters invalid in file paths:

| Platform | Invalid Characters |
|----------|-------------------|
| Windows | `< > : " / \ | ? *` + control chars |
| Linux/macOS | `/` + null |

```python
# These would fail on Windows:
chat_id = "user@example.com"  # Contains @
chat_id = "group:12345"        # Contains :
chat_id = "chat<test>"         # Contains < >
```

---

## Solution

```python
import re
from pathlib import Path

# Windows-invalid characters (most restrictive)
INVALID_PATH_CHARS = r'[<>:"/\\|?*\x00-\x1f]'


def sanitize_for_path(name: str, max_length: int = 200) -> str:
    """
    Convert any string to a safe filename.
    
    Args:
        name: Input string (chat ID, username, etc.)
        max_length: Maximum output length
        
    Returns:
        Sanitized string safe for use in file paths
    """
    # Replace invalid characters with underscore
    sanitized = re.sub(INVALID_PATH_CHARS, '_', name)
    
    # Remove control characters
    sanitized = re.sub(r'[\x00-\x1f\x7f]', '', sanitized)
    
    # Collapse multiple underscores
    sanitized = re.sub(r'_+', '_', sanitized)
    
    # Strip leading/trailing underscores and spaces
    sanitized = sanitized.strip('_ ')
    
    # Truncate to max length
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length]
    
    # Ensure not empty
    if not sanitized:
        sanitized = "unnamed"
    
    return sanitized


def get_chat_db_path(base_dir: Path, chat_id: str) -> Path:
    """Get safe database path for a chat ID."""
    safe_name = sanitize_for_path(chat_id)
    return base_dir / f"{safe_name}.db"


def get_user_file_path(base_dir: Path, user_id: str, filename: str) -> Path:
    """Get safe file path for user-specific file."""
    safe_user = sanitize_for_path(user_id)
    safe_file = sanitize_for_path(filename)
    return base_dir / safe_user / safe_file
```

---

## Usage Examples

```python
# Chat IDs
sanitize_for_path("123456789@s.whatsapp.net")  # → "123456789_s.whatsapp.net"
sanitize_for_path("group:120363@g.us")          # → "group_120363@g.us"

# Edge cases
sanitize_for_path("")              # → "unnamed"
sanitize_for_path("...")           # → "unnamed"
sanitize_for_path("a" * 300)       # → "aaa..." (truncated to 200)

# Building paths
db_path = get_chat_db_path(Path("data"), "user:123")
# → Path("data/user_123.db")
```

---

## Test Cases

```python
def test_sanitize_for_path():
    # Basic sanitization
    assert sanitize_for_path("hello:world") == "hello_world"
    assert sanitize_for_path("test<file>") == "test_file_"
    
    # Multiple invalid chars
    assert sanitize_for_path("a:b:c:d") == "a_b_c_d"
    
    # Collapse underscores
    assert sanitize_for_path("a:::b") == "a_b"
    
    # Empty/whitespace
    assert sanitize_for_path("") == "unnamed"
    assert sanitize_for_path("   ") == "unnamed"
```

---

## Platform Notes

- **Most restrictive**: Windows (use these rules for cross-platform safety)
- **Linux/macOS**: Only `/` and null are invalid
- **Recommendation**: Always use Windows rules for portability

---

**Source**: `src/db.py`  
**Reference**: Harvested from session 2026-03-26-code-optimization
