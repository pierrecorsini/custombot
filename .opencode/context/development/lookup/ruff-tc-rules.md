<!-- Context: development/lookup/ruff-tc-rules | Priority: medium | Version: 1.0 | Updated: 2026-05-04 -->

# Ruff: flake8-type-checking (TC) Rules

**Core Idea**: Ruff's TC ruleset (TC001–TC005) enforces that imports used only for type annotations are moved into `if TYPE_CHECKING:` blocks, reducing runtime overhead and breaking import cycles. Original plugin used TCH prefix; Ruff renamed to TC.

**Key Points**:
- TC001/TC002/TC003: Flag first-party/third-party/stdlib imports only used in annotations
- TC004: Catches runtime-required imports wrongly placed in TYPE_CHECKING blocks
- TC005: Removes empty TYPE_CHECKING blocks
- `strict = true` enforces TC001-003 even when a runtime import of the same symbol exists
- `runtime-evaluated-base-classes` exempts Pydantic/SQLAlchemy models (annotations evaluated at runtime)

## Configuration

```toml
[tool.ruff.lint]
select = ["TC"]

[tool.ruff.lint.flake8-type-checking]
strict = false                                    # Enforce even w/ runtime import
quote-annotations = false                         # Quote annotations to enable TYPE_CHECKING
exempt-modules = ["typing", "typing_extensions"]  # Modules exempt from TC001-003
runtime-evaluated-base-classes = []               # e.g. "pydantic.BaseModel"
runtime-evaluated-decorators = []                 # e.g. "dataclasses.dataclass"
```

## TC001–TC003: Move Type-Only Imports

```python
# Bad (TC001/002/003)
from pathlib import Path
def func(path: Path) -> str: ...

# Good
from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from pathlib import Path
def func(path: Path) -> str: ...
```

## TC004: Runtime Import in TYPE_CHECKING

```python
# Bad — NameError at runtime
if TYPE_CHECKING:
    import foo
def bar() -> None:
    foo.bar()  # NameError

# Good
import foo
def bar() -> None:
    foo.bar()
```

## Rule-to-Option Mapping

| Rule | Affected by |
|------|------------|
| TC001-TC003 | strict, quote-annotations, runtime-evaluated-*, exempt-modules |
| TC004 | quote-annotations |
| TC005 | (none) |

## Reference
- Settings: https://docs.astral.sh/ruff/settings/#lint_flake8-type-checking
- Rules: https://docs.astral.sh/ruff/rules/#flake8-type-checking-tc

## 📂 Codebase References
- Config: `pyproject.toml` — `[tool.ruff.lint]` select + flake8-type-checking table
- Module pattern: Every `src/` file uses `from __future__ import annotations` + `if TYPE_CHECKING:` guard
