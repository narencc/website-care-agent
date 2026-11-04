# Hyperframes Composition Brief: Feluda Care Agent

## Objective
Short launch-style brag video for the Feluda Care Agent (Meta hackathon submission).

## Output
- Composition: `brag-output/composition/` · Render: `brag-output/brag.mp4`
- Landscape 1920x1080 · 24.8s (voiced; no-voice cut kept as brag-no-voice.mp4 at 23s)

## Source material
- Project root: repo root. Read: README.md, feluda/templates/base.html (tokens), job.html (Mark done, verdict rows), report.html, scripts/live_demo.py (demo story), demo_site/app.py (fictional Bright Smile Dental data).
- Verbatim/near-verbatim copy: "Mark done", "Verifying", "Passed"/"Failed", "Final consistency gate", request text trimmed from live_demo.py.
- All names and numbers are the bundled fictional demo data.

## Creative direction
- Tone: polished with app-store card clarity; calm Meta-design product film.
- Hook: builder types "Phone number updated. Marking done ✓" → "Feluda checks."
- Outro: Feluda wordmark · "Done means verified." · "Built on Meta Muse Spark · SAM 3.1".

## Visual identity
White canvas, #0064e0 primary, #0a1317 ink, #5d6c7b steel, success #24823c, critical #c4122f; Plus Jakarta Sans + JetBrains Mono (local woff2).

## Storyboard
1. Hook 0–3.4s · 2. Request → 2 jobs 3.4–7.8s · 3. Mark done → header passed / footer failed 7.8–14.8s · 4. Blocked → fixed → closed 14.8–19.5s · 5. Outro 19.5–23s

## Audio
- Music: happy-beats-business-moves-vol-12 (bundled preset, 109.96 BPM), volume 0.32 with fade-in/out via volume automation.
- Beat locks: Mark done click 8.74s, outro wordmark 19.66s. Beat grid: job cards 4.91s / 5.34s.
- Audio-reactive: background blue glow opacity/scale follows pre-extracted low-band energy (15fps).
- SFX: keypresses on typing, soft impact on hook, card slides, click, soft drop on pass, quiet error on fail, bell on closed, soft impact on logo.
