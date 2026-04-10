<!-- Context: visual-development | Priority: high | Version: 1.0 | Updated: 2025-01-27 -->
# Visual Development Context

**Purpose**: Visual content creation, UI design, image generation, and diagram creation

---

## Quick Routes

| Task Type | Context File | Subagent/Tool |
|-----------|-------------|---------------|
| **Generate image/diagram** | This file | Image Specialist (tool:gemini) |
| **Edit existing image** | This file | Image Specialist (tool:gemini) |
| **UI mockup (static)** | This file | Image Specialist (tool:gemini) |
| **Interactive UI design** | `workflows/design-iteration-overview.md` | - |
| **Design system** | `ui/web/design-systems.md` | - |
| **UI standards** | `ui/web/ui-styling-standards.md` | - |
| **Animation patterns** | `ui/web/animation-basics.md` | - |

---

## Image Specialist Capabilities

- **Generate images** from text descriptions (illustrations, graphics, icons)
- **Edit existing images** (modify, enhance, transform)
- **Analyze images** (describe content, extract information)
- **Create diagrams** (architecture, flowcharts, system visualizations)
- **Design mockups** (UI mockups, wireframes, design concepts)

**Keywords**: "create image", "diagram", "mockup", "graphic", "illustration", "edit image", "screenshot"

---

## Minimal Invocation

```javascript
task(
  subagent_type="Image Specialist",
  description="Generate architecture diagram",
  prompt="Context: .opencode/context/core/visual-development.md
          Task: [Visual requirement]
          Style: [modern/minimalist/professional]
          Dimensions: [WxH], Colors: [hex codes], Format: [PNG/JPG]
          Output: [Save location]"
)
```

---

## Decision Tree

| Need | Use |
|------|-----|
| Interactive dashboard | `design-iteration-overview.md` |
| Dashboard mockup (static) | Image Specialist |
| Responsive landing page | `design-iteration-overview.md` |
| Architecture diagram | Image Specialist |
| Social media graphic | Image Specialist |
| Working HTML prototype | `design-iteration-overview.md` |

**Rule**: Interactive/responsive HTML/CSS → design-iteration. Static visual asset → Image Specialist.

---

## Tools & Dependencies

- **tool:gemini** (Gemini Nano Banana AI) — auto-included in Developer profile
- Requires `GEMINI_API_KEY` in `.env`
- Capabilities: Text-to-Image, Image-to-Image, Image Analysis, PNG/JPG/WebP, up to 2048x2048px

---

## Best Practices

✅ **Do**: Be specific about dimensions/format, describe visual style clearly, specify colors with hex codes, include key elements, mention use case

❌ **Don't**: Use vague descriptions ("make it nice"), forget dimensions, skip color specs, omit output location

---

## Troubleshooting

- **Doesn't match expectations**: Refine prompt with more detail, provide reference examples
- **Low quality**: Request higher resolution, specify quality in prompt
- **Wrong colors**: Provide exact hex codes, reference brand guidelines

---

## Related Context

- **UI Design Workflow**: `.opencode/context/core/workflows/design-iteration-overview.md`
- **Design Systems**: `.opencode/context/ui/web/design-systems.md`
- **UI Styling Standards**: `.opencode/context/ui/web/ui-styling-standards.md`
- **Animation Patterns**: `.opencode/context/ui/web/animation-basics.md`
- **Subagent Invocation**: `.opencode/context/openagents-repo/guides/subagent-invocation.md`
