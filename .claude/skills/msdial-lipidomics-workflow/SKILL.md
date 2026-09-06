---
name: msdial-lipidomics-workflow
description: Run a laboratory LC-MS/MS lipidomics workflow end to end - recognise the files, agree the sample table, run MS-DIAL in both polarities with a chosen LBM2 library, check the internal standards were annotated, normalize to concentration, and merge positive and negative into one lipidome. Use when someone points at a folder of raw lipidomics data and asks for the lab's standard analysis, or asks to normalize or merge an existing MS-DIAL result.
---

# Laboratory lipidomics workflow

A person hands over a folder of raw data and expects a quantified lipidome back. The
work is mostly decisions, not computation: which library, which retention-time
behaviour, which sample belongs to which group, which polarity quantifies which lipid
class. Each of those is settled once, confirmed by the person, and then saved as a
workset so the next dataset only has to confirm what changed.

## Before starting

- The Interactive backend must be running: `msdial_interactive_status`, and
  `msdial_interactive_launch(open_browser=false)` if it is not.
- A Console binary must be selected: `msdial_check_console_path`. Prefer one whose
  `provenance_status` is `verified`; if it reads `stale_mismatch`, say so and offer to
  rebuild, because the run's recorded software version would otherwise describe a
  different binary.
- Raw data and outputs stay off the C: drive and out of this repository.

## 1. Look at the data before asking anything

`msdial_guided_analysis_plan(input_path=...)` reports every recognised file with its
vendor, format, instrument family, acquisition type and a proposed sample table.

Report what it found — file count, format, polarity, whether a blank was detected —
before asking any question. A person can correct a misreading immediately, and cannot
correct a question they do not yet understand.

Positive and negative are separate runs. If the folder holds both, treat them as two
analyses that must later agree on their samples.

## 2. Agree the sample table

The proposal reads Class from how the file names vary: a token identical in every name
describes the study, one different in every name identifies the sample, and one taking
a few values each shared by several files is the comparison. `class_proposal` carries
the reasoning and the alternatives it rejected — the replicate index usually among
them. Show the whole table with that reasoning and ask, because a file name only
suggests the grouping and the person knows it.

Show every column that will reach MS-DIAL: file name, type (Sample/Blank/QC), class,
batch, analytical order, dilution factor. Two of these are silently consequential:

- **Analytical order** decides what "injection order" means in every drift plot
  downstream. If the file names carry a run number, say whether you used it.
- **Dilution factor** is 1 unless the project says otherwise, and a wrong value scales
  every concentration. Ask for it explicitly rather than assuming; if a metadata table
  accompanies the data, read the factor from there and say where it came from.

## 3. Agree the analysis settings

Ask, once, and record the answers in a named workset so later datasets inherit them:

- **Which annotation library.** A laboratory usually has its own LBM2 carrying
  predicted retention times for its own chromatography. Pass it as
  `answers["libraries"] = {"lbm_path": "<path to .lbm2>"}` with
  `library_strategy = "existing"`. Note: `libraries` is an object with
  `lbm_path` / `msp_paths` / `text_paths`, not a list.
- **Whether retention time is used for annotation.** A library with retention times
  for this chromatography is normally scored and filtered on them, with a tolerance of
  a couple of minutes; a library without them must not be. This is the person's
  judgement about their own library, so ask rather than infer. The guided plan raises
  it as `use_retention_time_for_annotation` once a library has been named, and asks for
  `retention_time_tolerance` only if the answer is yes.
- **Thread count.** `number_of_threads`, asked once; a property of their machine, not
  of the science.

These three do not block the plan: unanswered, they leave annotation without retention
time, which is the conservative reading of an unknown library. They appear in
`advisory_questions`, and leaving one unasked is a choice to accept that default —
so ask them, and say which defaults are standing if you do not.

Save the agreed set with `msdial_save_workset` under a name the laboratory will
recognise. The next dataset then starts from it and only has to confirm what changed.

## 4. Run each polarity

`msdial_prepare_guided_analysis` writes the reproducible bundle — analysis table,
method file, command, manifest — without running anything. Show the command and the
settings that matter, and obtain explicit confirmation before starting.

Then `msdial_start_guided_analysis(confirmed=true)` and follow with
`msdial_interactive_status` or `msdial_interactive_wait_for_completion`. A run of a few
files takes minutes; say so rather than leaving silence.

## 5. Check the internal standards were annotated

Normalization divides by an internal standard peak. If that peak was not annotated,
the lipid class it covers cannot be quantified — and the failure is silent unless
someone looks.

`MSDIALCUI normalize` resolves each standard by name against the alignment and reports
every resolution, every ambiguity, and every standard it could not find. Read that
report out. Standards absent for a good reason — the positive-mode standards are not
in a negative-mode run — are expected; say which those are, so the ones that matter
stand out.

## 6. Normalize to concentration

```
MSDIALCUI normalize -i <project.mddata> -s <standards.tsv> -o <output directory>
                    -u pmol_per_microL_plasma
```

It writes both matrices, `<alignment>_Height.txt` and `<alignment>_NormalizedHeight.txt`.
A concentration is the raw measurement divided by a standard, and nobody can check the
division with only one side of it, so both are kept and both are reported.

The standards table maps each lipid class to the standard that quantifies it and the
amount present: `StandardName`, `TargetClass`, `Concentration`, with `Any others`
covering classes the table does not name.

The amount depends on how much material was extracted, so the unit is a decision:
per microlitre of plasma, per milligram of tissue, per million cells. Confirm which
one applies and state it in the result — the exported matrix carries the unit in its
comment column, and a concentration without a unit is not a concentration.

Ignore any peak-ID column in a laboratory's lookup table. Those identify peaks in the
alignment the table was written against and mean nothing in a new run.

## 7. Merge the polarities

Each lipid class is quantified in whichever polarity and adduct measures it best. That
choice is a rule table, not something the data decides:

```python
from msdial_app.lipidome_merge import merge_pos_neg
merge_pos_neg(positive_matrix, negative_matrix, rules_table, output)
```

The merged table keeps one set of sample columns and pairs the polarities by position,
so it refuses unless both describe the same samples in the same order. If it refuses,
the two runs disagree about their samples — do not work around it.

Report what was kept and what was dropped, by reason. A count of kept lipids is only
meaningful beside the count of everything that did not qualify.

## What is not automated yet

- False-positive and false-negative review between steps 4 and 5 is still manual.

## Reporting

Say what ran, on which files, with which library and settings, and where the artifacts
are. When something was assumed rather than confirmed, name it. When a step was
skipped, say so — a workflow that reports success for work it did not do is worse than
one that fails.
