# Contributing a slip model

Thank you for adding to the database. The full reference is in the
[README](README.md#adding-a-slip-model). This page is the short checklist.

## Before you open a pull request

- [ ] There is one file per model in `data/models/`, named
      `<EventCamelCase><Year>_<Author>_<PubYear>.json`
      (e.g. `Samos2020_Aktug_2021.json`). `models.json` is reserved and rejected.
- [ ] `"schema_version": "1.1.0"`.
- [ ] `reference.authors` lists every author (no `et al.`). `reference.doi` is the
      bare DOI. `reference.url` is an `http(s)` link.
- [ ] `event.origin_time` is an ISO 8601 UTC time (`...Z`), or `null` only if it is
      genuinely unknown.
- [ ] `provenance.data_origin` says where the numbers come from. Add
      `provenance.source_figure` (e.g. `"Figure 5"`) when values were digitized or
      transcribed from a figure or table.
- [ ] `reference_point.position` is set only if the paper states which point of the
      fault plane the coordinates refer to (see
      [the fault-plane frame](README.md#fault-geometry-reference-point-and-patches)).
      `along_strike_km` and `down_dip_km` are given together or not at all.
- [ ] `provenance.license` is set if the licence is known. The build lists the
      published records without one.
- [ ] No placeholders remain (`REPLACE_WITH...`, `example.org` links). If the record
      is unfinished, add `"status": "draft"`.
- [ ] Run `python build.py` and `python -m pytest`. Both pass, and any data-quality
      warnings for your record are understood or fixed.
- [ ] If the build changed `event_registry.json`, commit it too.

## If the build asks for an event-matching decision

The build stops when a model could belong to more than one earthquake, or is close
to one but not a clear match. It writes `event_review.json` with the candidate
earthquake keys. Decide by adding an entry to `event_overrides.json`: map the file
name to one of the keys, or to `"separate"`. An earthquake kept `"separate"` never
absorbs other files automatically, so a later model that resembles it also asks for
a decision. Never edit other contributors' records to influence matching. See
[Automatic event matching](README.md#automatic-event-matching).

## Corrections to existing records

Edit the record and describe what changed and why in the pull request, quoting the
source (page, figure, table). Never change slip, geometry, coordinates or other
numeric values without a source for the new value. An earthquake takes its origin
time, coordinates, magnitude and name from its first file. If you change these in
that file, the build updates the earthquake in `event_registry.json` and prints a
note. Commit the registry with the change. Do not rename published files, because
the file name is the model's permalink ID. If a rename is unavoidable, rename the
file without changing its content, run `python build.py` and commit the registry
before you edit the record. The file then keeps its earthquake key.
