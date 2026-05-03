---
source: Context7 API + Official Docs (docs.astral.sh)
library: Ruff
package: ruff
topic: flake8-type-checking (TC/TCH) ruleset configuration
fetched: 2026-05-03T12:00:00Z
official_docs: https://docs.astral.sh/ruff/settings/#lint_flake8-type-checking
---

# Ruff: flake8-type-checking (TC/TCH) Configuration

> **Note on rule code prefixes**: The original `flake8-type-checking` plugin uses **TCH** prefixes (TCH001–TCH005). Ruff renamed these to **TC** prefixes (TC001–TC005). Both refer to the same rules. When selecting rules in Ruff config, use the **TC** prefix.

---

## 1. Configuration Options (`[tool.ruff.lint.flake8-type-checking]`)

All configuration lives under the `[tool.ruff.lint.flake8-type-checking]` TOML table (or `[lint.flake8-type-checking]` in `ruff.toml`).

### Available Options

| Option | Type | Default | Description |
|---|---|---|---|
| `exempt-modules` | `list[str]` | `["typing", "typing_extensions"]` | Modules to exempt from type-checking enforcement. Imports from these modules will not trigger TC001/TC002/TC003. |
| `quote-annotations` | `bool` | `false` | When `true`, Ruff wraps annotations in quotes if doing so enables the import to remain in a `TYPE_CHECKING` block. |
| `runtime-evaluated-base-classes` | `list[str]` | `[]` | Fully-qualified class names that require type annotations to be available at runtime. Classes inheriting from these are exempted from being moved into `TYPE_CHECKING` blocks. |
| `runtime-evaluated-decorators` | `list[str]` | `[]` | Fully-qualified decorator names that require type annotations at runtime. Classes/functions decorated with these are exempted. |
| `strict` | `bool` | `false` | Enforce TC001, TC002, TC003 rules even when valid runtime imports exist for the same symbol. |

---

## 2. `strict = true` — Detailed Behavior

When `strict = true`, Ruff enforces that **all** imports used only for type annotations must be placed inside `if TYPE_CHECKING:` blocks — **even if the same symbol is already imported at the module level for runtime use**.

Without `strict`, Ruff only flags an import if it's **exclusively** used in type annotations AND no runtime import of the same symbol exists. With `strict`, Ruff flags **any** import that _could_ be moved into a `TYPE_CHECKING` block, regardless of whether a runtime import already covers it.

This aligns with `flake8-type-checking`'s `--strict` option.

```toml
# pyproject.toml
[tool.ruff.lint.flake8-type-checking]
strict = true
```

```toml
# ruff.toml
[lint.flake8-type-checking]
strict = true
```

---

## 3. TC Rule Codes (TC001–TC005)

### TC001 — `typing-only-first-party-import`

**Added**: v0.8.0 | **Fix**: Sometimes available | **Original**: TCH001

Checks for **first-party** (your own project) imports that are only used for type annotations but aren't in a `TYPE_CHECKING` block.

**Why it's bad**: Adds runtime overhead and can create import cycles for first-party modules.

```python
# Bad
from __future__ import annotations
from . import local_module

def func(sized: local_module.Container) -> int:
    return len(sized)

# Good
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import local_module

def func(sized: local_module.Container) -> int:
    return len(sized)
```

---

### TC002 — `typing-only-third-party-import`

**Added**: v0.8.0 | **Fix**: Sometimes available | **Original**: TCH002

Checks for **third-party** imports that are only used for type annotations but aren't in a `TYPE_CHECKING` block.

**Why it's bad**: Adds runtime overhead from loading third-party packages unnecessarily.

```python
# Bad
from __future__ import annotations
import pandas as pd

def func(df: pd.DataFrame) -> int:
    return len(df)

# Good
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

def func(df: pd.DataFrame) -> int:
    return len(df)
```

---

### TC003 — `typing-only-standard-library-import`

**Added**: v0.8.0 | **Fix**: Sometimes available | **Original**: TCH003

Checks for **standard library** imports that are only used for type annotations but aren't in a `TYPE_CHECKING` block.

**Why it's bad**: Adds unnecessary runtime overhead even for stdlib imports.

```python
# Bad
from __future__ import annotations
from pathlib import Path

def func(path: Path) -> str:
    return str(path)

# Good
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

def func(path: Path) -> str:
    return str(path)
```

---

### TC004 — `runtime-import-in-type-checking-block`

**Added**: v0.8.0 | **Fix**: Sometimes available | **Original**: TCH004

Checks for imports that are **required at runtime** but are only defined inside a `TYPE_CHECKING` block.

**Why it's bad**: The `TYPE_CHECKING` block is **not executed at runtime**, so if the only definition of a symbol is in a `TYPE_CHECKING` block, it will cause a `NameError` at runtime.

```python
# Bad — NameError at runtime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import foo

def bar() -> None:
    foo.bar()  # NameError: name 'foo' is not defined

# Good — import at runtime
import foo

def bar() -> None:
    foo.bar()
```

**Options**: If `quote-annotations = true`, annotations will be wrapped in quotes if doing so enables the import to remain in the `TYPE_CHECKING` block.

