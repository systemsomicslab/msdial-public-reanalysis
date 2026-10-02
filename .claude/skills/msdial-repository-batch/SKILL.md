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
   The preflight reads the headers with the raw-metadata extractor, which a
   campaign pins, and a unit whose mode is still unknown after it is not run.
   In a campaign, a unit the repository does not declare runs without the
   files whose header left the mode unknown: its `campaign_disposition` lists
   each in `excluded_inputs` with the reason (`acquisition_unresolved`, or
   `raw_header_unreadable` or `raw_header_unsupported_format` for a header no
   reader opened), and INP-1, CNT-1 and SPL-1 count it as excluded, not as a
   lost sample. Where the preflight read an input, its CSV row's
   `acquisition_type` takes that input's `console_acquisition_type` (DDA,
   SWATH or AIF) and no other, and a mode the repository declared is written
   as DDA, SWATH or AIF, never DIA: the Console silently turns any value it
   cannot parse into DDA. An AIF file whose collision-energy target list is
   empty gets a recorded warning and still runs. Ion-mobility data are
   excluded, with the reason recorded (LC-MS only).
   mzML is an input, and so is a vendor folder (Waters `.raw`, Agilent or Bruker
   `.d`): one folder is one input and one CSV row, and its files are only
   downloaded. Outside a campaign, mzXML and mzData are `requires_conversion`:
   MS-DIAL has no reader for them, so the unit is excluded before download
   until a reviewed ProteoWizard conversion has produced an mzML manifest with
   its own provenance. The exclusion is unit-wide: one listed file or one
   sample naming an `.mzXML` excludes the unit, even when its other inputs are
   readable. In a campaign, an mzXML that is the encoding a sample is analysed
   by (a vendor container and mzML outrank it; it outranks a twin nothing
   reads, such as a `.dat`) is converted to mzML by Interactive's converter
   with every inference off but a polarity imputed from the unit's one
   declared ion mode (`CLAUDE.md`, Supported production scope), and the
   conversion is recorded as that input's
   provenance; until the unit's manifest records it, the mzXML stays
   `requires_conversion`, and a file whose conversion fails is excluded with
   its reason while the rest of the unit runs. mzData stays
   `requires_conversion` and excludes the unit: there is no reader and no
   converter for it. Interactive main (0.5.19) takes neither a folder nor a
   converted mzXML yet: it refuses a folder input with the production Console,
   and it still excludes an mzXML unit at eligibility, before download,
   because the lease does not run the converter.
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
   digest (`CLAUDE.md`, Confirmation boundaries): for the units it lists, that
   record is those confirmations, and the campaign runner carries it (see
   Unattended operation).
10. Call `msdial_repository_reanalysis_plan` and
   `msdial_download_repository_raw` with the same `analysis_unit_handoff_path` for
   each approved unit. For accession-bundle downloads, verify the resulting
   manifest admits only allow-listed paths into the analysis CSV. The lease
   extracts zip, tar, `.7z`, `.rar`, bare `.gz` and `.lzma` objects and records
   each extraction with its member listing (`archive_extractions`). An object
   that several units list is to be fetched once, into the accession's download
   store `_dl`, with each unit reading its own tree of it; the lease does not
   use the store yet.
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
   `msdial_estimate_peak_height` for the default 3,000-6,000 range with the
   step for the instrument: the inspection calls every mzML `QTOF`, so pass
   `threshold_step=1000` when the raw header or sample metadata shows
   Fourier-transform data (Orbitrap, FT-ICR). Add the accepted threshold to the
   answers and preserve `TimeBasedLinearWeightedMovingAverage`.
