# Brag Plan: Feluda Care Agent

## What is this app?
Feluda turns a customer's website change request into atomic builder jobs, then independently verifies the live site (DOM checks + SAM 3.1 crops + a Muse Spark visual judge) before anything is allowed to be called "done".

## The angle
Every web-care team knows the lie: "Done!" — but the footer still has the old number. Feluda is the detective that catches it. The video follows one real demo request (Bright Smile Dental's phone change, from `scripts/live_demo.py`) and shows the exact moment Feluda fails a half-finished change that a quick glance would pass.

## Hook (first 2-3 seconds)
A builder's chat bubble types out: "Phone number updated. Marking done ✓". Beat. A crisp headline lands below: **"Feluda checks."** The tension (is it actually done?) earns the rest.

## Key moments (the middle)
- Priya's request card ("our clinic phone number has changed to (555) 201-9900 … top of the website and in the footer") splits into two job cards: header + footer. Label: "Planned by Muse Spark".
- Cursor clicks the real **Mark done** button → "Verifying — capturing desktop + mobile".
- Verdicts arrive one by one: Header **passed** (crop shows (555) 201-9900); Footer **failed** — "Footer still shows (555) 201-4410". The fail is the money shot.
- Closure blocked → footer fixed → "Final consistency gate: closed".

## Outro / punchline
"Done means verified." Feluda wordmark, "Built on Meta Muse Spark + SAM 3.1".

## User flow worth showing
Request in → planner splits into jobs → builder marks done → Feluda's verdict (pass / fail with evidence) → closure gate.

## Tone
- Preset: polished (with app-store feature-card clarity)
- Creative direction: calm, confident product film in the dashboard's own Meta design language
- Interpretation: clean white canvas, Meta blue accents, cards that slide in and hold; restraint everywhere except the one red FAIL moment.

## Format: landscape — 1920x1080
## Duration: 22s

## Visual identity (from the project — feluda/templates/base.html)
- Background: #ffffff (canvas), #f1f4f7 (surface-soft)
- Accent: #0064e0 (primary), #0457cb (primary-deep)
- Text: #0a1317 (ink-deep), #5d6c7b (steel)
- Success #31a24c / #24823c, critical #e41e3f / #c4122f
- Display font: Plus Jakarta Sans (800 brand, 500 headings)
- Body font: Plus Jakarta Sans; mono JetBrains Mono
- Strongest visual element: rounded 24px panels, pill badges (passed / failed), the dark round "F" mark, the job "Mark done" big button.

All names/data are the bundled fictional demo (Bright Smile Dental, Priya, 555 numbers). No real customer data.

## Share copy (draft)
"Done" isn't done until Feluda says so — it verifies every website change on the live site and catches the footer you forgot. Built on Meta Muse Spark + SAM 3.1.

## Audio direction
- Role: warm, steady bed with sparse professional accents
- Music: happy-beats-business-moves-vol-12 (steady, clean; polished)
- Music treatment: 0.32 volume, 0.6s fade-in, 1.2s fade-out at end
- Music cue guidance: bundled preset (109.96 BPM). Strong cues: 8.74s (Mark done click / verifying), 13.11s (FAIL verdict), 18.56s (outro wordmark). Beat grid ~0.55s; sequential job cards use every other beat.
- Audio-reactive treatment: subtle; soft blue glow behind the main panel breathes with RMS. No waveforms.
- SFX posture: sparse, motion-matched
- Audio-coupled moments: typed hook (soft keypresses), job cards (card-slide), cursor click (click), pass (soft drop), fail (error buzz, quiet), closed (bell), outro (soft impact)
- Restraint rule: no SFX stacking; nothing louder than the FAIL moment except the final bell.

## Storyboard

### Scene 1 — Hook — 3.4s
Chat bubble from "Builder" types "Phone number updated. Marking done ✓". Headline "Feluda checks." lands below at ~2.0s and holds.
Sequential/interaction: yes — typed text
Audio intent: intrigue
Audio-coupled idea: soft keypresses on typing; soft impact on headline
Transition mood: clean → Scene 2

### Scene 2 — Request becomes jobs — 4.6s (3.4–8.0)
Request card (Priya, Bright Smile Dental, verbatim request text trimmed). Below, two job cards slide in one by one: "Update phone in header" (banner) / "Update phone in footer" (contentinfo), each "(555) 201-4410 → (555) 201-9900". Eyebrow: "Planned by Muse Spark".
Sequential/interaction: yes — 2 job cards on every other beat, then hold
Audio-coupled idea: card-slide per card
Transition mood: slide → Scene 3

### Scene 3 — Mark done → verdict — 6.0s (8.0–14.0)
Job page with "Mark done" button; cursor clicks at ~8.7s (beat-locked 8.74). Badge flips to "Verifying" with "Capturing desktop + mobile". Two verdict rows: Header **passed** with after-crop "(555) 201-9900" (~10.9s); Footer **failed** at 13.11s (beat-locked) — "Footer still shows (555) 201-4410". Hold.
Sequential/interaction: yes — simulated click; verdicts one at a time
Audio-coupled idea: click; soft drop on pass; quiet error on fail
Transition mood: clean → Scene 4

### Scene 4 — Blocked, fixed, closed — 4.4s (14.0–18.4)
"Closure blocked — 1 job failed" banner → footer re-verified passed → "Final consistency gate · closed" with a drafted done note line: "Your new number (555) 201-9900 is live in the header and footer."
Sequential/interaction: yes — status flip
Audio-coupled idea: bell on closed
Transition mood: soft → Scene 5

### Scene 5 — Outro — 3.6s (18.4–22.0)
Dark "F" mark + "Feluda" wordmark lands at 18.56s (beat-locked). "Done means verified." Small line: "Built on Meta Muse Spark · SAM 3.1".
Audio-coupled idea: soft impact on wordmark, music fades out
Transition mood: end

**Music mood for this video:** steady, upbeat-calm
**Audio summary:** a clean bed with typing, card, click, a single quiet fail buzz and a closing bell.

## Voiceover script
Kokoro `af_heart`, speed 1.08, one clip per scene (`composition/assets/vo/`). Music ducks to 0.14 under narration, lifts to 0.26 in the gaps. Scenes were retimed to the voice (total 24.8s).
1. (0.3s) "Every builder says it. Done."
2. (3.5s) "Feluda splits each request into precise jobs, one for every place the change must land."
3. (9.35s) "Mark done, and Feluda checks the live site, on desktop and mobile. The header's right. The footer? Still the old number."
4. (16.75s) "Nothing closes until every job passes."
5. (21.5s) "Feluda. Done means verified."