---

### TC005 — `empty-type-checking-block`

**Added**: v0.8.0 | **Fix**: Always available | **Original**: TCH005

Checks for **empty** `TYPE_CHECKING` blocks.

**Why it's bad**: An empty type-checking block does nothing and should be removed to avoid confusion.

```python
# Bad
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

print("Hello, world!")

# Good
print("Hello, world!")
```

---

## 4. `runtime-evaluated-base-classes` and `runtime-evaluated-decorators` — Detailed

### `runtime-evaluated-base-classes`

**Type**: `list[str]` (fully-qualified class names)

Exempts classes that **inherit** from the specified base classes from being moved into `TYPE_CHECKING` blocks. This is critical for libraries like **Pydantic** and **SQLAlchemy** that need type annotations available at runtime for validation, serialization, and ORM mapping.

```toml
[tool.ruff.lint.flake8-type-checking]
runtime-evaluated-base-classes = [
    "pydantic.BaseModel",
    "sqlalchemy.orm.DeclarativeBase",
]
```

**How it works**: When Ruff encounters a class like `class User(BaseModel):`, and `BaseModel` is in `runtime-evaluated-base-classes`, Ruff will **not** flag the imports used in that class's annotations (e.g., the `User` class's field types), because those annotations are evaluated at runtime by Pydantic.

**Custom base classes**: You can also include your own project's base classes:
```toml
runtime-evaluated-base-classes = ["myapp.models.Base", "pydantic.BaseModel"]
```

---

### `runtime-evaluated-decorators`

**Type**: `list[str]` (fully-qualified decorator names)

Exempts classes and functions **decorated** with the specified decorators from having their type-only imports moved into `TYPE_CHECKING` blocks.

```toml
[tool.ruff.lint.flake8-type-checking]
runtime-evaluated-decorators = [
    "pydantic.validate_call",
    "attrs.define",
    "attrs.frozen",
]
```

**How it works**: When Ruff encounters a function like `@validate_call def func(x: int) -> str:`, and `validate_call` is in `runtime-evaluated-decorators`, Ruff will **not** suggest moving the type annotations into `TYPE_CHECKING`, because the decorator evaluates them at runtime.

---

## 5. `quote-annotations` — Detailed

**Type**: `bool` | **Default**: `false`

When set to `true`, Ruff will wrap type annotations in **quotes** (making them string literals) if doing so enables the corresponding import to be moved into a `TYPE_CHECKING` block.

```toml
[tool.ruff.lint.flake8-type-checking]
quote-annotations = true
```

This avoids the need for `from __future__ import annotations` in files where only specific annotations need to be deferred.

**Note**: If `lint.future-annotations = true` is also set, Ruff will prefer adding `from __future__ import annotations` instead of quoting individual annotations.

---

## 6. `exempt-modules` — Detailed

**Type**: `list[str]` | **Default**: `["typing", "typing_extensions"]`

Modules listed here are exempt from TC001/TC002/TC003 checks. By default, imports from `typing` and `typing_extensions` are exempted since they are commonly needed at runtime and are lightweight.

```toml
[tool.ruff.lint.flake8-type-checking]
exempt-modules = ["typing", "typing_extensions"]
```

---

## 7. Complete Configuration Example

```toml
[tool.ruff.lint]
select = ["TC"]  # Enable all flake8-type-checking rules (TC001-TC005)

[tool.ruff.lint.flake8-type-checking]
strict = true
quote-annotations = false
exempt-modules = ["typing", "typing_extensions"]
runtime-evaluated-base-classes = [
    "pydantic.BaseModel",
    "pydantic.v1.BaseModel",
    "sqlalchemy.orm.DeclarativeBase",
]
runtime-evaluated-decorators = [
    "pydantic.validate_call",
    "attrs.define",
    "attrs.frozen",
    "dataclasses.dataclass",
]
```

---

## Rule-to-Option Mapping

| Rule | Affected by options |
|---|---|
| TC001 | `strict`, `quote-annotations`, `runtime-evaluated-base-classes`, `runtime-evaluated-decorators`, `exempt-modules` |
| TC002 | `strict`, `quote-annotations`, `runtime-evaluated-base-classes`, `runtime-evaluated-decorators`, `exempt-modules` |
| TC003 | `strict`, `quote-annotations`, `runtime-evaluated-base-classes`, `runtime-evaluated-decorators`, `exempt-modules` |
| TC004 | `quote-annotations` |
| TC005 | (none) |

---

## Sources

- Settings: https://docs.astral.sh/ruff/settings/#lint_flake8-type-checking
- TC001: https://docs.astral.sh/ruff/rules/typing-only-first-party-import/
- TC002: https://docs.astral.sh/ruff/rules/typing-only-third-party-import/
- TC003: https://docs.astral.sh/ruff/rules/typing-only-standard-library-import/
- TC004: https://docs.astral.sh/ruff/rules/runtime-import-in-type-checking-block/
- TC005: https://docs.astral.sh/ruff/rules/empty-type-checking-block/
- Rules list: https://docs.astral.sh/ruff/rules/#flake8-type-checking-tc
