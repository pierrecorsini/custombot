<!-- Context: ui/building-scrollytelling-pages | Priority: high | Version: 2.0 | Updated: 2026-02-15 -->

# Guide: Building Scrollytelling Pages

**Core Idea**: Scroll-linked image sequence animation using Canvas + Framer Motion's `useScroll`/`useTransform`. 120+ WebP frames, sticky canvas, text overlays at scroll positions.

**Tech**: Next.js 14+ App Router + Framer Motion + Canvas + Tailwind

---

## Key Patterns

```tsx
// Scroll tracking → frame index
const { scrollYProgress } = useScroll({ target: containerRef })
const frameIndex = useTransform(scrollYProgress, [0, 1], [0, FRAME_COUNT - 1])

// Preload images
const promises = Array.from({ length: 120 }, (_, i) => {
  return new Promise(resolve => {
    const img = new Image()
    img.src = `/frames/frame_${String(i+1).padStart(4,'0')}.webp`
    img.onload = () => resolve(img)
  })
})

// Draw to canvas on scroll update
useEffect(() => {
  const ctx = canvas.getContext('2d')
  ctx.drawImage(images[currentFrame], x, y, w, h)
}, [currentFrame])

// Text overlays at scroll positions
<motion.div style={{ opacity: useTransform(scrollYProgress, [0.25,0.30,0.35], [0,1,0]) }}>
  Precision Engineering.
</motion.div>
```

---

## Implementation Steps

1. **Generate frames**: AI image tools → video → `ffmpeg -i anim.mp4 -vf fps=30 frame_%04d.webp`
2. **Structure**: `app/page.tsx` + `components/ScrollAnim.tsx` + `public/frames/`
3. **Container**: `h-[400vh]` for long scroll, canvas with `sticky top-0`
4. **Preload**: All frames via `Promise.all` before starting
5. **Canvas render**: Scale + center current frame on scroll update
6. **Text overlays**: Fade in/out at specific scroll positions
7. **Background match**: MUST match frame background exactly (use eyedropper)

---

## Common Issues

| Issue | Fix |
|-------|-----|
| Images not loading | Check paths (case-sensitive), verify in `/public/frames/` |
| Stuttering | Ensure preloaded, use WebP not PNG |
| Visible edges | Background must match exactly — eyedropper, not guessing |
| Mobile slow | Reduce frame count, use `requestAnimationFrame` |

## References

- [Framer Motion useScroll](https://www.framer.com/motion/use-scroll/)
- [Next.js App Router](https://nextjs.org/docs/app)

## Related

- `concepts/scroll-linked-animations.md` — Technique overview
- `examples/scrollytelling-headphone.md` — Full code example
