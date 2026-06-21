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
- **License:** Creative Commons Attribution 4.0 International (CC BY 4.0),
  <https://creativecommons.org/licenses/by/4.0/>. Redistribution with
  attribution is permitted; the citation above is the required credit.
- **Modifications:** excerpted to a contiguous 6-hour window of the `mains`
  column and reformatted to the `t_s,watts` columns used here; the watt values
  are otherwise unmodified.
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

## `cyprus_netload.csv`

A 7-hour midday window of matching PV generation (`Ppv`) and load demand
(`Pload`) from one residential prosumer in Cyprus, on a partly-cloudy day — so
the PV carries real cloud-driven transients a synthetic half-sine lacks, and the
PV/load timing is genuinely correlated (same site, same instant).

- **Source:** Hadjidemetriou, L., Asprou, M., & Nikolaou, P. (2023).
  *Photovoltaic Generation and Load Demand Datasets with 30 seconds resolution
  from an Actual Prosumer in Cyprus* (Version v1) [Data set]. Zenodo.
  <https://doi.org/10.5281/zenodo.8348862>
- **License:** Creative Commons Attribution 4.0 International (CC BY 4.0),
  <https://creativecommons.org/licenses/by/4.0/>. Redistribution with
  attribution is permitted; the citation above is the required credit.
- **Modifications:** excerpted to a contiguous 7-hour midday window of the
  `Ppv`/`Pload` columns and reformatted to the `t_s,load_w,pv_w` columns used
  here. The raw CSV is unscaled; the evaluation rescales both power columns by
  ×0.45 at run time (balcony-system sizing) — that scaling is applied in code,
  not baked into this file.
- **Resolution:** 30-second samples (the dataset's native cadence).
- **Why this window:** 2022-09-01 09:00–16:00 is partly cloudy (PV peaks ~4.2 kW
  with ~40 cloud dips >15% of peak across the window), so the net-load swings
  through real solar variability. The evaluation scales both columns to a
  balcony-system size (so the scaled PV stays under the load model's 2 kW solar
  clamp) while preserving the cloud-dip shape; each seed slices a different part
  of the day (morning ramp / cloudy midday / afternoon).

To refresh or replace the excerpt, download a day file from the source above and
extract a clean daytime window of the `Ppv`/`Pload` columns into the
`t_s,load_w,pv_w` format used here.
