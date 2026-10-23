# Example methods

## Example: a threshold count and total

The rows below are a format illustration. A real submission uses the contributor's
own worked numbers.

```yaml
method: mangrove_restorable_above_threshold
title: Restorable mangrove in units above a restorability threshold
description: <the contributor's own words: what it is for and who uses it>
packs: [land-cover]
inputs:
  units: {feed: mangrove_restoration_mow_sea_pacific}
params:
  threshold: {value: 70, unit: percent}
steps:
  selected: units.pct_restorable > threshold
  n_units: count(selected)
  restorable_ha: sum(units.restorable_area_ha, where=selected)
outputs:
  n_units: {unit: units}
  restorable_ha: {unit: ha}
source: <the contributor's citation>
license: <the licence the source states>
examples:
  - rows:
      units:
        - {pct_restorable: 99, restorable_area_ha: 343}
        - {pct_restorable: 52, restorable_area_ha: 492}
        - {pct_restorable: 80, restorable_area_ha: 9099}
    expect: {n_units: 2, restorable_ha: 9442}
```

## Example: an allometric equation over plot measurements

The coefficients come from the person's source; never fill them in yourself.

```yaml
method: mangrove_agb_plot_allometry
title: Above-ground biomass of measured trees, from <the source's equation>
description: <the contributor's own words>
packs: [land-cover]
inputs:
  trees: {table: <a contributed table of tree measurements>}
params:
  a: {value: <number from the source>, unit: "1"}
  b: {value: <number from the source>, unit: "1"}
  wood_density: {value: <number from the source>, unit: g/cm3}
steps:
  agb_kg: a * wood_density * trees.dbh_cm ** b
  total_agb_t: sum(agb_kg) / 1000
  trees_measured: count(trees)
outputs:
  total_agb_t: {unit: t}
  trees_measured: {unit: trees}
countries: [<where the equation was fitted, if it is regional>]
source: <paper, DOI, equation number>
license: <licence>
examples:
  - rows: {trees: [<two or three rows with known dbh_cm>]}
    expect: {total_agb_t: <the person's own hand-calculated total>, trees_measured: <n>}
```
