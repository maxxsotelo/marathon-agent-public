# Research Log

Every lookup goes here with its sources, so nothing is googled twice. Newest first. When a
finding changes a rule, the rule cites the entry date. Principles distilled from these
entries live in `PRINCIPLES.md`.

---

## 2026-10-11 — Food trackers: getting macros into the system
**Finding:** MyFitnessPal has no public API; since Aug 2022 a hidden login captcha blocks
automated logins, so the only route is a browser session cookie via the unofficial
python-myfitnesspal library (cookies reported to last ~30 days; Cloudflare challenges cloud
IPs). The cloud environment's network policy also blocks myfitnesspal.com. Cronometer,
MacroFactor and Lose It also have no personal API (CSV exports only). **Garmin Connect's own
food logging is readable through the existing token** (`get_nutrition_daily_food_log`
returns per-food entries with the daily totals); today it shows only MyFitnessPal's calorie
total (no foods logged in Garmin).
**Use:** the athlete deprioritised MFP ("whatever with the MFP"). Options offered: log food in the
Garmin Connect app (no new login or network change), or keep screenshots.
**Sources:** [python-myfitnesspal docs](https://python-myfitnesspal.readthedocs.io/en/latest/getting_started.html),
[mfp-mcp](https://pypi.org/project/mfp-mcp/),
[cookie export script](https://glama.ai/mcp/servers/@ai-mcp-garage/mcp-myfitnesspal/blob/872d727cbe872badb4dec04b59a709e929b5442e/export_cookies.py).

## 2026-10-11 — Single-run spikes vs. weekly load (injury risk)
**Question:** what predicts running injury: single-session jumps or weekly load/ACWR?
**Finding:** Garmin-RUNSAFE cohort, 5,205 runners, 588,071 sessions, 1,820 injuries. Compared
with sessions no more than 10% longer than the longest run of the previous 30 days, injury
hazard was 1.64× for a 10–30% spike, 1.52× for 30–100% and 2.28× for > 100%. Week-to-week
change and **ACWR showed no meaningful association** with injury in this cohort.
**Use:** P8 (single-run spike check at planning). Note for review: the gate's ACWR veto is a
CLAUDE.md hard gate that the athlete chose; this study is a reason to treat the single-run spike as at
least as important as ACWR, not to drop ACWR.
**Sources:** Frandsen et al., *Br J Sports Med* 2025, doi 10.1136/bjsports-2024-109380
([PMC12421110](https://pmc.ncbi.nlm.nih.gov/articles/PMC12421110));
summaries: [Physiotutors](https://www.physiotutors.com/?p=26053),
[PhysioExtra](https://physioextra.ca/en/health-tips/running-study-injuries/).
Full text not read; the "1–10% increases also raised risk" claim in one summary is unverified.

## 2026-10-11 — Library spot-check (strength, detraining, economy)
- Llanos-Lagos et al. 2024 (*Sports Medicine* 54:1801–1833): heavy loads (≥ 80% 1RM) improve
  time-trial/time-to-exhaustion; combining methods may help more; trivial effect on VO2max,
  vVO2max, MMSS, sprint. Effect sizes in our summary not confirmed.
  [Essex PDF](https://repository.essex.ac.uk/37960/1/s40279-024-02018-z.pdf),
  running-economy companion [PMC11052887](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC11052887/).
- Mujika & Padilla detraining reviews: VO2max held ~4 weeks with drastically reduced volume and
  frequency if intensity kept; endurance capacity can still drop (one hard session/week kept
  VO2max but endurance fell ~20% in 4 weeks).
  [JSSM](https://jssm.org/jssm-08-622.xml-Fulltext),
  [Part II PDF](https://paulogentil.com/pdf/Detraining%20-%20Loss%20of%20Training-Induced%20Physiological%20and%20Performance%20Adaptations.%20Part%20II.pdf).
- Blagrove et al. 2018 (*Sports Medicine*) strength → running economy: review confirmed, the
  "2–8%" figure not confirmed. [Northumbria](https://nrl.northumbria.ac.uk/id/eprint/33750).

## 2026-10-11 — Treadmill vs. outdoor cadence (Forerunner 165)
**Question:** why does treadmill cadence read lower?
**Finding:** treadmill and overground running are biomechanically very similar; step rate is
essentially unchanged. The athlete's data: 6–10 spm lower on the treadmill at matched pace, with max
spikes of 210–240 spm: a wrist-sensor artefact (no foot pod).
**Use:** cadence trends use outdoor runs only; FBI (form) only from outdoor runs.
**Sources:** Van Hooren et al. 2020, *Sports Medicine*
([Maastricht PDF](https://cris.maastrichtuniversity.nl/ws/portalfiles/portal/53120529/Hooren_2019_A_Systematic_Review_and_Meta_Analysis_of_Crossover_Studies.pdf));
[summary](https://cissik.com/blog/?p=2293).

## 2026-10-11 — Heavy legs after a long run (the 48 h consult)
**Finding:** lower-body resistance training impairs running economy and performance for up to
48 h (Doma et al. 2017, 2019). Muscle function is down at 24 and 48 h after damaging exercise
(Burt et al. meta-analysis); knee-extension strength recovered by 48 h after downhill running;
damage markers can persist to 72–96 h while running economy is already normal (Paschalis).
No study tested a long run followed by heavy legs directly.
**Use:** P21; gate CONSULT for heavy legs < 48 h after a long/hard run; block heavy legs
< 72 h before a long run.
**Sources:** [Doma 2019 (PMC6548784)](https://pmc.ncbi.nlm.nih.gov/articles/PMC6548784),
[Doma 2017](https://researchonline.jcu.edu.au/49494/1/49494%20Doma%20et%20al%202017%20accepted.pdf),
[Burt et al.](https://wlv.openrepository.com/items/185bdcd5-5dea-4ff7-a67f-dcdba40a3439),
[Sci Rep 2020 downhill](https://link.springer.com/10.1038/s41598-020-76008-2),
[Paschalis](https://thieme-connect.de/products/ejournals/html/10.1055/s-2005-837461).

## 2026-10-11 — Lifting on low-HRV days
**Finding:** in resistance-trained men, lnRMSSD was back to baseline 24 h after six sets to
failure (Flatt 2019) and after varied time-under-tension sessions (Erickson); very high-volume
sessions suppressed some markers longer. Upper-body sessions do cause acute vagal withdrawal,
sometimes more than lower-body (small studies). HRV-guided strength training shows good gains
in early studies; evidence still thin.
**Use:** gate CONSULT for lifting on days HRV option B trips; middle option = cut ~1/3 of sets,
stop 2 reps short of failure.
**Sources:** [Flatt 2019](https://doaj.org/article/7aa301e7f43947f2bd3e2c4ea34c7a87),
[Erickson thesis](https://openscholar.uga.edu/record/4038),
[Marshall 2022](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC9141936/),
[SciELO bench vs squat](https://scielo.br/j/rbme/a/QCHvLRSpLJLqvWhJ86FBggc/?lang=en),
[Frontiers 2024 HRV-guided RT](https://www.frontiersin.org/journals/physiology/articles/10.3389/fphys.2024.1472702/full),
[remote HRV-guided strength](https://semaphore.uqar.ca/id/eprint/3555/).

## 2026-10-11 — Garmin token lifetime
**Finding:** garminconnect 0.3 uses a DI access token (~18 h, refreshed automatically) and a
refresh token whose lifetime Garmin does not publish; the library doesn't store it. The old
garth (pre-0.3) tokens lasted ~1 year, which does not apply. The athlete's token was created Oct 7, 2026.
**Use:** planned renewal every 4 weeks (next: weekend of Oct 31 – Nov 1), daily health check in
the morning routine; shorten the cycle if a token ever fails earlier.
**Sources:** [python-garminconnect](https://github.com/cyberjunky/python-garminconnect),
[garth](https://www.github.com/matin/garth).
