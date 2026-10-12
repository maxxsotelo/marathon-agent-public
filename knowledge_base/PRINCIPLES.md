# Training Principles Digest

The coach reads this at every planning session instead of the full library. Each principle
names its source in `knowledge_base/`, how strong the evidence is, and the rule it drives.
Decisions cite principles by ID (e.g. "per P4"). At block planning (every 3–4 weeks) the
coach re-reads the full summaries and checks this digest still holds.

**Evidence grades.** **A**: systematic review/meta-analysis or broad research consensus.
**B**: several studies or strong coaching consensus. **C**: one book/coach's method or a
plausible claim not checked against research. The library summaries look AI-generated
(`[cite: ...]` markers), so key numbers were spot-checked on Oct 11, 2026 (see "Verified").

**Where the athlete's own rules differ, the athlete's rules win** (brain_knowledge_base.md); the conflict
is noted so it's a known choice, not drift.

---

## Intensity distribution
- **P1. ~80% of sessions (by time/frequency, not km) easy, ~20% moderate-hard.** A.
  `80_20_running.md`, `speed_and_endurance_development.md`. Drives Rule 9 and the 80/20
  planning check (`core_plan_checks.py`).
- **P2. Avoid the moderate "gray zone" on easy days;** easy too fast makes hard days worse. B.
  `80_20_running.md`, Pfitzinger ("never race recovery runs").
- **P3. Hard days count: quality sessions only when fresh.** B. `80_20_running.md`.
  Drives the gate's hours-since-hard/long-run rule.

## Volume and progression
- **P4. Total volume is the strongest lever for endurance; build it gradually.** A/B.
  `80_20_running.md`, `volume_and_progression_framework.md`, Pfitzinger (plan tiers by
  weekly volume). Drives the roadmap: weekly km toward 55–65.
- **P5. Know your baseline volume.** Below it, rebuild fast (20–30%/week); above it,
  +5–10% held 2–3 weeks with flat consolidation weeks (e.g. 42, 42, 45, 45, 48). C.
  `volume_and_progression_framework.md`. The athlete's baseline: ~42 km/week (flat 12 months).
- **P6. Every 3rd–4th week: cut volume ~30% (recovery week).** B.
  `speed_and_endurance_development.md`, Pfitzinger.
- **P7. Never raise volume and intensity in the same week.** B.
  `speed_and_endurance_development.md`.
- **P8. Single-run spike: a run > 10% longer than the longest of the previous 30 days
  raises injury risk.** B (2025 cohort study, see RESEARCH_LOG.md). Drives the planning check;
  lifetime capacity (ledger) is shown next to it, never instead of it.
- **P9. Long run ≤ ~30–35% of weekly km in a normal week; extended runs (> 21 km) alternate
  with shorter long runs when weekly volume is ~40 km.** C.
  `maintenance_and_weight_loss.md` §6. Drives the long-run-share check.

## Building speed
- **P10. Strides 2–3×/week (6–10 × 80–100 m, full recovery) before adding VO2max work.** B.
  `speed_and_endurance_development.md`, Pfitzinger.
- **P11. Hill sprints (8–10 × 10 s, full 2–3 min rest): power and economy at low impact.** B.
  `speed_and_endurance_development.md`, `training_fast_vs_slow_twitch`.
- **P12. VO2max work: 600–1200 m (or 30 s–5 min) at ~5K effort; end the session when pace
  drops.** B. Pfitzinger, `training_fast_vs_slow_twitch`.
- **P13. Threshold: 20–40 min comfortably hard (cruise intervals in heat).** B. Pfitzinger
  (15K–HM pace), `speed_and_endurance_development.md`.
- **P14. The athlete is fast-twitch leaning (sprint background):** short work bouts, generous passive
  rest, very easy recovery runs, lots of aerobic volume. C. `training_fast_vs_slow_twitch`.
- **P15. Quality sessions: 1–2 per week.** B. Pfitzinger, `speed_and_endurance_development.md`
  (operating-manual no-race protocol said 1).

## Periodisation and readiness
- **P16. Phases: endurance → lactate threshold → race preparation → taper (3–6 weeks each).** B.
  Pfitzinger. Used when the athlete enters a race (race block built backwards from the date).
- **P17. "90% ready": stay ~4–6 weeks from race shape; fitness holds on reduced volume if
  intensity is kept.** A (concept; see Verified). `maintenance_and_weight_loss.md`.
  Drives the long-term roadmap.
