---
name: msdial-repository-batch
description: Audit or orchestrate a bounded batch of public repository analysis units through the local MS-DIAL Catalog and Interactive MCP servers. Use for MB-POST accession ranges, analysis-unit selection, compatibility review, reproducible MS-DIAL runs, QA, mzTab-M, publication artifacts, and structured feedback to Codex.
argument-hint: "audit | MB-POST MPST000001-MPST000010"
---

# MS-DIAL repository batch

Read `CLAUDE.md` first. Then read the existing detailed execution skill and its
repository reference:

- `D:\0_SourceCode\msdial_interactive_app\skills\msdial-guided-analysis\SKILL.md`
- `D:\0_SourceCode\msdial_interactive_app\skills\msdial-guided-analysis\references\repository-reanalysis.md`
- `D:\0_SourceCode\msdial_interactive_app\skills\msdial-guided-analysis\references\tool-reference.md`

If `$ARGUMENTS` contains `audit`, follow
`D:\13_MSDIAL_Public_Reanalysis\code\prompts\01-compatibility-audit.md`.
Do not download or execute MS-DIAL.

For a repository range:

1. Ask what the user wants to learn: the scientific question and comparison,
   annotation versus comparative-profiling emphasis, and required outputs.
   Preserve the answer as `analysis_purpose` in every plan and download call.
2. Verify both MCP servers and report their tool surfaces.
3. Query the local Catalog for every requested accession and enumerate analysis
   units. Report missing accessions.
4. Use `D:\13_MSDIAL_Public_Reanalysis\analysis` explicitly as
   `workspace_root` for every
   repository plan and download preview. Never derive it from the Claude project
   directory.
5. Classify units as eligible, excluded, or requiring review using the supported
   scope in `CLAUDE.md`: untargeted LC-MS/MS acquired by DDA or DIA/AIF/SWATH.
   If acquisition is unknown, require raw-header preflight; never guess DDA.
   mzML is an input. mzXML and mzData are `requires_conversion`: MS-DIAL has no
   reader for them, so the unit is excluded before download until a reviewed
   ProteoWizard conversion has produced an mzML manifest with its own
   provenance. The exclusion is unit-wide: one listed file or one sample naming
   an `.mzXML` excludes the unit, even when its other inputs are readable.
6. Obtain `msdial_catalog_reanalysis_handoff` for every selected unit. Keep the
   returned `handoff_path`; do not inline or truncate its external file/sample
   manifests or replace it with an accession-level Interactive inspection.
7. Pass all selected paths to `msdial_repository_batch_plan` as
   `analysis_unit_handoff_paths`. Confirm that
   every run has a distinct `analysis_unit_id` and workspace, and report all
   `blocking_reasons` before requesting a download.
8. Produce a dry-run manifest with file/sample counts, technical signature,
   selected-unit bytes, actual required bundle bytes,
   Class/contrast review needs, and proposed output directory for each unit.
9. Ask for one batch-level approval of the manifest and size budget. Keep the
   per-download and per-production-run confirmation boundaries required by the
   tools.
10. Call `msdial_repository_reanalysis_plan` and
   `msdial_download_repository_raw` with the same `analysis_unit_handoff_path` for
   each approved unit. For accession-bundle downloads, verify the resulting
   manifest admits only allow-listed paths into the analysis CSV.
   When raw-header preflight reports `Mixed`, the unit holds more than one
   acquisition mode and cannot run as one. Preview `msdial_split_repository_unit`
   with `confirmed=false`, show the parts, and split only on an explicit
   confirmation. Never run the Mixed parent. Each part is its own run, with its
   own workspace, preflight, diagnostic, gates and boundary-4 confirmation; the
   parts share the parent's `raw\`, and the parent stays the raw owner.
11. Before production, run `msdial_start_peak_count_diagnostic` on a mid-run QC,
   or the non-blank Sample nearest the run-order midpoint when there is no QC.
   Interactive picks one itself only from `analytical_order`; when the guided
   plan reports `analytical_order` as `derived_from: "listing"` or
   `"embedded"`, that order is the file listing or a number read out of the file
   names (with every Blank and QC placed after the samples), not the run. Order
   the files by the raw headers'
   `acquisitionStartTime` instead, and pass the choice as
   `representative_file`. A `file_type` Standard is not a Sample. Call
   `msdial_estimate_peak_height` for the default 3,000-6,000 range with the
   step for the instrument: the inspection calls every mzML `QTOF`, so pass
   `threshold_step=1000` when the raw header or sample metadata shows
   Fourier-transform data (Orbitrap, FT-ICR). Add the accepted threshold to the
   answers and preserve `TimeBasedLinearWeightedMovingAverage`.
12. Write the production bundle with `msdial_prepare_guided_analysis`, with the
   accepted `minimum_peak_height` in the answers, then run the
   `before-production` gate (see below) against it and stop the unit on a
   refusal. The gate reads `output\method.txt`, so it cannot run before this.
   The diagnostic in step 11 does not touch the production files: it writes its
   own single-file `analysis_files.csv`, `method.txt` and `run-manifest.json`
   under `<workspace>\diagnostics\<diagnostic-job-id>`, and
   `msdial_estimate_peak_height` appends the measurement to
   `peak_height_diagnostics` in `provenance\run-manifest.json`. PKH-1 fails a
   `method.txt` whose threshold no recorded diagnostic produced.
