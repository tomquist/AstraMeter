# Real-world load traces

Vendored excerpts of real household electricity data used to drive the
steering evaluation with a realistic disturbance, alongside the synthetic
scenarios. Real traces carry the *correlated drift* and *persistent appliance
switching* that synthetic IID base-load noise lacks, so they punish a balancer
that over-damps to reject white noise but then lags real load changes.

## `rae_household.csv`

A contiguous 6-hour window of the whole-house active-power channel (`mains`,
1 Hz) from House 1 of the Rainforest Automation Energy dataset.

- **Source:** Makonin, S. (2018). *Rainforest Automation Energy Dataset (RAE)*
  [Dataset]. Harvard Dataverse. <https://doi.org/10.7910/DVN/ZJW4LC>
  Paper: Makonin, Wang & Tumpach (2018), *RAE: The Rainforest Automation Energy
  Dataset for Smart Grid Meter Data Analysis*, Data 3(1):8.
- **License:** Creative Commons Attribution (CC BY). Redistribution with
  attribution is permitted; the citation above is the required credit.
- **Resolution:** 1-second samples — the dataset's native cadence, matching the
  controller's ~1 s meter-poll loop, so the controller sees real second-to-second
  structure rather than a held staircase.
- **Why this window:** it spans a calm overnight baseline through morning
  activity, mixing a ~500–800 W base with frequent switching and cooking spikes
  that exceed a single battery's discharge limit, so a 1-hour slice exercises
  both the steering band and the saturated-grid regime. ~28% of seconds carry a
  >20 W change. Each evaluation seed slices a different offset into the 6 hours,
  so seeds see genuinely different load regimes (quiet night vs busy morning),
  not just re-randomised noise.

To refresh or replace the excerpt, download `house1_power_blk1` from the source
above and extract a clean contiguous window of the `mains` column into the
`t_s,watts` format used here (House 1 is North American split-phase; only the
watt timeseries is used, so the nominal voltage is irrelevant).
