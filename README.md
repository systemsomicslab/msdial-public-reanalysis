# Claude Code MS-DIAL repository reanalysis workspace

This local workspace connects Claude Code to two independently testable MCP
servers:

- `msdial-repository-catalog`: selects repository **analysis units** and exposes
  technical, biological, file, sample, and provenance metadata.
- `msdial-interactive`: downloads raw data after confirmation, prepares one
  MS-DIAL run, executes MS-DIAL Console, validates mzTab-M, performs QA, and
  creates publication artifacts.

The first use is an audit, not a production analysis. Run Claude Code from this
directory and paste the contents of `prompts/01-compatibility-audit.md`. Use
`prompts/03-mpst000007-end-to-end-smoke.md` for one confirmation-gated test run,
then `prompts/02-mbpost-000001-000010-pilot.md` for a larger batch.

For a new Codex task, Claude Code session, or additional development agent,
start with the master prompt in the private repository
`systemsomicslab/msdial-family-dev-context`. It records the
cross-repository architecture, local SDK and demo-data map, scientific
invariants, validation strategy, private-resource boundaries, and definition of
done needed to continue MS-DIAL family development without relying on one long
chat history. Clone it beside this repository:

```text
D:\13_MSDIAL_Public_Reanalysis\
  code\      this repository (public)
  context\   systemsomicslab/msdial-family-dev-context (private)
  analysis\  archive of the pilots and preflights, never committed
```

It is a separate private repository because it documents licensed vendor SDK
install locations and private MSP library paths on the development machine.

## Current local paths

- Claude workspace: `D:\13_MSDIAL_Public_Reanalysis\code`
- Repository reanalysis data: the workspace root chosen for each campaign and
  passed explicitly (below); `D:\13_MSDIAL_Public_Reanalysis\analysis` is the
  archive of the pilots and preflights
- Interactive: `D:\0_SourceCode\msdial_interactive_app`
- Catalog: `D:\0_SourceCode\msdial_repository_catalog`
- Full catalog: `D:\0_SourceCode\msdial_repository_catalog\catalog-data\native-smoke.sqlite`
- Python: detected by `setup-windows.ps1`, or supplied with `-Python`

The Claude instructions and launcher are stored under `code`. Repository
downloads, MS-DIAL processing files and generated results go under the
workspace root of the campaign they belong to. The root is chosen per campaign
and never assumed: production runs on a root of its own, and
`D:\13_MSDIAL_Public_Reanalysis\analysis` keeps the pilots and preflights run
before it, with their ledgers, as an archive. Pass the root explicitly every
time: as `workspace_root` to every `msdial_repository_reanalysis_plan` and
`msdial_download_repository_raw` call, and to `scripts/campaign-runner.py` as
`--workspace-root` (or through `MSDIAL_REPOSITORY_WORKSPACE_ROOT`). Reading an
archived campaign means naming the archive root. Wherever it is, the root is not
on C:, in the user profile, in a temporary folder, or inside a code checkout.
The runner refuses a root that is not an absolute path, is a network or device
path (`\\server\share`, `\\localhost\C$\...`, `\\?\...`), lies on a mapped
network drive or lies inside a code checkout. It does not refuse C:, the user
profile or a temporary folder: its plan summary, the text a person approves,
names the root and the raw retention, and says in capitals where the root is
on the system drive, in the user profile or in a temporary folder, so the
person approving confirms such a root knowingly or plans again.
`setup-windows.ps1` checks only the root it is given, which it creates; no
campaign reads that root, so it protects no campaign's.
The `MSDIAL_REPOSITORY_WORKSPACE_ROOT` of the Interactive MCP server in
`.mcp.json` bounds a Claude session to one root: a session that works on another
root needs that value changed and the server reconnected. Each unit gets:

```text
<workspace_root>\<repository>\<accession>\<analysis-unit-id>\
  raw\downloads\
  raw\data\
  provenance\
  output\
```

The full catalog observed on 2026-09-02 contained 5,601 studies, 9,855 analysis
units, 1,051,091 sample records, and 365,276 raw-file records.

## Setup

1. Open PowerShell in this directory.
2. Copy `.mcp.example.json` to `.mcp.json` and replace the example paths with
   paths on the analysis PC. The local `.mcp.json` is intentionally ignored by
   Git because it contains machine-specific paths.
3. Run `powershell -ExecutionPolicy Bypass -File .\setup-windows.ps1
   -ReanalysisRoot <workspace root>`, adding `-InteractiveRoot` and
   `-CatalogRoot` when the defaults do not match the analysis PC. The root has
   no default; the script refuses one on C:, in the user profile, in a
   temporary folder, or inside a code checkout, and takes only a folder on a
   mounted drive of this PC named by its letter (`X:\...`), never a network or
   device path (`\\server\share`, `\\?\...`) or a mapped network drive.
4. Install and authenticate Claude Code if `claude --version` is unavailable.
5. Run `claude` from this directory, or double-click `Start Claude Code.cmd`.
6. Approve the two project-scoped MCP servers when Claude Code asks.
7. Start with `/msdial-repository-batch audit` or paste the audit prompt.

On the current PC, Claude Code `2.1.258` was installed successfully on
2026-09-02. `claude doctor` reported no installation problem, but Claude account
authentication was still required before the first independent audit.

Claude Code reads project MCP configuration from the untracked `.mcp.json` and
project skills from `.claude/skills/`. Project MCP configuration requires an
explicit trust decision in Claude Code. Audit outputs under `feedback/` and
generated status pages under `reports/` are also local by default; only the
review template is version controlled.

### Skills

This repository keeps one skill, `msdial-repository-batch`, for repository
batches.

