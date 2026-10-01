# MS-DIAL public repository reanalysis

This project uses Claude as a scientific workflow reviewer and orchestrator.
Use the two local MCP servers rather than shell scripts for scientific workflow
execution. The one exception is the campaign runner (see MCP responsibilities).

When the task is software development across the MS-DIAL family rather than a
single reanalysis run, first read the master prompt in the private repository
`systemsomicslab/msdial-family-dev-context`, cloned alongside this
one as `..\context\prompts\00-msdial-family-development-master-prompt.md`. It is the
durable cross-repository handoff for local source, SDKs, demo data, scientific
contracts, testing, and private-resource boundaries. It is private, and
excluded by `.gitignore` here, because it records licensed SDK install
locations and private MSP library paths.

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

The campaign runner, `scripts/campaign-runner.py`, is the execution path for
the units of an approved campaign (see Confirmation boundaries), and only for
them. It calls the same Interactive and Catalog functions the MCP tools expose,
in its own process, so a campaign unit passes every check an interactive one
does. Where a tool would ask for `confirmed=true` it passes the campaign
approval instead (`campaign_authorization_path` to Interactive, `ratification`
to the Catalog). No step the approval covers acts before the approval has been
checked for that unit and that boundary with Interactive's validator
(`campaign_authorization.authorize`) and the crossing recorded. Interactive
makes that check itself wherever it takes the approval. The Catalog checks only
a ratification's form, not the approval it names, so the runner validates
boundary 3 for the unit before the Class save, and the Catalog stores the
ratification with the decision. The discard of a failed, skipped or excluded
unit is no exception: the runner uses Interactive's approval-taking discard,
and the one `confirmed=true` call it may make in its place is a fallback that
waits for the user's approval (Raw-data deletion in a campaign). The runner
decides no eligibility: it reads the unit's `campaign_disposition`, which only
Interactive's `classify_preflight` writes, and runs, splits, skips or excludes
the unit as that record says. It stops a run on a gate verdict only as Gate
verdicts in a campaign says.

## Local storage

Use this mandatory default repository reanalysis root:

```text
D:\13_MSDIAL_Public_Reanalysis\analysis
```

Pass it explicitly as `workspace_root` to every
`msdial_repository_reanalysis_plan` and `msdial_download_repository_raw` call.
Do not place raw downloads or MS-DIAL outputs on C:, in the Claude project
directory, in the user profile, or in a temporary directory. Interactive creates
`<workspace_root>\<repository>\<accession>\<analysis_unit_id>\raw`,
`provenance`, and `output` (workspaces made before units had their own level
hold them at the accession level). Require explicit confirmation before using
any different absolute path.

Two kinds of directory under the workspace root are not analysis units and are
never run as one. `<workspace_root>\<repository>\<accession>\_dl` is the
accession's download store: it holds each repository object once, however many
units list it, and each unit gets its own raw tree built from it, because
MS-DIAL writes beside the files it reads. The store is raw data, under the same
deletion rule. `<workspace_root>\_campaigns\<campaign_id>` holds a campaign's
ledger and approval, which name each library by file name and sha256, never by
location.

## Supported production scope

- This public-repository campaign accepts LC-MS/MS only.
- Acquisition must be untargeted DDA or DIA/AIF/SWATH with an MS1 survey and
  product-ion spectra.
- In a campaign, where the repository record does not declare a unit's
  acquisition, it is read from each file's raw header with the raw-metadata
  extractor the campaign pins, and a unit whose acquisition is still unknown
  after that does not run. In such a unit, a file whose header leaves its
  acquisition unknown is excluded with its reason recorded, and the rest of
  the unit runs; a unit with no file left does not run. Interactive's
  `classify_preflight` applies this as the unit's `campaign_disposition`,
  which lists each excluded input with its reason.
- mzML is supported, and so is a vendor folder: a Waters `.raw`, or an Agilent
  or Bruker `.d` directory, or an archive holding one. A folder is one data
  file, one input and one row of the analysis CSV, which is generated from the
  unit's inputs; the files inside it are only downloaded.