13. Execute approved units sequentially, retaining the exact job ID and
   artifact inventory for each unit.
14. Run the `after-run` gate. Validate mzTab-M and generate only scientifically
   evaluable QA statements.
15. Run the `before-publish` gate before generating publication artifacts, and
   report every refusal in the summary rather than publishing around it.
16. Summarize successes, exclusions, failures, storage, and source-code feedback.

Never treat a missing accession as an empty successful study. Never combine
analysis units. Never use `allow_partial_mapping=true` without explicit consent.
Never delete raw data by default.

## Gates

Every stage of this pipeline reports success on its own terms, and several
observed failures produced a complete, self-consistent, publishable result set
that was not the study anybody approved. Run the gate; do not infer from a tool
reporting no blockers.

```powershell
python D:\13_MSDIAL_Public_Reanalysis\code\scripts\verify-run-invariants.py `
  <unit-workspace> --stage before-production --strict
```

`--stage` is `before-production`, `after-run` or `before-publish`. Exit code 0
means every evaluated check passed, 2 means at least one failed, 3 means the
workspace is unusable, and 4 (`--strict` only) means a check could not be
evaluated because an artifact that stage was responsible for producing is
absent. `--json` emits the full report.

**Always pass `--strict`.** Without it a workspace where nothing has happened
exits 0: `ok` means "no check FAILed", and a directory holding an empty
`provenance/` and an empty `output/` produces no FAILs at all -- measured on
2026-09-20, it returned 15 `not_evaluable` and exit 0. A unit nobody ran and a
unit that ran correctly gave the same answer, which is the one answer an
unattended loop must never get wrong. `--strict` exempts only the absences that
are a correct state: a unit whose acquisition mode needed no header read, and a
raw tree already released under the retention policy.

The gate reads the retained artifacts, not tool responses. That is deliberate:
the guided-plan response that reports the same sample count is large enough to
be truncated in transport, and its `blockers` field is serialised after the
payload, so a missing `blockers` and an empty `blockers` look identical.

A check that cannot be evaluated reports `not_evaluable`, never `pass`. Treat
`not_evaluable` on a stage's own artifacts as a reason to stop, not as consent.

Run the gate even where the server now refuses the same thing. The two checks are
independent on purpose: one is a property of the artifacts on disk, the other a
property of the server that wrote them, and a server that stops enforcing a rule
is exactly the case the gate exists to catch.

What the gate cannot do:

- It cannot establish which MS-DIAL Console binary produced the outputs. `BIN-1`
  compares the version the run manifest recorded against the version the
  mzTab-M attributes, which catches a batch split across two binaries, but
  neither is a hash and a version string did not change across a Console export
  fix. Record the resolved binary path and its hash in the unit summary
  yourself.
- It cannot see a unit that was never started. A batch's own state file is the
  only record that a unit was selected; the server's job registry keeps only the
  most recently updated jobs and downgrades running jobs on restart.

Server-side state on `msdial_interactive_app` `main`:

- The peak-count diagnostic writes to `<workspace>\diagnostics\<job>` (PR #16)
  and does not touch `output\analysis_files.csv`, `output\method.txt` or
  `output\run-manifest.json`. Seen on real data on 2026-09-25 (MTBLS2207): the
  production CSV kept its rows, the estimate landed in
  `provenance\run-manifest.json`, and the Console left its `.dcl`, `.pai2` and
  `_tags.xml` beside the representative raw file, as it does on every run.
  Verify the row count anyway; that is what CNT-1 is for.
- Raw data is never deleted without a separate explicit confirmation. Keeping
  `raw_retention_policy` at `keep` is still the right default for an unattended
  run, because it removes the decision rather than answering it. An accepted
  retention policy records intent and is not that confirmation: preview
  `msdial_cleanup_repository_raw`, show the exact directory and the retained
  inventory, and ask immediately before deleting. A split unit cannot be
  cleaned up yet: its parts share the parent's raw tree, which lies outside a
  part's workspace, and the parent is not a completed run, so the tool refuses
  both. Its raw data stay until split-parent cleanup exists.
- A unit whose manifest says `execution_allowed` is not true is refused before
  MS-DIAL starts, and so is a workflow whose polarity, output directory or input
  set disagrees with the manifest.
- A run that returns success without producing every planned export now fails.
- A rejected tool call answers with `ok: false`, a `reason` and the backend's own
  message instead of raising past the MCP boundary.

## Unattended operation

An unattended run may only proceed inside a manifest the user has already
approved. It may not widen `maximum_gb` to clear a size blocker, may not set
`allow_partial_mapping=true`, and may not change `raw_retention_policy`. When
the loop reaches a decision that needs a confirmation it does not hold, stop the
unit, write a failure record beside its artifacts, and continue with the next
unit; do not proceed and do not retry blindly.

Record `manifest_path`, `output_root`, `input_path`, the `analysis_unit_id` and
every job ID into the batch's own state file as each tool returns. The server's
job registry keeps only the most recently updated jobs and downgrades running
jobs to `interrupted` on restart, and no tool accepts a manifest path as a way
back into a unit, so a handle that is not recorded client-side is lost.

Treat any tool result that is not a mapping, or that is a string beginning
`Error executing tool `, as an opaque server failure: stop that unit, write the
failure record, and do not retry. The reason is not recoverable client-side.
