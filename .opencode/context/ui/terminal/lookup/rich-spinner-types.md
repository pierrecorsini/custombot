<!-- Context: ui/terminal/lookup | Priority: low | Version: 1.0 | Updated: 2026-03-25 -->
# Rich Spinner Types

**Core**: Run `python -m rich.spinner` to see all. Use by name with `spinner="name"`.

## Popular Spinners

| Name | Style |
|------|-------|
| `dots` | Default dots |
| `dots12` | 12-dot circle |
| `line` | Line rotation |
| `bouncingBar` | Bouncing bar |
| `arrow` | Arrow rotation |
| `monkey` | Fun monkey |
| `hearts` | Heart animation |
| `weather` | Weather icons |
| `dqpb` | D-Q-P-B cycle |
| `aesthetic` | Aesthetic style |

## Usage

```python
with console.status("Loading...", spinner="dots12"):
    do_work()
```

## View All

```bash
python -m rich.spinner
```

**Ref**: https://rich.readthedocs.io/en/stable/console.html#status
