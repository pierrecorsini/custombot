<!-- Context: ui/web/animation-advanced | Priority: medium | Version: 2.0 | Updated: 2026-02-15 -->

# Advanced Animation Patterns

**Core Idea**: Recipes for page transitions, micro-interactions, and accessibility. Keep animations under 400ms, use transform/opacity for 60fps, respect `prefers-reduced-motion`.

---

## Page Transitions

```css
.page-exit  { animation: fadeOut 200ms ease-in; }
.page-enter { animation: fadeIn 300ms ease-out; }
```

Micro-syntax: `pageExit: 200ms ease-in [α1→0]` | `pageEnter: 300ms ease-out [α0→1]`

## Micro-Interactions

```css
/* Link underline slide */
.link::after { width: 0; transition: width 250ms ease-out; }
.link:hover::after { width: 100%; }

/* Toggle switch */
.toggle-switch .thumb { transition: transform 200ms ease-out; }
.toggle-switch.on .thumb { transform: translateX(20px); }
```

## Chat UI Animation System (Complete)

```
userMsg:   400ms ease-out [Y+20→0, S0.9→1]
aiMsg:     600ms bounce   [Y+15→0, S0.95→1] +200ms
typing:    1400ms ∞       [Y±8, α0.4→1] stagger+200ms
sidebar:   350ms ease-out [X-280→0, α0→1]
sendBtn:   150ms          [S1→0.95→1] press
error:     400ms          [X±5] shake
success:   600ms bounce   [S0→1.2→1, R360°]
skeleton:  2000ms ∞       [bg: muted↔accent]
```

---

## Best Practices

✅ Under 400ms | Use transform+opacity | ease-out for enter, ease-in for exit | Stagger lists 50-100ms
❌ Animate width/height | Over 800ms | No purpose | Ignore reduced-motion

## Accessibility (REQUIRED)

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    transition-duration: 0.01ms !important;
  }
}
```

## References

- [CSS Easing Functions](https://easings.net/)
- [Animation Performance](https://web.dev/animations-guide/)

## Related

- `animation-basics.md` — Fundamentals
- `animation-components.md` — UI patterns
