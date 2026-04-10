<!-- Context: ui/scrollytelling-headphone | Priority: high | Version: 2.0 | Updated: 2026-02-15 -->

# Example: Scrollytelling Headphone Animation

**Purpose**: Complete working Next.js 14 scroll-linked image sequence using Framer Motion + Canvas
**Tech**: App Router + Framer Motion + Canvas + Tailwind

---

## File Structure

```
app/page.tsx                          # <HeadphoneScroll />
app/components/HeadphoneScroll.tsx     # Main component
app/globals.css                       # bg-[#050505], Inter font
public/frames/frame_0001-0120.webp    # 120 WebP frames
```

---

## HeadphoneScroll.tsx (Core Logic)

```tsx
'use client'
import { useEffect, useRef, useState } from 'react'
import { motion, useScroll, useTransform } from 'framer-motion'

const FRAME_COUNT = 120

export default function HeadphoneScroll() {
  const containerRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [images, setImages] = useState<HTMLImageElement[]>([])
  const [loading, setLoading] = useState(true)
  const [currentFrame, setCurrentFrame] = useState(0)

  const { scrollYProgress } = useScroll({ target: containerRef, offset: ['start start', 'end end'] })
  const frameIndex = useTransform(scrollYProgress, [0, 1], [0, FRAME_COUNT - 1])

  useEffect(() => frameIndex.on('change', v => setCurrentFrame(Math.round(v))), [frameIndex])

  // Preload all images
  useEffect(() => {
    Promise.all(Array.from({ length: FRAME_COUNT }, (_, i) =>
      new Promise<HTMLImageElement>(resolve => {
        const img = new Image()
        img.src = `/frames/frame_${String(i+1).padStart(4,'0')}.webp`
        img.onload = () => resolve(img)
      })
    )).then(loaded => { setImages(loaded); setLoading(false) })
  }, [])

  // Render frame to canvas
  useEffect(() => {
    if (!canvasRef.current || !images.length) return
    const ctx = canvasRef.current.getContext('2d')!
    const img = images[currentFrame]
    canvasRef.current.width = window.innerWidth
    canvasRef.current.height = window.innerHeight
    const scale = Math.min(canvasRef.current.width / img.width, canvasRef.current.height / img.height)
    ctx.drawImage(img, (canvasRef.current.width - img.width*scale)/2, (canvasRef.current.height - img.height*scale)/2, img.width*scale, img.height*scale)
  }, [currentFrame, images])

  // Text opacity transforms
  const title = useTransform(scrollYProgress, [0, 0.1, 0.2], [1, 1, 0])
  const text1 = useTransform(scrollYProgress, [0.25, 0.3, 0.4], [0, 1, 0])
  const cta = useTransform(scrollYProgress, [0.85, 0.9, 1], [0, 1, 1])

  if (loading) return <div className="fixed inset-0 flex items-center justify-center bg-[#050505]">
    <div className="h-12 w-12 animate-spin rounded-full border-4 border-white/20 border-t-white" />
  </div>

  return (
    <div ref={containerRef} className="relative h-[400vh]">
      <canvas ref={canvasRef} className="sticky top-0 h-screen w-full" style={{ willChange: 'transform' }} />
      <motion.div style={{ opacity: title }} className="pointer-events-none fixed inset-0 flex items-center justify-center">
        <h1 className="text-7xl font-bold text-white/90">Zenith X</h1>
      </motion.div>
      <motion.div style={{ opacity: text1 }} className="pointer-events-none fixed inset-y-0 left-20 flex items-center">
        <p className="text-4xl font-bold text-white/90">Precision Engineering.</p>
      </motion.div>
      <motion.div style={{ opacity: cta }} className="pointer-events-none fixed inset-0 flex items-center justify-center">
        <button className="mt-8 rounded-full bg-white px-8 py-3 text-lg font-semibold text-black">Pre-Order Now</button>
      </motion.div>
    </div>
  )
}
```

---

## Customization

| What | Where |
|------|-------|
| Frame count | `FRAME_COUNT` constant |
| Scroll length | `h-[400vh]` → `h-[300vh]`/`h-[500vh]` |
| Text timing | Transform ranges `[0.25, 0.3, 0.4]` |
| Background color | `bg-[#050505]` → match your images |

## Related

- `guides/building-scrollytelling-pages.md` — Step-by-step guide
- `concepts/scroll-linked-animations.md` — Technique overview