- **P18. Tendons and bone need regular loading; they lose stiffness after ~10–14 days off.** C.
  `maintenance_and_weight_loss.md`.

## Strength
- **P19. Heavy strength (≥ 80% 1RM) improves running performance without hurting aerobic
  markers; combining heavy + plyometric/power work helps most.** A (Llanos-Lagos 2024,
  verified). `strength_training_for_endurance`. Drives Rule 10 (main lifts 3–6 reps) and leg days.
- **P20. Strength helps through economy, rate of force development and fatigue resistance,
  not VO2max.** A. `strength_training_for_endurance`, `Running_rewired`.
- **P21. Heavy lower-body work impairs running for up to ~48 h; keep it away from key runs.** A/B
  (Doma 2017/2019, Burt meta-analysis; RESEARCH_LOG.md). Drives the 72 h-before-long-run block
  and the 48 h-after consult.
- **P22. Upper body priorities: lats, upper back, anti-rotation core first; shoulders, arms
  second; chest optional.** C. `upper_body_strength_for_runners.md`. Matches the athlete's pull-day
  rule (strength + width, not mass).
- **P23. Lift after running on the same day; no heavy lifting the day before a long run.** B.
  `upper_body_strength_for_runners.md`.
- **P24. Hips, feet and mobility (big toe, ankle, hip) are the runner's "chassis".** C.
  `Running_rewired`.
- *Conflict:* the library caps lifting at 2–3×/week. The athlete lifts near-daily as a split
  (Rule 10, his choice for physique and strength); the gate's weekly hard-set and
  lifting-free-day warnings are the guardrail.

## Cross-training
- **P25. Cycling and swimming add aerobic volume without impact, but don't load calves and
  Achilles: running stays primary.** B. `cross_training_cycling_swimming.md`.
- **P26. Bike HR runs ~10–15 bpm lower than running at the same effort; swim ~15–20 lower.** B.
- **P27. No hard cross-training on a hard run day or within 48 h before one.** B.

## Fuelling and body composition
- **P28. Fuel the work: carbs before hard sessions; 60–90 g carbs/hour in long runs over ~90
  min.** A. Pfitzinger (4th ed.), `maintenance_and_weight_loss.md`. Drives Rule 7.
- **P29. Protein 1.6–2.0 g/kg/day; 20–40 g after hard sessions.** A.
  `maintenance_and_weight_loss.md`, `upper_body_strength_for_runners.md`. Rule 7 (110–130 g).
- **P30. Low energy availability suppresses HRV, hormones and adaptation.** A. Drives Rule 12
  and `sensor_nutrition_audit.py`.

## Monitoring
- **P31. HRV-guided training: hard sessions when HRV is in the normal range; use the 7-day
  trend over single nights.** A. Drives Rule 11 (option B).
- **P32. Track progress by pace at a fixed HR, not pace alone.** B.
  `speed_and_endurance_development.md` §6. Drives `core_progress_tracker.py`.

---

## Verified (Oct 11, 2026)
- P19/P20: Llanos-Lagos et al. 2024, *Sports Medicine* 54(7):1801–1833: heavy loads improve
  time-trial and time-to-exhaustion; combined methods may improve more; trivial effects on
  VO2max, vVO2max, MMSS, sprint
  ([Essex repository](https://repository.essex.ac.uk/37960/1/s40279-024-02018-z.pdf)).
  The effect sizes quoted in the library summary (−1.035, −0.469) were **not** confirmed.
  Companion running-economy meta-analysis: [PMC11052887](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC11052887/).
- P17: Mujika & Padilla: VO2max can be kept for ~4 weeks with drastically reduced volume and
  frequency if intensity is kept; endurance capacity can still fall
  ([JSSM review](https://jssm.org/jssm-08-622.xml-Fulltext)). The library's "15 weeks with up
  to 60% less volume" figure was **not** confirmed.
- "Strength training improves running economy 2–8%" (`upper_body_strength_for_runners.md`,
  Blagrove 2018): the review exists ([Northumbria](https://nrl.northumbria.ac.uk/id/eprint/33750));
  the percentage was **not** confirmed. Treat as "improves economy" (A), size unknown.

## Claims treated as weak (grade C or below) until checked
- Sprinting "optimises HGH and testosterone" (operating manual §10).
- "Every 1 kg lost saves 1–2 s/km" (`maintenance_and_weight_loss.md`): directionally right,
  size not checked.
- "Cortisol trap" details (`maintenance_and_weight_loss.md` §4B).
