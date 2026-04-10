<!-- Context: development/ui-styling-standards | Priority: high | Version: 1.1 | Updated: 2026-04-05 -->
# UI Styling Standards

## Quick Reference

| Aspect | Convention |
|--------|-----------|
| **Framework** | Tailwind CSS + Flowbite (default) |
| **Approach** | Mobile-first responsive, utility-first |
| **Tailwind Loading** | `<script src="https://cdn.tailwindcss.com">` (JIT enabled) |
| **Flowbite** | CSS + JS via jsDelivr CDN |
| **Specificity** | Use `!important` for framework overrides only |
| **Colors** | Semantic names (`--primary`, `--accent`), never hardcoded |
| **Avoid** | Bootstrap blue (#007bff) unless explicitly requested |

---

## Responsive Breakpoints

| Prefix | Min-width | Use Case |
|--------|-----------|----------|
| (base) | 0px | Mobile (default) |
| `sm:` | 640px | Large phones |
| `md:` | 768px | Tablets |
| `lg:` | 1024px | Small desktops |
| `xl:` | 1280px | Desktops |
| `2xl:` | 1536px | Large screens |

**Rule**: Always mobile-first. Base styles = mobile, add `md:`, `lg:` for larger screens.

```html
<div class="flex flex-col md:flex-row">
  <div class="w-full md:w-1/2">Left</div>
  <div class="w-full md:w-1/2">Right</div>
</div>
```

**Test at**: 375px, 768px, 1024px, 1440px. Touch targets ≥ 44×44px.

---

## Color Palette Rules

- **Semantic naming**: `--primary`, `--accent`, `--destructive` — not `--blue`, `--red`
- **Contrast**: WCAG AA minimum (4.5:1 for text)
- **Consistency**: Use theme variables throughout, never hardcode hex
- **Avoid**: Generic Bootstrap blue (#007bff) unless requested

---

## CSS Specificity

- **Prefer Tailwind utilities** over custom CSS
- **Use `!important` sparingly** — only for framework/Flowbite overrides
- **Scope custom styles** to avoid conflicts with Tailwind

---

## Typography Hierarchy

| Level | Tailwind Classes |
|-------|-----------------|
| H1 | `text-4xl md:text-5xl lg:text-6xl font-bold` |
| H2 | `text-3xl md:text-4xl font-semibold` |
| H3 | `text-2xl md:text-3xl font-semibold` |
| Body | `text-base md:text-lg leading-relaxed` |
| Small | `text-sm text-gray-600` |
| Caption | `text-xs text-gray-500` |

**Readability**: Line length 60-80 chars, line height 1.5-1.75, min 16px body text.

---

## Accessibility (Key Rules)

- Use **semantic HTML** (`<header>`, `<nav>`, `<main>`, `<article>`, `<footer>`)
- Add **ARIA labels** for interactive elements (`aria-label`, `aria-labelledby`)
- Provide **visible focus states** (`focus-visible` with outline)

---

## Performance

- **Preconnect** to font sources (`fonts.googleapis.com`, `fonts.gstatic.com`)
- **Lazy-load images** (`loading="lazy"`) with responsive `srcset`
- **Inline critical CSS** for above-the-fold content

---

## Framework Alternatives

| Framework | CSS CDN |
|-----------|---------|
| Bootstrap | `cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css` |
| Bulma | `cdn.jsdelivr.net/npm/bulma@0.9.4/css/bulma.min.css` |
| Foundation | `cdn.jsdelivr.net/npm/foundation-sites@6.7.5/dist/css/foundation.min.css` |

---

## References

- [Tailwind CSS Docs](https://tailwindcss.com/docs)
- [Flowbite Components](https://flowbite.com/docs/getting-started/introduction/)
- [WCAG Guidelines](https://www.w3.org/WAI/WCAG21/quickref/)
- [MDN Web Accessibility](https://developer.mozilla.org/en-US/docs/Web/Accessibility)
