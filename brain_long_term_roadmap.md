# Long-Term Roadmap (replaces the archived marathon plan, Oct 11 2026)

**Main goal (the athlete, Oct 11 2026): run faster and longer in general, so that when the next
marathon opportunity comes he can adapt quickly and crush his goals.** No race is entered.
The old Capas / New Clark City plan is in `archive/brain_marathon_plan.py` (its block
structure is the template for a future race block).

The idea is the "90% ready" state (PRINCIPLES.md P17): always about 4–6 weeks from race
shape. Phases move on **fitness criteria** read from `brain_athlete_history.md`, never on
dates. The monthly progress review (block planning) decides whether a phase is done.

Long-term milestones (brain_knowledge_base.md section 7): half marathon 1:40 → 1:35 →
1:30; marathon sub-3:30 first, then sub-3; a 32–35 km long run inside a race block.

---

## Phase A — Raise the floor (current)
- **Volume:** from the ~43–46 km/week baseline toward 55 km. Above baseline, +5–10% held
  2–3 weeks, then a recovery week (~70%) every 3rd–4th week (P5, P6). Example:
  46, 46, 50, 50, 36, 53, 53, 56.
- **Long runs:** alternate 16–20 km with a fast finish or marathon-pace segment, and 22–25 km
  easy, always inside the single-run cap (longest of the last 30 days + 10%, P8).
- **Quality:** strides 2×/week; one threshold session/week (cruise intervals in heat, P13);
  a **5K time trial every 6–8 weeks** as the speed benchmark (the first one sets the baseline).
- **Strength:** legs 1–2×/week (Rule 10, heavy + power once re-adapted, P19); upper body split.
- **Done when:** 4 straight weeks at 52 km+ without an HRV trend flag, a 5K time-trial
  baseline recorded, and pace at 150 bpm not declining.

## Phase B — Raise the ceiling
- **Volume:** 55–65 km/week, same progression rules.
- **Quality:** two sessions/week: threshold + VO2max or hill repeats (P12, P15); strides stay.
- **Long runs:** 22–26 km every other week; marathon-pace segments grow (Rule 5).
- **Done when:** 6 weeks at 60 km+, a half marathon (race or time trial) at or under 1:40,
  a 25 km+ long run in the last 6 weeks.

## Phase C — Ready base (hold here between races)
- 55–60 km/week, one quality session plus strides, long run 20–25 km, a 5K or 10K test
  every 6–8 weeks. From here a marathon is 12–16 weeks away.

## Race block (only when the athlete enters a marathon)
- Built backwards from race day with Pfitzinger's phases (P16): endurance → lactate
  threshold → race preparation → taper; 12–18 weeks depending on the starting phase.
- The 32–35 km long runs live here only (KB section 7), with the pre-fatigue leg session
  allowed in the last 8–10 weeks (Rule 5).
- The race goal is set from the latest half-marathon result and the tracker, not from hope.

---

## Monthly review (at block planning)
1. Run `python core_progress_tracker.py` and `python core_plan_checks.py`.
2. Verdict per trend: improving / plateau / declining; any flagged plateau gets its playbook
   fix (tracker section 5).
3. Is the current phase done? If yes, move on; if a criterion is slipping, say why.
4. Re-read the full library summaries; check PRINCIPLES.md still holds.
5. Write the verdict and the next 3–4 weeks in `brain_current_week_plan.py` and the session notes.
