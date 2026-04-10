<!-- Context: standards/code | Priority: critical | Version: 3.0 | Updated: 2026-03-25 -->
# Code Standards

## Core Principle: MINIMAL & CLEAN

**Write less code. Delete more. Simplify everything.**

```
Less code = Fewer bugs = Easier maintenance = Better software
```

---

## Rules

### 1. No Bloat
- Every line must earn its place
- Delete unused code immediately
- No "just in case" code
- No commented-out code

### 2. Simple Over Clever
- Obvious code > clever code
- Readability > brevity
- Explicit > implicit
- Flat > nested

### 3. One Thing Well
- One responsibility per function
- One purpose per module
- One concept per file

### 4. Small Functions
- Minimize lines per function
- Minimize parameter count
- Minimize levels of nesting
- Only one return point (optional, but often clearer)

---

## Patterns

### ✅ Good: Simple & Clear

```python
def get_user_name(user: dict) -> str:
    return user.get("name", "Unknown")

def format_price(amount: float) -> str:
    return f"${amount:.2f}"
```

### ❌ Bad: Complex & Nested

```python
def process_user(user, options, config, logger):
    if user:
        if options.get("validate"):
            if config.get("strict"):
                if logger:
                    logger.info("validating")
                    # 50 more lines...
```

---

## Structure

```
module/
├── __init__.py    # Public interface only
├── core.py        # Core logic (pure functions)
└── _util.py       # Private helpers (if needed)
```

**Keep it flat. Avoid deep nesting.**

---

## Naming

| Type | Convention | Example |
|------|------------|---------|
| Functions | `verb_noun` | `get_user`, `save_file` |
| Variables | `descriptive` | `user_count`, not `uc` |
| Constants | `UPPER` | `MAX_RETRIES` |
| Private | `_prefix` | `_internal_helper` |

---

## Function Rules

```python
# ✅ Good: Small, focused, pure
async def fetch_url(url: str) -> dict:
    response = await http.get(url)
    return {"status": response.status, "data": response.text}

# ❌ Bad: Does too much, has side effects
async def fetch_and_parse_and_save_and_notify(url, db, notifier, logger):
    # 100 lines of mixed concerns...
```

---

## Module Rules

1. **< 200 lines** per file (split if larger)
2. **< 10 functions** per module (refactor if more)
3. **Clear imports** at top (stdlib → third-party → local)
4. **Docstring** only for public API

---

## Refactoring Triggers

Refactor when you see:
- Function > 20 lines
- More than 3 parameters
- Nested ifs (use early returns)
- Repeated code (extract function)
- Comment explaining "what" (code should be self-explanatory)

---

## Summary

| Do | Don't |
|----|-------|
| Write simple code | Write clever code |
| Keep functions small | Let functions grow |
| Delete unused code | Keep "just in case" |
| Use early returns | Nest deeply |
| One thing per function | Multiple responsibilities |

**Golden Rule: If you can delete it, delete it.**
