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
11. Before production, run `msdial_start_peak_count_diagnostic` without an
   explicit representative so Interactive selects a mid-run QC or non-blank
   sample. Call `msdial_estimate_peak_height` for the default 3,000-6,000 range,
   add the accepted threshold to the answers, and preserve
   `TimeBasedLinearWeightedMovingAverage`.
12. Execute approved units sequentially, retaining the exact job ID and
   artifact inventory for each unit.
13. Validate mzTab-M and generate only scientifically evaluable QA statements.
14. Summarize successes, exclusions, failures, storage, and source-code feedback.

Never treat a missing accession as an empty successful study. Never combine
analysis units. Never use `allow_partial_mapping=true` without explicit consent.
Never delete raw data by default.