- mzXML is not an MS-DIAL input. Of several encodings of one sample one is
  analysed: a vendor container or folder first, then mzML, then mzXML, so a
  convertible mzXML outranks a twin that nothing reads or converts, such as a
  `.dat`. Outside a campaign, an mzXML or mzData that is the encoding analysed
  is `requires_conversion`: stop before download/execution, and require a
  reviewed ProteoWizard-to-mzML conversion with new provenance. In a campaign,
  where an mzXML is the one analysed, Interactive's validated converter turns
  it into mzML with every inference the converter offers switched off, and
  records the conversion as that input's own provenance (source and output
  sha256, converter identity, validation). The mzML, written into the unit's
  own raw tree under `raw\converted` and never into the download store, is the
  input. An mzXML runs only as the mzML its recorded conversion made: until
  the unit's manifest records that conversion it stays `requires_conversion`,
  and a file whose conversion fails is excluded with its reason recorded while
  the rest of the unit runs. mzData has no converter: where it is the encoding
  a sample has, it is `requires_conversion` and excludes the unit.
- **mzXML without a polarity, awaiting the user's decision.** Whether
  imputing a polarity is one of the inferences a campaign's conversion keeps
  off is not decided. The converter imputes one only when asked
  (`impute_polarity`), and then the unit's declared ion mode, which CONV-1
  holds it to. Until the user decides, it stays off. A scan whose mzXML
  records no polarity (the attribute is optional, and may be `any`) then
  becomes a spectrum with none, which MS-DIAL skips as it skips any spectrum
  whose polarity is not the method's ion mode, and CONV-1 FAILs the
  conversion. CONV-1 is in neither of the campaign's gate lists (Gate verdicts
  in a campaign), so the FAIL is recorded, the unit runs without those
  spectra, with none at all where no scan records a polarity, and its raw data
  then go as any finished run's do.
- One project type, ion mode, acquisition mode, chromatography regime, and ion
  mobility regime per MS-DIAL run.
- GC-MS, SRM/MRM, SIM, DI-MS, imaging MS, product-ion-only
  experiments, and unresolved mixed-polarity units are review or exclusion
  cases, not silent conversions. MS-DIAL Interactive can analyze some of these
  modes outside this campaign; that broader capability does not expand this scope.
- LC-IM-MS is excluded from this campaign, which is LC-MS only. Ion-mobility
  data (Bruker TDF/timsTOF, Waters and Agilent ion mobility) are excluded with
  the reason recorded, whether the repository or the raw headers show it.

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

Where the Catalog abstains because no declared factor groups the samples, do not
build Class from other columns. Show the abstention preview
(`msdial_catalog_save_class_proposal(..., abstain=True)`, which reports the
reason and the factors considered) and, after the same confirmation as a
proposal (boundary 3), save it: the run then carries no contrast, every sample
in the one Class `All`, and the gate's B3 accepts the ratified abstention.

## Confirmation boundaries

Never perform these operations without an explicit user confirmation in the
current conversation:

1. Raw-data download, including destination, size bound, selected analysis
   unit, and retention policy.
2. Official-library download.
3. Saving a Class proposal, or an abstention.
4. Starting each production MS-DIAL run after showing the exact plan/command.
5. Raw-data deletion after validated output and retained-artifact inventory.
6. Recording a person's reading of the sentences a gate check left for them
   (`scripts/record-reading.py --digest`): only after that person has read the
   sentences it shows, in the conversation, and said what they found; `--by` is
   that person and `--conclusion` is what they said. A reading is never the
   agent's own.

A campaign approval is the one exception. The user approves one campaign
manifest, identified by its sha256 digest, in the conversation, and the
approval is recorded with their words verbatim as an
`msdial-campaign-authorization.v1` record. For the units the approval record
lists, which must be the approved manifest's units, and the parts an automatic
split derives from them, that record is the confirmation for boundaries 1, 3,
4 and 5 and for the split, under the rules the manifest states (among them its
Class rule, its method rules, and the deletion and gate rules below) and with
the pins it fixes. It never covers boundary 2 or 6, a unit the record does not
list, or anything the manifest does not state. Interactive holds a step to the
record's own unit list, and compares the manifest with the digest only when the
record names the manifest's path, so the runner writes the manifest's units
into the record and names the manifest's path in it (`campaign_manifest_path`).

