# Real-world load traces

Vendored excerpts of real household electricity data used to drive the
steering evaluation with a realistic disturbance, alongside the synthetic
scenarios. Real traces carry the *correlated drift* and *persistent appliance
switching* that synthetic IID base-load noise lacks, so they punish a balancer
that over-damps to reject white noise but then lags real load changes.

## `uci_household.csv`

A contiguous 6-hour evening window of the whole-house active-power channel
(`Global_active_power`, converted kW → W) from one French home.

- **Source:** Hebrail, G. & Berard, A. (2006). *Individual Household Electric
  Power Consumption* [Dataset]. UCI Machine Learning Repository.
  <https://doi.org/10.24432/C58K54>
- **License:** Creative Commons Attribution 4.0 International (CC BY 4.0).
  Redistribution with attribution is permitted; the citation above is the
  required credit.
- **Resolution:** 1-minute samples (the dataset's native cadence). The
  evaluation holds each sample (zero-order hold) between minutes and layers a
  small sub-minute jitter on top, since real sub-minute behaviour is not
  captured at this cadence.
- **Why this window:** it mixes quiet baseline stretches with cooking spikes
  that exceed a single battery's discharge limit, so a 60-minute slice
  exercises both the steering band and the saturated-grid regime. Each
  evaluation seed slices a different offset into the 6 hours, so seeds see
  genuinely different load regimes (not just re-randomised noise).

To refresh or replace the excerpt, download the dataset from the source above
and extract a clean (no missing `?` samples) contiguous window of the
`Global_active_power` column into the `t_s,watts` format used here.
