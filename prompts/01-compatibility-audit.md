# Request: independent Claude Code compatibility audit

Act as an independent external reviewer of the current MS-DIAL repository
reanalysis system. Do not modify source code, download raw data, save Class
proposals, start MS-DIAL, or delete files.

Evaluate whether a future request such as "reanalyze MB-POST repositories 1 to
10" can be executed correctly using the currently connected
`msdial-repository-catalog` and `msdial-interactive` MCP servers.

## Audit procedure

1. List the available tools from both MCP servers and identify their versions or
   status responses.
2. Call Catalog status and verify that the active database is
   `D:\0_SourceCode\msdial_repository_catalog\catalog-data\native-smoke.sqlite`.
   Report study, analysis-unit, sample, and raw-file counts.
3. Verify that every repository plan/download preview explicitly uses
   `workspace_root="D:\13_MSDIAL_Public_Reanalysis\analysis"`, and that no raw-data or output
   path resolves to C:, the Claude project directory, or a temporary directory.
4. Search/inspect `MPST000001` through `MPST000010`. Enumerate every Catalog
   analysis unit with its `analysis_unit_id`, subrecord, project type, polarity,
   acquisition mode, target omics, review status, sample count, file count, and
   declared bytes. Report absent accessions explicitly.
5. Classify each unit as currently supported, excluded, or requiring scientific
   review. Current execution scope is untargeted LC-MS/MS acquired by DDA or
   DIA/AIF/SWATH. Treat GC-MS as out of scope. Confirm that an unknown
   acquisition mode requires raw-header review instead of defaulting to DDA.
   Confirm that a missing `analysis_purpose` is client-visible and blocks
   download readiness; use a clearly labeled audit-only purpose for later dry runs.
6. For one simple unit and one mixed accession, create Catalog reanalysis
   handoffs without saving Class decisions. Use the returned `handoff_path` with
   Interactive; do not inline or truncate the external file/sample manifests.
   Confirm that a 200-sample handoff and batch-plan response remain within a
   normal MCP response budget and that a deliberately inconsistent inline
   handoff is rejected.
7. Compare the Catalog handoff contract with the arguments of
   `msdial_repository_reanalysis_plan` and `msdial_download_repository_raw`.
   Verify these concrete eligibility outcomes: `c9967bd660f179b995c5` is
   excluded for SRM/targeted metadata, `86f5045931f5284fb9e9` requires review
   because untargeted status is unknown, and `6f27431da49ec82f3734` remains
   technically eligible but not download-ready until a Class proposal is saved.
   Confirm that Catalog and Interactive both expose `class_proposal:missing`.
   Test `confirmed=false` previews. Also verify that a technically excluded
   SRM/targeted unit remains blocked at the download tool after a synthetic
   Class decision removes `class_proposal:missing`; do not save a real proposal
   or transfer data.
8. Verify that previews distinguish selected-unit bytes from actual required
   repository-bundle bytes. For an MPST000010 unit, use a size limit below the
   bundle size and confirm that `size_limit:exceeded` appears in the complete
   `blocking_reasons` before transfer. Do not
   contact raw-data archives.
9. Check that handoff title, description, and normalized publication DOI are
   retained. Confirm that `publication_status` distinguishes a recorded
   publication, no upstream publication, and a failed retrieval. For
   `86f5045931f5284fb9e9`, confirm the eight listed objects become
   two primary WIFF inputs, two WIFF.SCAN sidecars, two WIFF2 alternates, and
   two auxiliary `.timeseries.data` files, while its Class/QA sample table has
   exactly two rows and no auxiliary sample IDs.
   Confirm that Catalog search and `msdial_catalog_get_analysis_unit` report the
   same two analytical samples, classify all four file roles consistently, and
   omit the sample payload by default while returning `sample_table_path`.
10. Confirm exact normalized allow-list matching, rejection of nested
    basename-only collisions, rejection of empty/relative `workspace_root`, and
    a bounded default Interactive status response (five jobs, artifact counts
    rather than complete path inventories). Validation failures must return a
    client-visible structured reason and detail rather than a generic MCP error.
    Confirm that the configured repository workspace boundary rejects an
    absolute path outside `D:\13_MSDIAL_Public_Reanalysis\analysis`.
11. Check Interactive status. If the backend is merely stopped, call
   `msdial_interactive_launch` with `open_browser=false`, then check status and
   MS-DIAL Console discovery again. Do not restart an unrecognized or
   incompatible process without explicit confirmation. Verify that job-scoped
   completion, mzTab-M validation, QA, and publication tools are present, but do
   not run an analysis.
12. Assess confirmation boundaries, raw retention, output isolation, failure
    recovery, private MSP handling, and provenance completeness.
13. Inspect the repository `answer_seed` without running MS-DIAL. Confirm that it
    requests `auto_peak_range`, 3,000-6,000 peaks, and
    `TimeBasedLinearWeightedMovingAverage`, and that the tuning tools expose QC
    selection plus 100-unit QTOF / 1,000-unit FT threshold steps.
14. Return both a readable report and the YAML block defined in
   `feedback/claude-audit-template.md`.

Use `PASS` only if one selected Catalog analysis unit can reach an Interactive
unit-scoped plan without expanding to sibling units, and the only remaining
download gate is an explicitly named unconfirmed scientific decision such as
Class. Use
`CONDITIONAL` for non-blocking scientific review. Use `FAIL` for a tool-contract
gap, wrong database, unavailable MCP, missing unit enforcement, or unsafe
confirmation behavior.

Address the final report to "Codex/MS-DIAL developers" and propose the smallest
testable code changes. Do not claim a Claude finding unless you reproduced it
through the connected tools or source inspection.