Interactive checks the record at each boundary it guards and writes the
crossing into the unit's manifest. The Catalog does not read the record: it
checks only that a ratification names an approval id and a well-formed
manifest digest, and it cannot be told which unit a ratification is for. So
the runner checks boundary 3 for the unit with Interactive's validator before
the Class save and records the crossing in its ledger, and the Catalog stores
the ratification with the Class decision. Every boundary a campaign unit
crossed is therefore an artifact, not a conversation fact. The pins are the
Console and raw-metadata extractor binaries (by sha256), each library (by file
name and sha256), and the Interactive, Catalog and gate commits. A change to
any pin pauses the campaign until a new approval is recorded. The gate is
pinned by this repository's commit and a clean tree, so any commit here pauses
a running campaign too, one that changes only this contract or records a
decision in the trial manifest included.

Outside a campaign, an accepted raw-retention policy records intent but is not
deletion approval. Preview `msdial_cleanup_repository_raw` and obtain a
separate confirmation for the exact raw directory immediately before deletion.

Dry-run previews must use `confirmed=false`. Default raw retention is `keep`.
The preview must report both selected-unit bytes and actual required bundle
bytes. The latter is the download approval and safety-limit quantity. In a
campaign there is no per-unit size limit (the user's decision of 2026-09-30):
`maximum_gb` is the disk bound, the free space above the reserve that the
approved manifest's policy states, as the runner computes it when the download
starts, and an unattended run may not raise it above that bound. The approval
record states no size, and Interactive holds a campaign download to
`maximum_gb` as it does any other, so the runner always passes the bound.

## Raw-data deletion in a campaign

Under a campaign approval that states `delete_after_validated_output`, a
unit's raw data are deleted with no further question:

- once every MS-DIAL output its run planned is present and its mzTab-M
  validates, whatever the gate's verdict;
- once a failed unit has been retried twice without success;
- when a unit that holds raw data is skipped or excluded.

Each of these deletions crosses boundary 5, which the approval covers: in a
campaign, boundary 5 is every deletion this rule names, not only the one after
validated output. The first goes through `msdial_cleanup_repository_raw`,
which takes the approval itself. The other two go through Interactive's
approval-taking discard: `discard_download_lease` given the campaign approval,
as cleanup is given it, which checks boundary 5 for the unit and records the
crossing in the unit's manifest before it deletes anything. The runner uses
that discard and passes it no `confirmed=true`. It is not on Interactive main
yet, where the discard takes only `confirmed`.

**Fallback, awaiting the user's approval.** Until the approval-taking discard
lands, the runner may call the discard with `confirmed=true`, and only after
`campaign_authorization.authorize` has accepted boundary 5 for that unit and
the crossing is recorded in the unit's manifest. Without that approval, the
raw data of a failed, skipped or excluded unit stay until the discard lands,
with the reason recorded in the unit's failure record and its bytes counted
against the disk budget. Either way the fallback ends when the discard lands.

For a failed unit whose output holds an mzTab-M, because the mzTab-M did not
validate or because the run missed another planned export, the deletion rule
wins: once its retries are spent its raw tree is discarded, and the mzTab-M,
its validation and whatever else the run left in `output` are kept as the
failure record's artifacts. The approval-taking discard is to accept such a
unit; the discard on main refuses any unit whose output holds an mzTab-M, and
cleanup accepts only a validated run. Until a discard accepts it, its raw data
stay, the refusal is recorded as the reason in its failure record, and its
bytes count against the disk budget. The runner never works around the guard,
by deleting the mzTab-M or the raw tree itself.

Every guard Interactive puts on a deletion still applies, the retained-artifact
inventory a finished run's cleanup requires included. Before a finished run's
raw data go, Interactive moves that run's per-file and alignment containers
from beside the inputs into `output\msdial-intermediates`, where the
retained-artifact inventory lists them, and deletes the loaded-library copy
`<project>_Loaded.msp2.dbs`; a container it cannot move holds the deletion.
Earlier attempts' containers stay in the raw tree and go with it. A split
parent's raw tree, which its parts share, goes once every part has reached one
of these ends, and an object in the download store once no unit still claims
it. The gate's verdict does not hold the deletion; it is kept for the
verification that follows the campaign.

## Gate verdicts in a campaign

The runner runs the gate at each of its points and keeps every report for the
verification that follows the campaign. No verdict holds the raw-data
deletion. Which `before-production` FAILs stop a unit's MS-DIAL run is the
user's decision of 2026-10-01, which stops it only for a FAIL in the first
list:

- `blocks_run`: ELIG-1, ACQ-1, SUM-1, CNT-1 and INP-1. A FAIL in one of them
  breaks the results, so it stops the run, and the unit is a failed unit under
  the deletion rule: it is retried twice, and its raw data are then deleted.
- `record_only`: CLS-1, CLS-2, CLS-3, ORD-1 and PKH-1. A FAIL in one of them is
  recorded with the unit, and the unit runs. Its raw data then go once its
  outputs are present and its mzTab-M validates, so a Class, grouping, order or
  threshold error it carries is corrected only from a new download.

**In neither list.** The user's rule does not name four `before-production`
checks: ID-1 (the workspace is the unit its manifest names), SPL-1 (a split
part partitions its parent's inputs), PRE-1 (a header-confirmed acquisition
claim is permitted) and CONV-1 (every converted input is a validated
conversion). It stops the run only for the five `blocks_run` checks, so a FAIL
in one of these four is recorded with the unit and the unit runs, as a
`record_only` FAIL is. PRE-1 never FAILs: it reports PASS, WARN or
not_evaluable. A check the gate adds to `before-production` is named here
until the user places it.

Nothing else stops the run. A check left `not_evaluable` is recorded with
the unit and the unit runs, whichever list it is in and whether or not it is
required; the report lists the required ones in `strict_failures`. A gate that
produced no report is recorded the same way, and the unit runs: one that
exited 3 (the workspace is unusable), or with any code other than 0, 2 and 4,
or whose output does not parse as a report. The gate lifts none of
Interactive's own refusals: a unit Interactive refuses to start does not run,
whatever the gate said.

The runner reads which checks failed from the `checks` of the gate's `--json`
report, never from the exit code. The exit code is 2 for any FAIL, in a list
or in neither, and a FAIL outranks a strict refusal, so one `record_only` FAIL
gives 2 however many required checks were left unevaluated. Outside a
campaign every `before-production` refusal still stops the unit.

## Batch behavior

Treat an accession range as a selection request, not an instruction to run every
record. Enumerate every analysis unit, explain exclusions, estimate bytes, and
ask the user to approve the final run manifest. Process approved units
sequentially at first. Use a unique output directory and exact `job_id` for each
run. A failure in one unit must not erase or mutate another unit's artifacts.

When raw preflight reports `Mixed`, preview and explicitly confirm
`msdial_split_repository_unit`; under a campaign approval that covers the
split, the approval is that confirmation and the split follows the unit's
`campaign_disposition`. Never execute the Mixed parent. Preflight every
generated child independently and proceed only with children whose DDA or
DIA/AIF/SWATH mode is resolved and accepted.

## Annotation and private resources

For the current broad LC-MS annotation pilot, preserve separate evidence tiers:
LBM rule-based lipid annotation, strict MSP search, and broad MSP candidate
search. A lower-priority MS/MS reference match outranks a higher-priority
precursor-only suggestion. Broad candidates are not equivalent to high-quality
matches. Private VS20/VS21 MSP files may be used locally but must never be
copied into bundles, logs, repositories, or shared reports. Their locations
are never written into code, tests, logs or shared artifacts either: name a
library by file name and sha256, and give tests synthetic paths.

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
