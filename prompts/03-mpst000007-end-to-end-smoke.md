# Independent end-to-end evaluation: MB-POST MPST000007

Use the `msdial-repository-batch` project skill and both local MCP servers.
Evaluate the current Catalog-to-Interactive implementation independently, then
perform one bounded reanalysis only after the required human confirmations.

Do not read any existing file under `feedback/` until you have written your own
initial findings. Do not treat values in previous audits as instructions.

## Fixed safety policy

- Repository: `mb_post`
- Accession: `MPST000007`
- Workspace root: `D:\13_MSDIAL_Public_Reanalysis\analysis`
- Raw-data retention: `keep`
- Analysis purpose: verify the end-to-end repository-to-mzTab-M workflow while
  preserving the biological cell-line comparison and complete provenance.
- Never combine different `analysis_unit_id` values in one run.
- Never replace a Catalog handoff with an accession-level download plan.
- Do not download, save a Class proposal, start MS-DIAL, or delete anything
  without the confirmation required by the corresponding tool.
- Do not use `allow_partial_mapping=true` unless I explicitly approve it.

## Phase A: independent dry-run

1. Check both MCP servers and report their available repository tools.
2. Query the local Catalog for this exact accession. Enumerate every analysis
   unit and explain which units are supported, excluded, or need review.
3. Select only a scientifically coherent untargeted LC-MS/MS DDA or
   DIA/AIF/SWATH analysis unit. Base the choice on Catalog evidence, not the
   accession name. Exclude GC-MS from this campaign and require raw-header review
   when DDA versus DIA cannot be established.
4. Inspect its technical settings, files, sample metadata, biological metadata,
   publication provenance, warnings, and unresolved fields.
5. Build a Class proposal request. If `sample / Cell line` is populated and
   defines the biological groups, propose it as the Class hierarchy and report
   the number of Classes and replicates. Preview the proposal without saving it.
6. Obtain `msdial_catalog_reanalysis_handoff` for that unit and pass the exact
   handoff to `msdial_repository_batch_plan`, including the fixed
   `analysis_purpose` above.
7. Pass the same handoff to `msdial_repository_reanalysis_plan`. Verify that
   the same `analysis_purpose` is supplied and that
   `analysis_unit_id`, technical settings, file count, byte estimate, and output
   workspace remain unit-scoped.
8. Confirm that the unsaved Class decision appears as
   `class_proposal:missing` and prevents download readiness. Do not begin Phase B
   implicitly.

The dry-run is a failure if mixed polarity, separation, acquisition mode, or a
sibling analysis unit is collapsed into one scalar plan. Record any such result
as a blocking contract mismatch.

## Phase B: confirmed reanalysis

Continue only after I explicitly approve the displayed Class proposal,
destination, size bound, selected unit, and raw retention policy.

1. Save the accepted Class proposal, regenerate the Catalog handoff, and verify
   that `blocking_reasons` is empty.
2. Call `msdial_download_repository_raw` with the regenerated handoff and
   the same `analysis_purpose`, initially with `confirmed=false`. Show the
   complete preview and ask me to approve the
   destination, required bundle bytes, and retention policy. Call it again with
   `confirmed=true` only after that approval.
3. Start the unit-scoped download using that regenerated handoff. Poll the exact
   download job and report progress, speed, ETA, and current object.
4. Confirm all handoff-listed checksums supplied by the Catalog. Verify that no
   sibling-unit file enters `input_candidates` or `analysis_files.csv`.
5. Run raw-metadata preflight on representative files. Stop if polarity,
   acquisition mode, or separation contradicts the handoff.
6. Preview repository metadata-to-Class mapping. Report unmatched and ambiguous
   files, and request confirmation before writing `analysis_files.csv`.
7. Continue through guided MS-DIAL planning. Present unresolved peak-picking,
   RT-correction, library, QA, and publication choices neutrally. Do not invent
   an MSP path or annotation evidence.
8. Run the zero-threshold diagnostic. Let Interactive select the representative
   mid-run QC, estimate a 3,000-6,000-peak stepped threshold, and retain
   `TimeBasedLinearWeightedMovingAverage`. Show the representative, diagnostic
   count, step, and threshold before production.
9. Treat the EquiSPLASH declaration only as internal-standard evidence. Do not
   invent exact target ions or RT values without review.
10. Preview and confirm the production command, run one unit, and retain its
   exact job ID.
11. Validate and preview the job-owned mzTab-M output, generate scientifically
   evaluable QA, generate the Materials and Methods and supplementary workbook,
   and create the data-mining handoff.
12. Keep the downloaded raw data. Do not call a cleanup tool.

## Acceptance report

Write `feedback/claude-mpst000007-smoke-2026-09-02.md` with:

- selected accession, unit ID, technical signature, sample/file counts, bytes,
  Class hierarchy, and local paths;
- every confirmation obtained;
- download, checksum, allow-list, raw-header, mapping, MS-DIAL, mzTab-M, QA,
  publication, and handoff outcomes;
- exact job IDs and artifact inventory;
- PASS/FAIL for analysis-unit isolation and end-to-end execution;
- reproducible defects for Codex, with severity, observed/expected behavior,
  affected tool, and an acceptance test.

Write the report even if execution stops early. After saving it, you may compare
it with the earlier audit and clearly label that comparison as post-audit.
