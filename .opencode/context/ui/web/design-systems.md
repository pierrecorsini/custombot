<!-- Context: development/design-systems | Priority: high | Version: 1.1 | Updated: 2026-04-05 -->
# Design Systems

## Quick Reference

| Aspect | Standard |
|--------|----------|
| **Color Format** | OKLCH (perceptually uniform) |
| **Theme Variables** | CSS custom properties (`--variable-name`) |
| **Font Sources** | Google Fonts |
| **Responsive** | Mobile-first, all breakpoints |

---

## Theme Patterns

### Neo-Brutalism

- **Aesthetic**: 90s web, bold borders, flat/hard shadows, high contrast
- **Use for**: Creative portfolios, playful apps, retro designs
- **Key traits**: `--radius: 0px`, hard offset shadows (`4px 4px 0px`), bold colors
- **Fonts**: DM Sans (sans), Space Mono (mono)
- **Primary**: Vibrant orange `oklch(0.6489 0.2370 26.9728)`, accent: purple `oklch(0.5635 0.2408 260.8178)`

### Modern Dark Mode (Vercel/Linear)

- **Aesthetic**: Clean, minimal, professional
- **Use for**: SaaS, developer tools, dashboards, enterprise
- **Key traits**: `--radius: 0.625rem`, soft shadows, monochromatic palette
- **Fonts**: System font stack (ui-sans-serif, system-ui, etc.)
- **Primary**: Near-black `oklch(0.2050 0 0)`, borders: `oklch(0.9220 0 0)`

> Full CSS theme templates are available in the project's theme files. Apply via `:root` CSS custom properties.

---

## Typography System

**Sans-Serif** (UI/body): Inter, Roboto, Poppins, Montserrat, DM Sans, Geist, Space Grotesk

**Monospace** (code): JetBrains Mono, Fira Code, Space Mono, Geist Mono

**Serif** (editorial): Merriweather, Playfair Display, Lora, Source Serif Pro

**Display**: Oxanium, Architects Daughter

> Load from Google Fonts. Limit to 2-3 families per project.

---

## Color System (OKLCH)

- **Format**: `oklch(L C H)` — L=lightness(0-1), C=chroma(0-0.4), H=hue(0-360)
- **Semantic naming**: `--primary`, `--destructive`, `--success` (not color names)
- **Contrast**: WCAG AA minimum (4.5:1 text, 3:1 large text)
- **Background/foreground**: Light component → dark background (and vice versa)

---

## Shadow Scale

| Token | Depth | Style |
|-------|-------|-------|
| `--shadow-2xs` | Minimal | 1-2px |
| `--shadow-sm` | Small cards | 3-4px |
| `--shadow-md` | Medium | 6-8px |
| `--shadow-lg` | Modals/dropdowns | 8-12px |
| `--shadow-xl` | Floating panels | 12-16px |

**Soft** (Modern): `0 1px 3px hsl(0 0% 0% / 0.10)` | **Hard** (Brutalism): `4px 4px 0px hsl(0 0% 0% / 1)`

---

## Spacing & Radius

**Base unit**: `--spacing: 0.25rem` (4px). Scale: 1x=4px, 2x=8px, 4x=16px, 8x=32px, 16x=64px.

| Style | Radius | Use Case |
|-------|--------|----------|
| Sharp | `0px` | Neo-brutalism |
| Subtle | `0.375rem` | Modern minimal |
| Rounded | `0.625rem` | Friendly/app |
| Pill | `9999px` | Buttons/badges |

---

## Best Practices

✅ CSS custom properties for all theme values | ✅ Semantic color names | ✅ WCAG contrast validation | ✅ Consistent spacing scale | ✅ Test light/dark modes

❌ Hardcode colors in components | ❌ Generic blue (#007bff) | ❌ Mix color formats | ❌ Skip contrast testing | ❌ More than 3 font families

---

## References

- [OKLCH Color Picker](https://oklch.com/) | [Google Fonts](https://fonts.google.com/)
- [WCAG Contrast Checker](https://webaim.org/resources/contrastchecker/) | [Tailwind Colors](https://tailwindcss.com/docs/customizing-colors)
