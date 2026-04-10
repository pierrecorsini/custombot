<!-- Context: ui/terminal/navigation | Priority: critical | Version: 2.0 | Updated: 2026-03-25 -->

# Terminal UI Context

**Purpose**: Terminal UI patterns, CLI animations, and Rich library usage

---

## Structure

```
ui/terminal/
├── navigation.md           # This file
├── concepts/
│   └── rich-platform.md    # Cross-platform TTY handling
├── examples/
│   ├── rich-console.md     # Console setup & integration
│   ├── rich-panels-tables.md
│   ├── rich-progress.md    # Progress bars
│   └── rich-spinners.md    # Status & spinners
└── lookup/
    ├── rich-markup.md      # Markup syntax
    ├── rich-colors.md      # Color options
    └── rich-spinner-types.md
```

---

## Quick Reference

| Need | File |
|------|------|
| Console setup | `examples/rich-console.md` |
| Progress bars | `examples/rich-progress.md` |
| Spinners/status | `examples/rich-spinners.md` |
| Panels/tables | `examples/rich-panels-tables.md` |
| Markup syntax | `lookup/rich-markup.md` |
| Color options | `lookup/rich-colors.md` |
| Cross-platform | `concepts/rich-platform.md` |

---

## Rich Library Overview

**Install**: `pip install rich`

**Core Components**:
- `Console` - Main output class
- `Progress` - Progress bars
- `Table` / `Panel` - Layout
- `console.status()` - Spinners

**Docs**: https://rich.readthedocs.io/

---

## Related

- `ui/web/` - Web UI patterns
- `development/` - General development
