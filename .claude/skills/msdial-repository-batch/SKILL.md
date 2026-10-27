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
4. Use the workspace root chosen for this campaign explicitly as
   `workspace_root` for every repository plan and download preview, and as the
   runner's `--workspace-root`. MB-POST production uses
   `F:\MSDIAL_Public_Reanalysis\analysis`; `D:\13_MSDIAL_Public_Reanalysis\analysis`
   is the archive of the pilots and preflights, not a default for new work. Any
   other root needs the user's explicit confirmation. Never derive the root from
   the Claude project directory, and never use one on C:, in the user profile,
   in a temporary folder, or inside a code checkout.
5. Classify units as eligible, excluded, or requiring review using the supported
   scope in `CLAUDE.md`: untargeted LC-MS/MS acquired by DDA or DIA/AIF/SWATH.
   If acquisition is unknown, require raw-header preflight; never guess DDA.
   The preflight reads the headers with the raw-metadata extractor, which a
   campaign pins, and a unit whose mode is still unknown after it is not run.
   In a campaign the header decides even where the repository declares a mode
   (rule B2, 2026-10-06): the declaration is the Catalog's keyword inference,
   and the extractor's `confidence` is a constant per heuristic branch, not a
   probability. A unit runs without the
   files whose header left the mode unknown: its `campaign_disposition` lists
   each in `excluded_inputs` with the reason (`acquisition_unresolved`, or
   `raw_header_unreadable` or `raw_header_unsupported_format` for a header no
   reader opened), and INP-1, CNT-1 and SPL-1 count it as excluded, not as a
   lost sample. Where the preflight read an input, its CSV row's
   `acquisition_type` takes that input's `console_acquisition_type` (DDA,
   SWATH or AIF) and no other, and a mode the repository declared is written
   as DDA, SWATH or AIF, never DIA: the Console silently turns any value it
   cannot parse into DDA. The rest of the scope is in `CLAUDE.md`, Supported
   production scope and Evidence and decisions: AIF by its recorded collision
   energies, ion mobility from unit-level evidence, vendor folders and
   archives, mzXML conversion in a campaign, the copies inside one per-sample
   archive, the exclusion of a unit whose declared inputs are only partly
   openable, declared-"Both" units kept for a later stage, and the user's one
   encoding rule of 2026-10-09: of one sample's readable encodings exactly
   one runs, vendor before mzML before mzXML, and every file not used is
   recorded (`manifest.encoding_choices`). Interactive's plan still excludes a
   unit that names an `.mzXML` (outside a campaign) or an `.mzData` even where
   that sample has a readable encoding; report such an exclusion as the
   implementation gap `CLAUDE.md` names, not as policy.
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
   tools, unless the approval is a recorded campaign approval of the manifest's
   digest (`CLAUDE.md`, Confirmation boundaries): the user records it with
   `scripts/campaign-runner.py approve`, and for the units it lists that record
   is those confirmations, with no per-unit confirmation, and the campaign
   runner carries it (see Unattended operation). The production campaign still
   starts only on the user's explicit go, asked for in the conversation once
   everything is ready ("本番開始前には私がGoサインを出すので、聞いてください。",
   2026-10-01): no approved manifest, pilot approval or authorization record is
   that go. Show the person the plan summary before the approval: its
   workspace root, raw retention, libraries and automatic RT correction
   statement. A manifest that carries no automatic RT correction statement is
   refused and planned again (the user's second-round answer 4 of 2026-10-08).
10. Call `msdial_repository_reanalysis_plan` and
   `msdial_download_repository_raw` with the same `analysis_unit_handoff_path` for
   each approved unit. For accession-bundle downloads, verify the resulting
   manifest admits only allow-listed paths into the analysis CSV. The lease
   extracts zip, tar, `.7z`, `.rar`, bare `.gz` and `.lzma` objects and records
   each extraction with its member listing (`archive_extractions`). An object
   that several units list is fetched once, into the accession's download
   store `_dl`, with each unit reading its own tree of it, and the runner
   claims it for every sibling unit so that one unit's cleanup does not
   collect it (`CLAUDE.md`, Raw-data retention; pending the gate's
   truthful-libraries change).
   When raw-header preflight reports `Mixed`, the unit holds more than one
   acquisition mode and cannot run as one. Preview `msdial_split_repository_unit`
   with `confirmed=false`, show the parts, and split only on an explicit
   confirmation. Under a campaign approval that covers the split, split instead
   as the unit's `campaign_disposition` says (`disposition: split`, with its
   `split_key`). Never run the Mixed parent. Each part is its own run, with its
   own workspace, preflight, diagnostic, gates and boundary-4 confirmation (in a
   campaign, the approval's); the parts share the parent's `raw\`, and the
   parent stays the raw owner.
11. Before production, run `msdial_start_peak_count_diagnostic` on a mid-run QC,
   or the non-blank Sample nearest the run-order midpoint when there is no QC.
   Interactive picks one itself only from `analytical_order`. Since 0.5.2,
   `msdial_prepare_repository_reanalysis` ranks a unit's files by the
   acquisition start time each raw header records, whenever every file has one,
   and records it as `analytical_order` in the unit's run manifest; the guided
   plan then reports `derived_from: "raw_header_acquisition_start_time"`, and
   ORD-1 passes only if the CSV carries exactly that order. When the plan
   instead reports `"listing"` or `"embedded"`, the order is the file listing or
   a number read out of the file names (with every Blank and QC placed after the
   samples), not the run: say so, order the files by the raw headers'
   `acquisitionStartTime` yourself, and pass the choice as
   `representative_file`. A `file_type` Standard is not a Sample. Call
   `msdial_estimate_peak_height` for the default 3,000-6,000 range and leave
   `threshold_step` at 0. Since Interactive 0.5.28 the step comes from the
   representative file's `instrument_family`: 100 for QTOF-type data and 1,000
   for Fourier-transform data (Orbitrap-class, FT-ICR), read from the vendor
   format or the mzML header, and from the unit's declared instrument only
   where the file names none (`instrument_family_source: format_default`). That
   family step is always the coarse step. Of its multiples the estimate takes
   the highest whose estimated count is still at least 3,000, the lower end of
   the range. Only when no multiple lands in range does it fall back to the fine
   step, 10 (QTOF-type) or 100 (Fourier-transform) and never finer, and say
   `step_fallback: true` with its `fallback_reason`. A `threshold_step` you pass
   is only recorded (`requested_threshold_step`, with a warning), never
   searched, so it cannot make a unit Fourier-transform data. When the raw
   header or sample metadata shows FT data that the estimate's
   `instrument_family` does not, report the disagreement to the user rather
   than passing a step. When `within_target_range` is false, show the
   estimate's `warnings`. Add the accepted threshold to the answers and
   preserve `TimeBasedLinearWeightedMovingAverage`.
12. Write the production bundle with `msdial_prepare_guided_analysis`, with the
   accepted `minimum_peak_height` in the answers, then run the
   `before-production` gate (see below) against it and stop the unit on a
   refusal. In a campaign (`CLAUDE.md`, Gate verdicts in a campaign), a FAIL
   stops it only in a `blocks_run` check (ELIG-1, ACQ-1, SUM-1, CNT-1, INP-1,
   ID-1, PRE-2 and CONV-1; LIB-2 joins them once the gate's truthful-libraries
   change merges), and so does one of them left `not_evaluable` where
   it is required: the unit is then a failed unit, retried and its raw data
   deleted as the campaign's rule for one says. A FAIL in a `record_only` check
   (CLS-1, CLS-2, CLS-3, ORD-1, PKH-1, SPL-1, PAIR-1 and PRE-1, which never
   FAILs) is
   recorded, and the unit runs, as it does past a check left `not_evaluable`
   where it is not required. A gate that gives no usable report holds the unit
   unrun, its raw data kept and nothing counted, until a recheck gives one; the
   other units go on. The gate reads `output\method.txt`, so it cannot run
   before this.
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
means no evaluated check failed, 2 means at least one failed, 3 means the
workspace is unusable, and 4 (`--strict` only) means a check could not be
evaluated because an artifact that stage was responsible for producing is
absent. `--json` emits the full report. Exit 0 includes WARNs: report them, and
do not call a unit ready to publish while QA-1 WARNs. QA-1 passes only when the
QA texts carry Interactive's own statement for the assessment and nothing else;
its WARN quotes every other QA sentence for a person to read. Those sentences,
and the checksum phrasings SUM-2 sets aside, are held by READ-1 (exit 4 under
`--strict`) until a person's reading is recorded. Show the person the output of
`scripts/record-reading.py <unit-workspace> --check QA-1` (or `SUM-2`), and
only after they have read the sentences and said what they found, record it
with `--digest <the digest shown> --by <that person> --conclusion
accepted|rejected`. Never record a reading on your own: that is confirmation
boundary 6. A rejected reading is a FAIL until the text is corrected, or until a later
reading of the same sentences accepts them. A text the gate could not read holds the run
too, until it is made readable.

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
In a campaign, what stops a unit is decided instead by `CLAUDE.md`, Gate
verdicts in a campaign: there a `blocks_run` check left `not_evaluable` where
it is required stops the run as its FAIL does, and any other check left
`not_evaluable` is recorded with the unit, and the unit runs.

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
  only record that a unit was selected; for a campaign that is the runner's
  ledger under `<workspace_root>\_campaigns\<campaign_id>`. The server's job
  registry keeps only the most recently updated jobs and downgrades running
  jobs on restart.

Server-side state on `msdial_interactive_app` `main`:

- The peak-count diagnostic writes to `<workspace>\diagnostics\<job>` (PR #16)
  and does not touch `output\analysis_files.csv`, `output\method.txt` or
  `output\run-manifest.json`. Seen on real data on 2026-09-25 (MTBLS2207): the
  production CSV kept its rows, the estimate landed in
  `provenance\run-manifest.json`, and the Console left its `.dcl`, `.pai2` and
  `_tags.xml` beside the representative raw file, as it does on every run.
  Verify the row count anyway; that is what CNT-1 is for.
- Raw data is never deleted without a separate explicit confirmation, or a
  campaign approval that covers boundary 5 for the unit. Outside a campaign,
  keeping `raw_retention_policy` at `keep` is still the right default for an
  unattended run, because it removes the decision rather than answering it. An
  accepted retention policy records intent and is not that confirmation:
  preview `msdial_cleanup_repository_raw`, show the exact directory and the
  retained inventory, and ask immediately before deleting. Given
  `campaign_authorization_path`, the tool deletes without `confirmed=true` only
  when the approval covers boundary 5 for the unit and states
  `delete_after_validated_output`, the unit's manifest recorded that same
  policy at download, and the preview is ready; it writes the crossing into the
  manifest first. For a campaign unit, the finalised run's MS-DIAL containers
  are moved into `output\msdial-intermediates`, and its loaded-library copy is
  kept once per library set in the root's `_library_copies` store; a container
  that could not be moved is recorded in `finalisation_holds` and holds the
  deletion. `discard_download_lease`, which releases a failed, skipped or
  excluded unit's raw data, takes the campaign approval too: it checks
  boundary 5 and records the crossing itself, and it keeps a failed unit's
  mzTab-M as a failure artifact. A split parent's raw tree, which its parts
  share, is released once every part has ended. With an Interactive that lacks
  the approval-taking discard, a `confirmed=true` discard is a fallback that
  waits for the user's approval, made only where the approved manifest's policy
  states `confirmed_discard_fallback: true` (`CLAUDE.md`, Raw-data retention);
  without it those raw data stay. A unit Interactive's disposition holds is
  never discarded unless an operator's skip passes `release_disposition_hold`,
  and that skip is made only on the user's explicit word for the unit
  (`CLAUDE.md`, Held by Interactive). A `recheck-held` deletes nothing and
  needs no OK.
- A unit whose manifest says `execution_allowed` is not true is refused before
  MS-DIAL starts, and so is a workflow whose polarity, output directory or input
  set disagrees with the manifest.
- A run that returns success without producing every planned export now fails.
- A rejected tool call answers with `ok: false`, a `reason` and the backend's own
  message instead of raising past the MCP boundary.

## Unattended operation

An unattended run may only proceed inside a manifest the user has already
approved. The runner's `run` (`--until-idle` included) is never started or
scheduled on the production campaign before the user's explicit go, asked for
in the conversation (`CLAUDE.md`, Confirmation boundaries, 2026-10-01); an
approved digest is not that go. For a campaign, that approval is the recorded
campaign approval of the manifest's digest, which the user records with
`approve` (`CLAUDE.md`, Confirmation boundaries), and the execution path is the
campaign runner, `scripts/campaign-runner.py`, as `CLAUDE.md`, MCP
responsibilities, describes it. The runner never records a reading
(boundary 6), and it decides no eligibility: it acts on the unit's
`campaign_disposition`, which only Interactive's `classify_preflight` writes.

Outside a campaign, an unattended run may not widen `maximum_gb` to clear a
size blocker. In a campaign there is no per-unit size limit (the user's
decision of 2026-09-30): `maximum_gb` is the disk bound, the free space above
the reserve that the approved manifest's policy states, as the runner computes
it, and an unattended run may not raise it above that bound. The approval
record states no size, so the runner passes the bound on every download; left
out, Interactive's default of 20 GB applies. An unattended run may not set
`allow_partial_mapping=true`, and may not change `raw_retention_policy`. When
the loop reaches a decision that needs a confirmation it does not hold (an
official-library download, or a unit the approval does not list), stop the
unit, write a failure record beside its artifacts, and continue with the next
unit; do not proceed. A pin that changes pauses the whole campaign until a new
approval is recorded. A failed unit is retried only as the approved manifest's
policy states (twice, in this campaign), never blindly, and one unit's failure
never stops the campaign.

In a campaign the runner runs the gate at each of its points and keeps every
report; what a verdict does there is `CLAUDE.md`, Gate verdicts in a campaign
(step 12). A gate that gives no usable report holds the unit in `gate_held`,
an Interactive reply the runner cannot read holds it in `contract_held`, and
`scripts/campaign-runner.py recheck-held` asks for a recheck at once. A stop is
per unit and never stops the runner. A pin change, a short disk, a repository
outage and an Interactive backend that does not answer pause the whole
campaign instead, each lifting by itself. Read which checks failed, and which
were left unevaluated, from the `checks` of the `--json` report, never from
the exit code: it is 2 for any FAIL, in either list, however many required
checks were left unevaluated beside it (`strict_failures`, `run_blocked_by`).
Report a unit whose outputs are all present and whose mzTab-M validates as
`outputs produced`, not `completed`: a run is completed only when it reaches
B10 and its gate, run with `--stage all --strict`, exits 0, which a run whose
QA-1 or SUM-2 left sentences does only once a person's reading is recorded.

Record `manifest_path`, `output_root`, `input_path`, the `analysis_unit_id` and
every job ID into the batch's own state file (for a campaign, the ledger) as
each tool returns. The server's job registry keeps only the most recently
updated jobs and downgrades running jobs to `interrupted` on restart. The
preflight, split, preparation, peak-height estimate, QA, publication and cleanup
tools accept the unit's `manifest_path` as a way back into it, so that is the
handle to keep above all.

Treat any tool result that is not a mapping, or that is a string beginning
`Error executing tool `, as an opaque server failure: stop that unit and write
the failure record. The runner, which calls the functions in-process, treats an
exception they raise the same way. Outside a campaign, do not retry: the reason
is not recoverable client-side. Inside one, the retry rule above applies.
