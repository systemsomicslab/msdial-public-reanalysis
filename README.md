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
  analysis\  reanalysis workspace, never committed
```

It is a separate private repository because it documents licensed vendor SDK
install locations and private MSP library paths on the development machine.

## Current local paths

- Claude workspace: `D:\13_MSDIAL_Public_Reanalysis\code`
- Repository reanalysis data: `D:\13_MSDIAL_Public_Reanalysis\analysis`
- Interactive: `D:\0_SourceCode\msdial_interactive_app`
- Catalog: `D:\0_SourceCode\msdial_repository_catalog`
- Full catalog: `D:\0_SourceCode\msdial_repository_catalog\catalog-data\native-smoke.sqlite`
- Python: detected by `setup-windows.ps1`, or supplied with `-Python`

The Claude instructions and launcher are stored under `code`. All repository
downloads, MS-DIAL processing files, and generated results use `analysis`:

```text
D:\13_MSDIAL_Public_Reanalysis\analysis\<repository>\<accession>\<analysis-unit-id>\
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
3. Run `powershell -ExecutionPolicy Bypass -File .\setup-windows.ps1` with
   `-InteractiveRoot`, `-CatalogRoot`, and `-ReanalysisRoot` when the defaults
   do not match the analysis PC.
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
`blocks_run`. CLS-1, CLS-2, CLS-3, ORD-1, PKH-1 and SPL-1 are `record_only`,
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
more than one energy sanctions nothing. Archive members of a unit-scoped
archive that no sample row pairs with, which Interactive 0.5.31 includes as
inputs of their own (`name_pairing.paired_by` `unattributed_member`, rule
`unit_scoped_archive_2026_10_07`), are explained inputs to INP-1, CLS-1, CLS-2,
CLS-3 and PAIR-1: each WARNs listing them with
`manifest.unattributed_members.count`, their rows carry the abstention Class or
`Unattributed`, and CLS-2 counts them as no approved sample (an approved sample
with no attributed input beside them is listed under
`samples_without_attributed_input`, not as missing or undelivered). INP-1 FAILs
them only from a download shared with other units, and PAIR-1 FAILs where their
record falls short of the rule. CLS-2 reads what the download
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
them) makes its preflight again. `skip --unit KEY` is the operator's explicit
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
download. A manifest approved before the decision names none of these fields;
its units run as its profile says.

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