12. Write the production bundle with `msdial_prepare_guided_analysis`, with the
   accepted `minimum_peak_height` in the answers, then run the
   `before-production` gate (see below) against it and stop the unit on a
   refusal. In a campaign (`CLAUDE.md`, Gate verdicts in a campaign), a FAIL
   stops it only in a `blocks_run` check (ELIG-1, ACQ-1, SUM-1, CNT-1, INP-1,
   ID-1, PRE-2 and CONV-1), and so does one of them left `not_evaluable` where
   it is required: the unit is then a failed unit, retried and its raw data
   deleted as the campaign's rule for one says. A FAIL in a `record_only` check
   (CLS-1, CLS-2, CLS-3, ORD-1, PKH-1, SPL-1 and PRE-1, which never FAILs) is
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
  are moved into `output\msdial-intermediates` and its
  `<project>_Loaded.msp2.dbs` is deleted when the run is finalised; a container
  that could not be moved is recorded in `finalisation_holds` and holds the
  deletion. Three parts of the campaign's deletion rule have no approval-taking
  entry point on main yet. `discard_download_lease`, which releases a failed,
  skipped or excluded unit's raw data, takes only `confirmed`. The runner is to
  use its approval-taking form, which checks boundary 5 and records the
  crossing itself; until that lands, calling it with `confirmed=true` after
  `campaign_authorization.authorize` has accepted boundary 5 for the unit and
  the crossing is recorded in the unit's manifest is a fallback that waits for
  the user's approval, made only where the approved manifest's policy states
  `confirmed_discard_fallback: true` (`CLAUDE.md`, Raw-data deletion in a
  campaign); without it those raw data stay. The discard on main also refuses
  any unit whose output holds an mzTab-M, and cleanup accepts only a validated
  run, so neither can delete the raw data of a failed unit whose mzTab-M did
  not validate, or whose run left one while missing another planned export:
  those raw data stay, with the refusal recorded as the reason, until the
  approval-taking discard accepts such a unit and keeps its mzTab-M as a
  failure artifact. A split unit cannot be cleaned up: its parts share the
  parent's raw tree, which lies outside a part's workspace, and the parent is
  not a completed run, so the tool refuses both, and its raw data stay until
  split-parent release exists.
- A unit whose manifest says `execution_allowed` is not true is refused before
  MS-DIAL starts, and so is a workflow whose polarity, output directory or input
  set disagrees with the manifest.
- A run that returns success without producing every planned export now fails.
- A rejected tool call answers with `ok: false`, a `reason` and the backend's own
  message instead of raising past the MCP boundary.

## Unattended operation

An unattended run may only proceed inside a manifest the user has already
approved. For a campaign, that approval is the recorded campaign approval of
the manifest's digest (`CLAUDE.md`, Confirmation boundaries), and the execution
path is the campaign runner, `scripts/campaign-runner.py`. It calls the
Interactive and Catalog functions the MCP tools expose, in its own process, and
where a tool would ask for `confirmed=true` it passes the approval instead:
`campaign_authorization_path` to Interactive, `ratification` to the Catalog. No
step the approval covers acts before the approval has been checked for that
unit and that boundary with Interactive's validator
(`campaign_authorization.authorize`) and the crossing recorded. Interactive
makes that check itself wherever it takes the approval. The Catalog checks only
a ratification's form, never the approval it names or the unit it is for, so
the runner validates boundary 3 for the unit before every Class save. The
discard of a failed, skipped or excluded unit goes through Interactive's
approval-taking discard; until that lands, the runner's `confirmed=true` call
after the same validation for boundary 5 is a fallback that waits for the
user's approval, and the runner makes it only where the approved manifest's
policy states `confirmed_discard_fallback: true`. The runner never records a
reading (boundary 6). It decides no eligibility: it reads the unit's
`campaign_disposition`, which only Interactive's `classify_preflight` writes,
and runs, splits, skips or excludes the unit as that record says.

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
report for the verification that follows the campaign. A verdict never holds
the raw-data deletion (the user's decision of 2026-09-30), and at
`before-production` a FAIL holds the run only in a `blocks_run` check, and
so does such a check left `not_evaluable` where it is required (the user's
decisions of 2026-10-01 and 2026-10-02; step 12 and `CLAUDE.md`, Gate verdicts
in a campaign). A FAIL in a `record_only` check and any other check left
`not_evaluable` are recorded and the unit runs. A gate that gives no usable
report holds the unit in `gate_held`. That is no answer within the runner's
time limit, a crash or an exit code other than 0, 2, 3 and 4, exit 3, output
that does not parse, or a gate the runner could not start. The held unit is
not run, keeps its raw data and is counted neither as a retry nor as a
failure. It is warned about in the ledger and the status export while the
other units go on. The runner runs the gate again for it at each start and
every few hours, and `scripts/campaign-runner.py recheck-held` asks for that
at once. After the run a missing report is only recorded. A stop is per unit
and never stops the runner, which goes on with the other units; a pin
change, a short disk and a repository outage pause the whole campaign
instead, and each pause lifts by itself. Read which checks failed, and which
were left unevaluated, from the `checks` of the `--json` report, never from
the exit code. It is 2 for any FAIL, in either list, and a FAIL outranks a
strict refusal, so it is 2, not 4, however many required checks were left
unevaluated beside a `record_only` FAIL; the report lists those in
`strict_failures`, and `run_blocked_by` names every check that stops the run.
The report states each check's list as `run_policy`, and the runner holds the
`blocks_run` list itself whatever a report states.
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