The laboratory lipidomics skill, `msdial-lipidomics-workflow` (internal
standards, normalization to concentration, and the positive/negative merge),
has moved to MS-DIAL Interactive, which is now its canonical home:
`skills/msdial-lipidomics-workflow/` in
[systemsomicslab/msdial-interactive-app](https://github.com/systemsomicslab/msdial-interactive-app),
since [msdial-interactive-app#74](https://github.com/systemsomicslab/msdial-interactive-app/pull/74).
Interactive's setup script (`Setup MS-DIAL for Claude.cmd`, or
`scripts/setup_claude.py`) installs it into `~/.claude/skills/` for Claude Code
and packages it for Claude Desktop, and its tutorial is
`docs/tutorials/lab-lipidomics.md` there. A copy installed earlier as a link to
this repository's `.claude/skills/msdial-lipidomics-workflow` no longer resolves
once this checkout is updated; run Interactive's setup script to replace it.

## Safety boundary

Metadata inspection and dry-run planning may proceed without a download. The
following actions require a separate explicit human confirmation:

- contacting a repository for a raw-data download;
- downloading an official annotation library;
- accepting a Class proposal, or an abstention where no declared factor groups
  the samples;
- starting each production MS-DIAL run;
- deleting downloaded raw data;
- recording a person's reading of the sentences a gate check left for them
  (`scripts/record-reading.py --digest`), only after that person has read them
  and said what they found.

The default retention policy is `keep`. Never combine different analysis units
in one MS-DIAL run. Never redistribute the laboratory's private MSP libraries.

## Run gates

Every stage of this pipeline reports success on its own terms. Several observed
failures produced a complete, self-consistent, publishable result set that was
not the study anybody approved: a tuning diagnostic that rewrote the production
analysis CSV to one row, a QA verdict computed against an injection order
synthesized from file order, an exported mzTab-M whose whole evidence section
was one column wider than its header.

`scripts/verify-run-invariants.py` checks the invariants those failures break.
It reads the retained artifacts rather than tool responses, because the response
that reports the same sample count is large enough to be truncated in transport
and its `blockers` field arrives after the payload. A check it cannot evaluate
reports `not_evaluable`, never `pass`.

```powershell
python .\scripts\verify-run-invariants.py <unit-workspace> --stage before-production --strict
python -m unittest discover -s tests -t tests
```

Stages are `before-production`, `after-run` and `before-publish`, or `all`. Exit
code 0 means no evaluated check failed, 2 means at least one failed, 3 means
the workspace is unusable, and 4 (`--strict` only) means an artifact that stage
owed is absent. Exit 0 includes WARNs, which a person reads before publishing.
QA-1 passes only when the QA texts carry Interactive's own statement for the
assessment and nothing else; any QA sentence edited or added is a WARN that
quotes it. Those sentences, and the checksum phrasings SUM-2 sets aside, hold
the run under `--strict` (READ-1, exit 4) until a person's reading of them is
recorded: `python .\scripts\record-reading.py <unit-workspace> --check QA-1`
shows them and their digest; once the person has read them and said what they
found, the same command with `--digest`, `--by` and `--conclusion` records it
in `provenance/readings.json`. A changed text is read again. Always pass `--strict`: without it a workspace where nothing has
happened exits 0. The one report whose exit code is not a verdict is a split
unit's raw owner at `before-publish`: it is not a run and owes no run artifacts,
so read its DSK-1 from it and ignore its exit code. Run the gate; do not infer
safety from a tool reporting no blockers.

Each before-production check carries a `run_policy` in `--json`. It is
`blocks_run` where its FAIL stops a campaign unit's MS-DIAL run, so the unit
counts as failed, and `record_only` where the FAIL is recorded and the unit
runs. The user decided it: a FAIL stops the run only for the checks that break
the MS-DIAL results (2026-10-01), and every before-production check is placed
(2026-10-02). ELIG-1, ACQ-1, SUM-1, CNT-1, INP-1, ID-1, PRE-2 and CONV-1 are
`blocks_run`, and so is LIB-2 (2026-10-24): in a campaign unit, every library
`method.txt` and its MSP annotator settings load must be one the approval
names, with the sha256 it names, and what the unit's answers name for its
role; the names must agree across `method.txt`, `workflow-settings.json` and
the run manifest; every MSP annotator row must state its own
`target_omics`; and what the method annotates with must be what the approval
states for the unit's ion mode (review of 2026-10-25): the annotation profile,
the LBM's score cutoffs and priority, and each MSP annotator row's library,
priority, tolerances, score cutoffs, RT use, target omics, library kind and,
where the approval states it, suggestions. Since 2026-10-28 the pin of an MSP
the profile names in `libraries.msp_paths` or `libraries.in_silico_msp_paths`
carries its library kind (`acquired`, or `predicted` for the in-silico one),
and the method must search it as that kind; the method's Target omics is held
to what Interactive applies: a `workflow_overrides` `target_omics`, else
`target_omics_pin`, else the `target_omics` answer. The
plan writes that annotation contract into the manifest and the authorization,
read from the pinned Interactive, with the profile's retention-time answers
applied to the MSP rows as Interactive applies them, and refuses a tiered
profile whose LBM2 the pinned Interactive would replace with its official one,
or a strategy that loads a library the profile does not name (the official
strategy, or the tiered one given no `libraries.lbm_path`). It names files,
never their locations, and does not apply to a unit without
`provenance/campaign-authorization.json`. LIB-2 reads the method Interactive's
prepare wrote, and Interactive's start writes it again; the parameter template
its LBM cutoffs come from (the one Interactive's settings name, else its own)
is pinned by sha256 with the code and the libraries, and the runner compares
the pins before each unit's prepare and each Console start, so a template
changed during a campaign pauses it instead of failing every later unit or
running unseen. CLS-1, CLS-2, CLS-3,
ORD-1, PKH-1 and SPL-1 are `record_only`,
and so are PRE-1, which never FAILs, and PAIR-1 (2026-10-06), which lists as a
WARN every input the lease paired with a declared raw file by inference
(`input_names_paired_by_inference`), so each such pairing is on record; a split
part lists only its own inputs' pairings. ACQ-1 follows rule B2 (2026-10-06): a
row FAILs where it runs as another type than its file's header alone gives
(`header_console_acquisition_type`), at any confidence, and where an MS1-only
file was folded into DDA in a unit declared DIA, AIF or SWATH. Its table of
sanctioned header-to-row mappings holds one entry (2026-10-07): an AIF header
may run as SWATH where the binding disposition's `aif_run_as_swath` names the
rule `single_ce_aif_as_swath_2026_10_07` and exactly one collision energy, and
the file's record says that rule decided it (`console_acquisition_basis`
`aif_single_ce_as_swath`); such a row is a WARN naming the rule, and a record of
more than one energy sanctions nothing. A multi-energy AIF unit runs as AIF, not
as SWATH, on a Console with MsdialWorkbench #825 (Interactive 0.5.34), which
deconvolutes each energy and represents each peak by the energy of its MS/MS
reference-spectrum match, else the energy with the most product ions. ACQ-1
PASSes such rows only where the binding disposition records `aif_multi_ce_run`
under the rule `multi_ce_aif_with_console_825` with two or more energies, each
row's record carries `console_acquisition_basis` `aif_multi_ce_console_825` and
those same `ms2_collision_energies`, and the Console the run manifest records
(`output/run-manifest.json`, `console.assembly_sha256`) is shown to have #825:
the disposition's `multi_energy_aif_console` probe found it in that assembly, or
the gate finds both of #825's markers there itself. A multi-energy AIF row on a
Console without #825, one whose disposition holds the unit or records no such
run, and one whose energies differ from the unit's or are unrecorded, FAIL.
Inputs whose energy sets differ run as they are, on record (the user's decision
of 2026-10-08; Interactive 0.5.36): where `aif_multi_ce_run` records
`energy_sets_differ` true, each row must record the set the disposition's
`aif_collision_energies_by_input` records for its input, within the unit's
energies. Those sets are keyed by each input's path relative to the manifest's
`input_directory` (`/`-separated, compared without case:
`path_relative_to_input_directory`, the second-round answer of 2026-10-08), so
`POS/QC_01.mzML` and `NEG/QC_01.mzML` are each held to their own set; a record
keyed by another scheme is not read. ACQ-1 is then a WARN that names each set and the decision
(`aif_energy_sets_differ_between_inputs`), which stops no run. Without that
record, differing sets still FAIL. A
single-energy AIF unit is still expected as SWATH whatever the Console.
Archive members of a unit-scoped
archive that no sample row pairs with, which Interactive 0.5.31 includes as
inputs of their own (`name_pairing.paired_by` `unattributed_member`, rule
`unit_scoped_archive_2026_10_07`), are explained inputs to INP-1, CLS-1, CLS-2,
CLS-3 and PAIR-1: each WARNs listing them with
`manifest.unattributed_members.count`, their rows carry the abstention Class or
`Unattributed`, and CLS-2 counts them as no approved sample (an approved sample
with no attributed input beside them is listed under
`samples_without_attributed_input`, not as missing or undelivered). INP-1 FAILs
them only from a download shared with other units, and PAIR-1 FAILs where their
record falls short of the rule. Interactive 0.5.36 converts an unpaired mzXML
in a campaign into an unattributed input (its lineage row the conversion, named
after the mzXML, listed in `unattributed_members.converted`), and records what
it leaves out of the unit in `unattributed_members.left_out` (an
opposite-polarity name, a shared archive's member, a member no declaration
names). INP-1 FAILs, blocking the run, where a member `left_out` names reaches
the run or a converted unattributed input is not the conversion of the mzXML it
names; PAIR-1 FAILs, on record only, where `converted` or `left_out` falls
short. Which one of a sample's encodings runs is the user's one rule of
2026-10-09 (`one_encoding_per_sample_2026_10_09`), which supersedes the
case-by-case answers of 2026-10-08 (the readable twin of an undecodable mzML,
`chosen_other_encoding`, a copy nearest the data root), whose checks are gone:
of a sample's readable files exactly one is used, the highest in the order
vendor format, mzML, mzXML; a tie goes to the first path without case; the next
is taken where the chosen one cannot be read or converted; the file used is that
sample's own input; and every other is recorded with its reason. The gate
recomputes the rule from Interactive's record (`manifest.encoding_choices` and
each used input's lineage `encoding_choice`): INP-1 FAILs, blocking the run, a
choice the rule does not make (a file passed over as unreadable must carry a
reason that applies to its encoding: `undecodable` an mzML's,
`conversion_failed`, `requires_conversion` and `polarity_contradicts_declaration`
an mzXML's, `incomplete_container` and a raw-header reason a vendor file's), a
choice missing from the lineage row or the manifest, a file left unused that
runs (in a split part, by its raw owner's choices too), a file used that runs
unattributed or as another sample than its rows, or a pairing rule's inference
(`name_pairing`, `inferred_name_pairings`), give it, a vendor file excluded for
its raw header with the sample's next encoding not taken
(`encoding_fallback_not_taken`), two files of one sample (one stem, polarities
that make one sample as Interactive merges them, so a path stating none is one
sample's with one stating the unit's own, or paired to one sample row whatever
their stems, by an exact name, a prefix or a leading identifier, or else as
the one row whose named file has its stem, in any encoding, under one
polarity; not two sample rows' own) that both run, an input beside a
lease-excluded encoding of its sample that no choice records, and a file of a
sample the unit records (a running input's, a candidate's of any choice, the
used file excluded or none used included, or an excluded file's) that an
archive member listing shows extracted, or a download that is no archive
delivered, and that no choice, input, exclusion or `left_out` record names,
nor, in a declared unit, leaves out by not declaring it (a plain download only:
an archive member no declaration names stays out on Interactive's `left_out`
record, `not_named_by_the_catalog_declaration`). A `left_out` entry takes
the rule's choice from a file the rule uses before the one of its sample that
runs only where its reason removes that file: a reason it could not be read
that applies to its encoding, with its exclusion or failed conversion on
record (`requires_conversion` outside a campaign), a polarity its path states
other than the unit's, a declaration that does not name it, or a stem two
sample rows share; any other reason (a shared archive's, none) is a FAIL.
A sample whose file used was excluded is named as running on no file. A
declared input the rule left unused is counted beside the candidates, not as
missing. CONV-1 FAILs a choice that uses an mzXML whose conversion did not
complete, or leaves one unused as `conversion_failed` whose conversion did.
PAIR-1 lists each choice (WARN) and FAILs, on record only, where the record does
not follow the rule. CLS-2 reads what the download
delivered from the archive member listings and the downloads, counting a
vendor folder fetched file by file (a Waters `.raw`) as one input and a
companion file (`.wiff.scan`) as none: an approved sample whose file was
delivered and left unpaired is a FAIL (`delivered_unpaired_samples`), one never
delivered a WARN (`undelivered_samples`). PKH-1 holds the step floor, never
finer than 10 for QTOF-type or 100 for Fourier-transform data, whatever family
step a diagnostic records. It takes the family from the file first, as
Interactive does: a family the diagnostic records from the vendor format or the
mzML header stands, and the Catalog's instrument text decides only where none
is recorded. It also reports the production run's `production_peak_counts` where
Interactive records them. The campaign ledger (schema 3 on) records the step an
estimate used (`threshold_step`), the family step it searched first
(`coarse_threshold_step`) and the fallback (`step_fallback`,
`fallback_reason`). The runner asks Interactive 0.5.28 for no step: the
family Interactive reads from the file decides it, the estimate is read against
that family's step, and a Catalog instrument text that would give another step
is a note on the `diagnosed` transition, never a hold. Only an Interactive
before 0.5.28, which searched the step it was asked for, is still asked at the
Catalog's step. A `blocks_run` check left not evaluable on
an artifact its stage owed, which `strict_failures` names, stops the run as its
FAIL does; one not evaluable where the stage owed nothing, such as INP-1 for a
unit that declares no analysis inputs, never does. Each check's docstring gives
its reason under "RUN POLICY", and moving a check is one entry of `RUN_POLICY`.
Checks of the later stages state no `run_policy`. `run_blocked_by` lists the
checks that stop the run, FAILed or unevaluated where owed. The campaign runner
reads these fields, and holds a unit unrun, its raw data kept and nothing
counted against it, while the gate gives it no report it can read before
production (`scripts/campaign/machine.py`). It holds a unit the same way where
Interactive's campaign disposition holds it (`hold` true; the AIF rule of
2026-10-07: a multi-collision-energy AIF unit, `aif_multi_ce_awaiting_console`,
waits for a patched Console). That hold (`disposition_held`, ledger schema 4)
is counted apart in `status`, is never rechecked by itself and keeps no runner
running; only `recheck-held --unit KEY` (or `--disposition-held` for all of
them) makes its preflight again. The patched Console is MsdialWorkbench #825.
The runner sends the preflight the pinned Console's path, and Interactive 0.5.34
decides for it: with #825 a multi-energy AIF unit runs as AIF
(`multi_ce_aif_with_console_825`; its disposition records `aif_multi_ce_run`
and the probe), without it the unit is held as before. A recheck that releases a
held unit writes a `disposition_hold_lifted` event, and `status` counts such
runs under `multi_energy_aif_runs`. An input with no recorded energy still holds
the unit with that Console. Inputs whose energy sets differ run with Interactive
0.5.36 (0.5.34-0.5.35 held them as `aif_collision_energies_differ_between_inputs`,
which a recheck decides again): the unit's `status` warning says it ran as it is,
on record, and `status` counts it under `multi_energy_aif_runs.energy_sets_differ`;
the `preflighted` transition and a `disposition_hold_lifted` event carry the
same. The plan records
Interactive's probe in the Console pin (`multi_energy_aif`) and prints what it
means, and refuses neither, since a Console without #825 only holds such units.
A campaign pinned to a Console without #825 cannot change it in place (a changed
pin pauses the campaign); a new campaign planned with `--replan-from`, pinning
the #825 Console, takes its `disposition_held` units again. `skip --unit KEY` is the operator's explicit
decision that lifts the hold: its discard passes Interactive
`release_disposition_hold`, which records `disposition_hold_released_by`
`operator_skip`. A split parent's release passes it only when every part its
disposition holds was skipped that way, since Interactive's release lifts the
hold of every held part; while a held part has no skip (it ended failed or
stopped while held), the parent's raw data are kept and its `raw_detail` names
the part it waits for. Interactive never discards a held unit or a held split
part without it, and the runner passes it nowhere else.

The report also prints a `PROGRESS` line: the furthest stage, B1-B10, whose
artifacts exist. It is derived from the artifacts on disk and from fields the
provenance manifest records, each paired with an artifact where one exists, and
never from a stage written by hand; the trial manifest's `evaluation.rule` lists
which fields each stage reads. Progress is
not a verdict. A unit can have reached B5 and still be refused, and a stage it
has not reached is reported as not reached, not as two records disagreeing; a
run the manifest records as attempted is judged as a run. The stages are defined
by `COMPLETION_STAGES` in the gate, and the trial manifest's `evaluation` block
mirrors them (a test holds the two equal); `--json` reports progress and the
checks that judge each stage under `progress` and `checks_by_stage`.

`scripts/verify-download-store.py <store | accession directory | workspace root>`
reads an accession's download store (`<repository>\<accession>\_dl`, where each
repository object is held once and linked into every unit that uses it) beside
the unit manifests, and changes nothing. It refuses (exit 2) an orphan object, a
claim still live for a unit whose raw tree is released and opened before that
deletion (one opened after it, such as a re-run's pre-claim, is a new consumer,
listed for information), an object no unit ever claimed, which the store's
collection can never delete, and a tombstone whose bytes remain or that names no
approval covering boundary 5. It lists every tombstone, and warns about
abandoned partial transfers, linked files modified in place and locks whose
heartbeat has lapsed. Exit 3 means no store was found. It may run beside a
campaign: an object the store holds a fresh lock on, or one that changed as it
was read, is reported as `object_busy` for information and left for a later run,
and so is a released unit's claim whose release is under way (its lock fresh,
released as it was read, or a deletion recorded within a lock's heartbeat
window), and its output escapes what the console code page cannot print rather
than ending without a verdict.

The campaign's run answers come from its approved profile
(`msdial-campaign-profile.v1`); the runner pins the Console path, the smoothing
method, the target peak counts and automatic alignment RT correction on top of
them. Automatic RT correction is decided for the campaign (2026-10-07):
MsdialWorkbench #826's local outlier test at its default window, with 12
anchors. The campaign policy records it (`automatic_rt_correction` true,
`automatic_rt_correction_maximum_anchors` 12), and every Console start is sent
`execute_automatic_rt_correction` true and those anchors, so a profile cannot
leave them out and run at Interactive's default of 6, or uncorrected. A manifest
is approvable only if its profile does not say otherwise (correction off,
another anchor count, the anchor-library correction, or a local window other
than 1.5 min), and its Console pin records `automatic_rt_correction`
`local_support`, read from the method keys in the Console assembly. A Console of
#810 alone (`run_wide`) would run the run-wide test without a word, and one with
neither (`none`) is refused by Interactive at every unit's run start, after its
download. The plan states the automatic RT correction in the summary a person
approves, and the manifest's digest covers that statement. Approve refuses
every manifest that carries no such statement (one planned before the plan
stated it), whatever its policy and profile say, and tells the person to plan
again: the default is correction ON (the user's answer of 2026-10-08), so a
manifest that says nothing is never read as "uncorrected". A manifest planned
now with the default policy states the correction ON. A campaign already
approved before the decision names none of these fields; its units run as its
profile says.

When the Console cannot select anchors (too few candidates, or too few anchors
in enough samples), it exits -1 before alignment with no output, and every
retry would do the same. The runner finds the Console's line in the job's log,
marks the failed attempt `automatic_rt_correction_failed`, and, with the
policy's `automatic_rt_correction_fallback` (on by default), runs the unit's
next attempts without the correction. The failed run still counts as an
attempt. The unit's `campaign-record.json` (`automatic_rt_correction`) and its
status row say so. With the fallback off, the unit is retried and ends as any
failure does.

Blank files have no anchors of their own. By default the Console gives each
Blank a model interpolated between the non-Blank files beside it in the
analytical order. The runner allows that only where the order Interactive
recorded with the analysis CSV is an injection order: the raw headers'
acquisition start times, or the order the repository's sample table declares
(the two ORD-2 accepts). For an order read out of the file names or the
listing, or none recorded, it sends
`automatic_rt_correction_interpolate_blanks_by_analytical_order` false, and the
Blanks keep their measured RTs (audit status `BlankNotCorrected`; the Methods
paragraph counts them among the files that kept their original RTs). A profile
may not set this answer. The unit's `campaign-record.json` records the choice
and the order source, and the `prepare_run` attempt in the ledger keeps
Interactive's plan warnings, which stop nothing. For a declared order,
Interactive may still warn about Blank interpolation, because its plan adopts
only a header order.

The zero-threshold diagnostic never corrects: Interactive turns the correction
off for it. The gate needs nothing more. EXP-1 requires the two audit TSVs that
Interactive adds to `expected_analysis_exports` when the correction is on.
CNT-1 does not count them as samples, MTH-1 reads the correction's method keys
like any other, and QA-1 sets aside the reference file the Methods paragraph
names. The gate reads no column or status of the audit TSVs. A unit that fell
back has no audit TSVs and none expected, since Interactive lists them only for
a run with the correction on.

A profile may name an in-silico MSP library for each ion mode
(`libraries.in_silico_msp_paths`, the fourth tier of `tiered_lipid_msp`). The
plan then states the four-tier contract the pinned Interactive's own tiered
profile writes for it (Interactive 0.5.48: LBM priority 4, the acquired rows 3
and 2, and the in-silico row 1, `predicted`, suggestions false, no RT); until
2026-10-28 it stated the three-tier one, and LIB-2 failed every four-tier unit.
A pinned Interactive whose tiered profile takes no in-silico MSP is refused at
the plan, and so is what that Interactive's own plan would refuse after each
unit's download without reading the file: an in-silico MSP named for another
strategy, more than once for an ion mode, for the other ion mode, other than as
`library:<file name>`, or not as an `.msp`, and a preprocessing step it cannot
state or that does not fit the ion mode. After the run, LIB-3 (the author's decisions of 2026-10-25 and
2026-10-28) holds how such a run reports the in-silico tier's matches: (a)
their evidence source is `InSilico`, never `ReferenceSpectrum` or `RuleBased`,
and an accepted one is coded 430 (a Console before the MS-DIAL change) or 450
(the author's code), the codes seen being recorded; (b) mzTab-M declares the
library as "User-defined in-silico MSP library file"; (c) the in-silico row is
`predicted` with suggestions false, and no feature is named from an in-silico
suggestion; (d) no in-silico match is tagged lipid-RT Confirmed; (e) no
in-silico match is counted as an identification in the per-file peak tables
Interactive counts them from. LIB-3 is not applicable to a run without an
in-silico tier. Where the QA matrix marks a feature represented by an in-silico
match Reference matched, LIB-3 records the count without judging it.
`scripts/report-insilico-concordance.py <unit> [...] [--json]` reports, per
unit and in aggregates only, how often the in-silico tier's top candidate shares
the InChIKey's first block with an acquired reference match (the author's
decision D-B: collected on Stage 1 before Stage 2 is decided); it needs the
alignment candidate table, which a run writes only where its answers set
`export_annotation_candidates` true.

Two decisions of 2026-10-29 shape what a campaign selects and runs. A unit
whose Catalog record says `Both` polarities is in the pools: Interactive reads
each file's polarity from its raw header and splits the unit into
single-polarity parts, which run like any other unit (each part's ledger row
carries its own polarity); a file whose scans switch polarity inside the one
file is excluded (`polarity_switching`, unresolved mixed polarity being outside
the campaign's scope), one whose header gives none as `polarity_unresolved`,
and the rest of the unit runs. And only CID/HCD data run, since every reference
spectrum is CID/HCD-derived (rule `cid_hcd_only_2026_10_29`): the plan excludes
a unit as `fragmentation_not_cid_hcd` where its own evidence names another
fragmentation method (EAD, OAD, ECD, ETD, EThcD, EID, UVPD, IRMPD and the like)
in its label, unit evidence, every sample row, or every analysis file name,
read as the ion-mobility option A reads evidence (the study's text, or an
attribute every unit of a study carries alike, never excludes a unit). An
instrument's model name ("LTQ Orbitrap Velos ETD") is no evidence, and a file
name counts only where the unit's instrument could fragment so, or none is
recorded (`METHOD_INSTRUMENTS`, one table in the plan, the gate and
Interactive); otherwise it is recorded as not credible on that instrument. A
sample row's name (an Extract Name "AA_EAD_A_1") is held to the instrument as a
file name is; a row field that states the method ("Fragmentation method: OAD"),
the label and the unit's evidence are not. A
unit only some of whose files are named so is planned with a
`fragmentation_reading`, needs Interactive 0.5.52 or later, and that
Interactive's campaign lease keeps those files out with their evidence (also an
mzML's activation terms, or a converted mzXML's `activationMethod`); no vendor
header records the method, and the SCIEX wiff2 reader's CID is no evidence.
A sample row that states the method in only some rows plans the unit too, with
the files of those rows listed to be kept out (Interactive's lease reads the
same rows, evidence `sample_row`); a statement is never read as the study's
text. An identifier (a GUID, an all-hexadecimal run of eight or holding a
digit) names nothing. The plan refuses any Interactive before 0.5.52. Absence
of evidence means run. INP-1 FAILs, blocking the run, a unit whose lease
applied the rule (its record carries `fragmentation_check`) and whose
candidates or CSV rows include a file named so or a sample whose row states the
method, or whose lease kept an input out for it without evidence that holds; a
lease before 0.5.52 is gated as it always was. CONV-1 leaves out the conversion
of an input a binding disposition excluded (a Both unit's mzXML without
polarity, excluded as `polarity_unresolved`).

Two more of 2026-10-29, from the verification campaign v4-both-20261029. A
SCIEX .wiff without its .wiff.scan cannot be opened by the Console: Interactive
0.5.53 excludes it at the disposition (`vendor_companion_missing`), and a
Console start refused for it ("WIFF.SCAN is not accessible next to ...") ends
the unit excluded, never retried (`policy.console_start_exclusion`). And where
the pinned extractor cannot read an input's header (`unsupported_format`, every
ZenoTOF .wiff2) in a unit that runs on its declared acquisition and declares no
single polarity, Interactive reads the input's polarity from one polarity
token of its file name and records it (`polarity_inferences`,
`unreadable_header_polarity_from_file_name_2026_10_29`); ACQ-1 FAILs a row of
such an input that runs on anything else, and WARNs naming the inference. Such a unit,
running on its declaration alone and declaring DDA, DIA, AIF or SWATH, is
untargeted where its record leaves that unset
(`untargeted_inferred_from_declared_acquisition`); ELIG-1 FAILs a campaign unit
whose handoff leaves untargeted unset and whose disposition records neither
that inference nor `untargeted_inferred_from_headers`.

The user's decision of 2026-10-31, from the verification campaign
v5-mpst48-20261029, whose MPST000048 positive part hung three times out of
three in the .wiff2 reader. The .wiff2 stays the preferred encoding; where a
diagnostic or production run of a unit or split part fails or times out, the
unit analyses a .wiff2 and it has an attempt left, the runner asks Interactive
0.5.55 (`msdial_switch_repository_unit_to_wiff`) to switch it to the same
samples' .wiff. Interactive switches a DDA unit only (ZT Scan DIA data can only
be analysed from the .wiff2), only where every .wiff2 has its .wiff with that
.wiff's .wiff.scan, and once, recording the switch in the unit manifest
(`encoding_fallback`, rule `wiff2_to_wiff_after_console_failure_2026_10_31`).
The attempt that failed counts; the unit then resumes at its metadata
preparation, so the analysis CSV, the diagnostic's threshold and the run bundle
are made again on the .wiff, and the remaining attempts run on them. The switch
is a `wiff_fallback` attempt, a `wiff2_to_wiff_fallback` event, and part of the
unit's `campaign-record.json` and of the Catalog run records after it. A refusal,
or an Interactive without the tool, leaves the retry as it was. Interactive
reads each .wiff's own header with the pinned extractor (the runner sends it,
as for the preflight) and switches only where each reads as one DDA sample in
the unit's polarity; the headers are recorded beside the switch. A unit whose
inputs were switched after its metadata was last prepared goes back to its
metadata preparation whichever path brought it to a Console start (a retry, an
interruption, a job the backend lost). INP-1 and SPL-1 accept the .wiff only
where that record holds (a diagnostic or run the unit's run_attempts record as
failed before the switch, a DDA unit, swaps exactly of each .wiff2 for its .wiff
with its .wiff.scan, no .wiff2 left running, each .wiff's header, and in a
campaign the switch's boundary-4 crossing); without it they FAIL as before.
ACQ-1 reads each .wiff through its own header, the .wiff2's disposition kept as
the decision record. Each header must list exactly one sample, state the
polarity the gate recomputes for its .wiff2 (the unit's ion mode, else the
.wiff2's header record, else its file-name inference), stand in for its own
swap's .wiff2, and come from the extractor the unit's preflight ran (in a
campaign, a verified build of a pinned pair). PKH-1 FAILs a threshold that only
a diagnostic marked superseded by the switch, or one whose representative file
is no current input, produced.

## Running a campaign unattended

Launch `scripts/campaign-runner.py run` for an unattended campaign through Task
Scheduler, not from a Claude session. A process started from Claude Code or the
Claude desktop app, `Start-Process` included, sits in the app's Windows job
object. That job allows no breakaway, and the app is force-closed when it updates
(it was on 2026-10-02 and 2026-10-06), so a runner started there ends with the
app. `campaign-runner.py schedule-command --campaign ID [--xml-out FILE]` prints
the task definition and the `schtasks` lines, and registers nothing: the task is
persistent system configuration, so registering it is the user's step. The task
has no execution time limit (a task made with `schtasks /SC` alone is stopped
after 72 hours), runs one instance, restarts on failure, and starts again every
hour, so a runner that ended comes back. It runs the runner with `pythonw.exe`,
which runs itself again in a console with no window, and writes the runner's
output to the campaign's `logs\runner.log`.

The runner starts the Interactive backend through WMI (`Win32_Process.Create`):
a broker (`scripts/campaign/backend_launch.py`) starts the backend and exits. The
backend is then neither the runner's child nor in the runner's job, so a tree
kill of the runner, or the end of its task or of the Claude app, leaves the
backend and a running Console alone, and the next runner reattaches. It has a
console of its own with no window, which git, the Console, 7-Zip and the
extractor inherit. `run --backend-launch child` starts it as the runner's child,
the way it was started before 2026-10-06. A backend already answering on the
campaign port is reused, and the runner says how it was started where that is
knowable (`backend-launch.json` beside the campaign's job registry). The runner
knows its own backend by the process tree: when its Python is a launcher (a
venv's `python.exe`, `py.exe`), the process holding the port is the launcher's
child, and the launch record names it, with the launcher as `launcher_pid`.
Where the tree breaks at a process that has exited (under a venv Python, the
broker's own interpreter exits after starting the backend), the listener's
command line and creation time identify it instead.

Three failed backend starts in a row pause starting for an hour. The failures,
the pause and a start still waited for are kept in the campaign ledger, so a
runner the task starts after another one exited honours them. A start is waited
for until `--backend-start-timeout` from its launch, by that runner or the next.
A backend still running past that deadline without answering counts as a failed
start at every check, so the pause applies; no second backend is started beside
it, and the failure names the process to end, with its creation time. When the
WMI broker never reported, the backend has no recorded process id: past the
deadline it is found by the port it holds, or by its command line and creation
time, and a broker that still runs is named in its place; only a start of which
nothing is found running is given up. Whether a named process still runs is read
again each time it is named, the pause's refusal included, so a process that has
ended since gets no `taskkill` line. Once the backend
answers its status, `/api/config` has `--backend-config-timeout` of its own, and
a backend that answers the status but not `/api/config` is reported as that.

When the campaign has no work left (every unit has ended, no request waits for
a runner, and no ended unit holds raw data the runner would look at again),
`run` exits at once, before it takes the campaign, locks the Catalog or starts a
backend, and prints how to end the task. The one thing it still does is release
a Catalog campaign lock that this campaign's approval holds and whose owner has
died (a runner killed after the last unit ended), as `run` does before it locks
the Catalog; a lock of another approval, or with a live owner, is left alone.
A lock it cannot check (the Catalog cannot be imported, or the lock file cannot
be read or is not a lock record) makes `run` exit with code 3 and say so.
The task keeps starting it every hour
until you do: disable it with `schtasks /Change /TN "MSDIAL-campaign-ID"
/Disable` or delete it with `schtasks /Delete /TN "MSDIAL-campaign-ID" /F`. The
runner changes no task itself. Neither command stops the backend the last runner
started; stop it yourself once no job runs in it.

Only `approve` makes a campaign's ledger (2026-10-15). On that day
`verify-env` run before `approve` created an empty `ledger.sqlite` (the schema,
no campaign, no approval, no unit), `approve` then refused the campaign as
already approved, and the scheduled task's `run` made the file again at every
restart. Every other command (`run`, `verify-env`, `status`, `export`,
`pause`, `resume`, `skip`, `retry`, `release-held`, `recheck-held`,
`revoke`) now reads the ledger's state without opening it: where there is no
ledger, or only an empty one, it says the campaign has no approval yet and to
approve it first, and exits with code 2, creating and writing nothing (`run`
before it takes the campaign, locks the Catalog or starts a backend). `approve`
takes an empty ledger as absent and records the approval into it (an
`empty_ledger_approved` event, written in the approval's own transaction, says
so), and `plan` does not refuse one; both still refuse a ledger that holds a
campaign, an approval or a unit, or that cannot be read. Should `approve` meet
an error, it reads the ledger again before it says nothing was recorded: an
approval already committed keeps its `campaign-authorization.json`.

A unit Interactive excludes at its download is excluded, not failed (the
contract of 2026-10-15). Where every declared analysis input a download lacks is
a declared archive the extract stage expanded that holds no file MS-DIAL opens,
the failed lease's `download_failure` carries `retryable` false and a
`disposition` whose `decision` is `exclude`, with the reason
`declared_inputs_unsupported_format` and the inputs (each declared path, its
archive, how many files it held and their commonest suffixes); Interactive
0.5.40 gives the download job the same record as its own `download_failure`,
which the runner reads where the unit manifest has none. The runner reads only
a record of the job it polls: one an earlier job's lease left in the manifest
says nothing of a unit retried since. It then
counts no failure and does not retry the unit; it ends `excluded` with that
terminal reason and the inputs in its terminal detail, and its raw data are
deleted under boundary 5 as for every other exclusion. `status` counts excluded
units by reason (`excluded_by_reason`: `preflight_exclude`, `download_blocked`,
`declared_inputs_unsupported_format`). The same failure recorded without a
disposition, as by an Interactive before the contract, is retried and fails as
before.

A download several units share is fetched once (2026-10-24). Interactive's lease
fetches through the accession's download store, whose gc deletes an object under
the approval once no live claim keeps it, and only a pending claim keeps one for
a unit that has not started. The runner made none, so with raw data deleted after
each unit, the first unit's cleanup let the store collect the object its siblings
were to fetch next, and each fetched it again (MPST000012's four units fetched one
14.78 GB tar four times) while the plan's summary said "fetched once". Now, before
a unit of a download group of more than one unit downloads, the runner claims the
group's objects, by the Catalog's URLs, for every other unit of it that will still
download (Interactive's `pre_claim_downloads`, the claim a batch approval makes).
A unit that ends without validated outputs (excluded, skipped, failed, stopped)
releases what it claimed and never read through the store's own release, with the
approval only where raw data are deleted under a live boundary 5, so the store
collects what no sibling still needs; a claim a lease materialized is left to
Interactive's cleanup or discard. A claim that cannot be made never stops a
download: a `shared_download_not_claimed` event says so. Each finished download's
attempt records what its lease moved (`transfer`: transferred bytes, objects
fetched, refetched and found in the store), and `status` adds it up under
`downloads`, beside the bytes the units' raw trees hold, so a shared object
fetched again shows. The plan's summary says "each shared object fetched once"
only for an Interactive whose lease uses the store and that has
`pre_claim_downloads` (`pins.interactive.pre_claim_downloads`); otherwise it gives
the distinct bytes as the least and the per-unit sum as the most.

The runner takes the workspace root per campaign (2026-10-24). Until then a
command without `--workspace-root` used `D:\13_MSDIAL_Public_Reanalysis\analysis`
without a word, so one aimed at another root looked on D:, said the campaign had
no approval, or planned against the pilots' workspaces. Every command now takes
the root from `--workspace-root` (given before the command), else from
`MSDIAL_REPOSITORY_WORKSPACE_ROOT`, and with neither it refuses (exit code 2),
reading and writing no campaign file. The task definition `schedule-command` prints
names the root itself, so a scheduled runner does not depend on the variable;
the pilots and preflights are read by naming the archive root. While a command
runs, the runner sets `MSDIAL_REPOSITORY_WORKSPACE_ROOT` to the campaign's root
in its own process and puts the user's value back afterwards, so the Interactive
tools it calls in-process are bounded to that root, as an MCP session is to its
own. A backend already answering on the campaign port is used only if it was
started for the campaign's root: the root is read from that process's
environment, or, where that cannot be read, from the campaign's
`backend-launch.json`, which now keeps it, when that record names the process.
One started for another root, for none, or whose root cannot be read is refused,
naming both roots (`run` exits with code 3; a campaign paused for its backend
stays paused): a backend left running for a campaign on the archive root would
otherwise take the new campaign's jobs into the old campaign's job registry.

## Codex pre-audit

The local Python test suites passed on 2026-09-02 after the re-audit fixes:

- Repository Catalog: 25/25
- MS-DIAL Interactive: 139/139

Codex's detailed pre-audit is stored separately in
`feedback/codex-pre-audit-2026-09-02.md`. Claude should complete its own audit
and write its report before reading that file. Compare the two reports only
after Claude has fixed its findings in writing.

## MB-POST 1-10 is not ten equivalent runs

In the current catalog, `MPST000002` is absent and the other nine accessions
produce sixteen analysis units. Several are targeted SRM/SIM, GC-MS, DI-MS, or
product-ion experiments and therefore fall outside the current untargeted
LC-MS/MS DDA/DIA scope. The pilot prompt requires Claude to enumerate,
classify, and
exclude unsupported units instead of blindly processing ten accession IDs.

## Official Claude Code references

- MCP project configuration: <https://docs.anthropic.com/en/docs/claude-code/mcp>
- Claude Code skills: <https://code.claude.com/docs/en/slash-commands>
- Claude Code CLI: <https://docs.anthropic.com/en/docs/claude-code/cli-usage>
