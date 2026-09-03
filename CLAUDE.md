# MS-DIAL public repository reanalysis

This project uses Claude as a scientific workflow reviewer and orchestrator.
Use the two local MCP servers rather than shell scripts for scientific workflow
execution.

## Mission

Select technically compatible public repository analysis units, preserve their
biological and analytical provenance, run one reproducible MS-DIAL workflow per
unit, validate mzTab-M, perform evaluable QA, and retain publication-ready audit
artifacts. The long-term objective is evidence-traceable repository reanalysis,
not maximal throughput at the cost of incorrect grouping.

## MCP responsibilities

- `msdial-repository-catalog` is the local metadata index. Use it to inspect
  accessions, enumerate analysis units, inspect samples and raw-file manifests,
  formulate Class/contrast proposals, and create a reanalysis handoff.
- `msdial-interactive` is the execution layer. Use it for bounded download,
  raw-header preflight, analysis metadata creation, MS-DIAL execution, mzTab-M
  validation, QA, publication reporting, and data-mining handoff.

Do not substitute accession-level Interactive metadata for a selected Catalog
analysis unit without proving that the same files and technical signature are
preserved.

Catalog handoffs are local file-backed contracts. After
`msdial_catalog_reanalysis_handoff`, pass its `handoff_path` to Interactive as
`analysis_unit_handoff_path`; for a batch, pass the paths as
`analysis_unit_handoff_paths`. Do not inline or truncate full sample/file
manifests in the model context. Treat any handoff consistency error as a stop.

## Local storage

Use this mandatory default repository reanalysis root:

```text
D:\13_MSDIAL_Public_Reanalysis\analysis
```

Pass it explicitly as `workspace_root` to every
`msdial_repository_reanalysis_plan` and `msdial_download_repository_raw` call.
Do not place raw downloads or MS-DIAL outputs on C:, in the Claude project
directory, in the user profile, or in a temporary directory. Interactive creates
`<workspace_root>\<repository>\<accession>\raw`, `provenance`, and `output`.
Require explicit confirmation before using any different absolute path.

## Supported production scope

- This public-repository campaign accepts LC-MS/MS only.
- Acquisition must be untargeted DDA or DIA/AIF/SWATH with an MS1 survey and
  product-ion spectra.
- One project type, ion mode, acquisition mode, chromatography regime, and ion
  mobility regime per MS-DIAL run.
- GC-MS, SRM/MRM, SIM, DI-MS, imaging MS, product-ion-only
  experiments, and unresolved mixed-polarity units are review or exclusion
  cases, not silent conversions. MS-DIAL Interactive can analyze some of these
  modes outside this campaign; that broader capability does not expand this scope.

## Evidence and decisions

Repository fields and raw headers are evidence. Class, contrast, target omics,
internal-standard ions, and annotation policy are scientific decisions. Keep
source facts, model inferences, and accepted decisions separate.

Before selecting units or proposing Class, ask what the user wants to learn from
the reanalysis. Record the scientific question, intended biological comparison,
whether annotation or comparative profiling is central, and required outputs as
`analysis_purpose`. Use that purpose consistently for Class, contrast, library,
QA, and reporting decisions. Do not proceed to download while it is missing.

For production repository runs, use the zero-threshold diagnostic before the
main run. Select a QC nearest the analytical-order midpoint, or a non-blank
sample nearest that midpoint when no QC exists. Set Minimum peak height to keep
approximately 3,000-6,000 peaks, using 100-unit steps for QTOF-type data and
1,000-unit steps for Fourier-transform data. Keep 0 when the diagnostic count is
at most 6,000. Use `TimeBasedLinearWeightedMovingAverage` and retain the method,
representative sample, diagnostic count, threshold step, and accepted threshold
in provenance.

For Class proposals, include one assignment per sample, selected source fields,
the intended contrast, rationale, and warnings about confounding or missingness.
Do not use continuous fields merely because they are available.

## Confirmation boundaries

Never perform these operations without an explicit user confirmation in the
current conversation:

1. Raw-data download, including destination, size bound, selected analysis
   unit, and retention policy.
2. Official-library download.
3. Saving a Class proposal.
4. Starting each production MS-DIAL run after showing the exact plan/command.
5. Raw-data deletion after validated output and retained-artifact inventory.

Dry-run previews must use `confirmed=false`. Default raw retention is `keep`.
The preview must report both selected-unit bytes and actual required bundle
bytes. The latter is the download approval and safety-limit quantity.

## Batch behavior

Treat an accession range as a selection request, not an instruction to run every
record. Enumerate every analysis unit, explain exclusions, estimate bytes, and
ask the user to approve the final run manifest. Process approved units
sequentially at first. Use a unique output directory and exact `job_id` for each
run. A failure in one unit must not erase or mutate another unit's artifacts.

## Annotation and private resources

For the current broad LC-MS annotation pilot, preserve separate evidence tiers:
LBM rule-based lipid annotation, strict MSP search, and broad MSP candidate
search. A lower-priority MS/MS reference match outranks a higher-priority
precursor-only suggestion. Broad candidates are not equivalent to high-quality
matches. Private VS20/VS21 MSP files may be used locally but must never be
copied into bundles, logs, repositories, or shared reports.

## Required outputs

For every attempted analysis unit retain a machine-readable run manifest,
repository/publication metadata, reviewed sample metadata, analysis CSV,
parameter file, command, software versions, logs, checksums, MS-DIAL text
outputs, mzTab-M validation, QA status, publication artifacts when requested,
and a failure record when unsuccessful.

## Feedback to Codex

When asked to audit, do not edit source code and do not download raw data. Return
the structure in `feedback/claude-audit-template.md`. Include exact MCP tool
names, arguments with secrets removed, returned error/status, expected behavior,
severity, and the smallest reproducible sequence. Distinguish missing feature,
contract mismatch, scientific ambiguity, and implementation defect.
