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

This file states the rules in force. The history of each decision, with the
user's words, is in `trials/2026-09-20-ten-unit-trial-manifest.json`
(`decisions`). A rule marked pending, in bold brackets naming the pull request,
is decided but rests on code that has not merged yet; Pending pull requests, at
the end, lists them. Until it merges, the merged code does what the mark says.

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
the units of an approved campaign, and only for them. It calls the same
Interactive and Catalog functions the MCP tools expose, in its own process.
Where a tool would ask for `confirmed=true` it passes the campaign approval
(`campaign_authorization_path` to Interactive, `ratification` to the Catalog),
and no covered step acts before Interactive's validator
(`campaign_authorization.authorize`) has accepted that boundary for that unit
and the crossing is recorded. The Catalog checks only a ratification's form, so
the runner validates boundary 3 for the unit before each Class save. The
runner decides no eligibility: it runs, splits, skips or excludes a unit as
the unit's `campaign_disposition`, written only by Interactive's
`classify_preflight`, says.

## Local storage

The workspace root is chosen per campaign and passed explicitly: as
`workspace_root` to every `msdial_repository_reanalysis_plan` and
`msdial_download_repository_raw` call, and as `--workspace-root` (or
`MSDIAL_REPOSITORY_WORKSPACE_ROOT`) to the campaign runner. It is never derived
from the project directory.

```text
F:\MSDIAL_Public_Reanalysis\analysis      MB-POST production (dedicated HDD)
D:\13_MSDIAL_Public_Reanalysis\analysis   archive of the pilots and preflights
```

Any other root needs the user's explicit confirmation. Raw data and MS-DIAL
outputs never go to C:, a user profile, a temporary folder or a code checkout.
**[pending: gate truthful-libraries]** The runner takes the root per campaign,
refuses a relative, network or in-checkout root and a backend serving another
root, and the plan summary names the root; merged runner code still defaults to
the D: root.

Interactive creates `<workspace_root>\<repository>\<accession>\<analysis_unit_id>\raw`,
`provenance` and `output`. These directories under a root are not analysis
units and are never run as one:

- `<accession>\_dl`, the accession's download store: each repository object
  once, however many units list it; each unit gets its own raw tree built from
  it, because MS-DIAL writes beside the files it reads. It is raw data, under
  the same deletion rule.
- `_campaigns\<campaign_id>`, a campaign's ledger and approval.
- `_library_copies`, the library-copy store: one MS-DIAL loaded-library copy
  (`.dbs`) per library set and annotation settings, kept so that finalised
  projects can be reopened, instead of one per unit.
- `_tmp`, the Console's temporary folder for repository units; `_viewing`,
  `_verification` and `_acceptance`, for viewing restores and test runs.

## Supported production scope

- This public-repository campaign accepts LC-MS/MS only: untargeted DDA or
  DIA/AIF/SWATH with an MS1 survey and product-ion spectra. One project type,
  ion mode, acquisition mode, chromatography regime and ion mobility regime per
  MS-DIAL run.
- GC-MS, SRM/MRM, SIM, DI-MS, imaging MS, product-ion-only experiments and
  unresolved mixed-polarity units are review or exclusion cases, not silent
  conversions. LC-IM-MS is excluded, with its reason recorded. MS-DIAL
  Interactive can analyze some of these modes outside this campaign; that
  broader capability does not expand this scope.
- **Declared "Both" polarity.** A unit whose repository declares both
  polarities is outside the campaign's pools for now. Such units come in a
  later stage, after polarity splitting has been validated on one of them,
  each with its Class decision.
- **Acquisition, header first (rule B2).** Each file's acquisition is read from
  its raw header with the raw-metadata extractor the campaign pins, and the
  header decides over the repository's declaration, which is only the
  Catalog's keyword inference. The extractor's `confidence` is no reason to
  set a verdict aside. A file whose header leaves the mode unknown is excluded
  with its reason, and the rest of the unit runs. In a unit declared DIA, AIF
  or SWATH, an MS1-only file is not folded into a DDA run. ACQ-1 FAILs a row
  run as another type than its header gives. Where no header could be read and
  the repository declares a mode, the unit runs on its declaration, recorded as
  `acquisition_declared_only`.
- **AIF** goes by the MS2 collision energies its inputs record (a Waters
  LockSpray reference function records none). One energy: it runs as SWATH.
  Several: it runs as AIF on a Console with MsdialWorkbench #825, else it is
  held with raw data kept (`aif_multi_ce_awaiting_console`). Inputs whose
  energy sets differ run as is, each file with its own representative energy,
  recorded as `aif_energy_sets_differ_between_inputs`; each input's set is
  keyed by its path relative to the unit's raw data root. Any one input that
  records no energy above 0 holds the whole unit, raw data kept
  (`aif_collision_energy_unrecorded`).
- **Inputs.** mzML is an input, and so is a vendor folder (a Waters `.raw`, an
  Agilent or Bruker `.d`) or an archive holding one: one folder is one input
  and one row of the analysis CSV, which is generated from the unit's inputs.
  Archives (zip, tar, `.7z`, `.rar`, bare `.gz`, `.lzma`) are extracted, and
  each extraction is recorded.
- **Folders inside one per-sample archive are copies.** Where one sample's
  archive holds the same file in several folders (`S1/A/S1.mzML` and
  `S1/B/S1.mzML`, or a vendor folder beside an mzML), one is used, by the one
  encoding rule, and the others are recorded; they are copies even where their
  bytes differ. Folders whose names mark opposite polarities, and different
  stems, are still refused.
- **A unit whose declared inputs are only partly in a format MS-DIAL opens** is
  excluded whole, with its reason (`declared_inputs_unsupported_format`), and is
  not retried; its raw data go under the approval.
- **mzXML and mzData** are not MS-DIAL inputs. Outside a campaign, one that is
  the encoding analysed is `requires_conversion`: stop before download or
  execution and require a reviewed ProteoWizard-to-mzML conversion with new
  provenance. In a campaign, Interactive's validated converter turns such an
  mzXML into mzML under the unit's `raw\converted` (never into the download
  store) and records the conversion as the input's provenance; a file whose
  conversion fails is excluded with its reason, and the rest of the unit runs.
  An mzXML scan without a polarity is given the unit's declared ion mode only
  when the handoff declares exactly one polarity in
  `technical_settings.ion_mode` and the file records no scan of the opposite
  polarity; the imputation is recorded as an inference. Otherwise nothing is
  imputed and CONV-1 FAILs. An mzXML whose scans mix the opposite polarity
  with scans of none is excluded with its reason. mzData has no converter:
  where it is a sample's only encoding, it excludes the unit.
- **Ion mobility from unit-level evidence.** A unit counts as ion mobility only
  on its own evidence: its rows or assay fields (an instrument such as a
  timsTOF, Synapt HDMS, Vion IMS, Agilent 6560 or Cyclic IMS, or an explicit
  ion-mobility parameter) and the formats of its own containers. Study-level
  text that many units share is not such evidence. The plan excludes a unit
  only where the Catalog's `ion_mobility_evidence` state is `enabled`; a mixed
  unit (MTBKS219 and MTBKS220: Bruker BAF beside TDF), and a unit whose Catalog
  record the plan cannot read, pass to the raw-header check, which excludes the
  ion-mobility files or parts.

## Evidence and decisions

Repository fields and raw headers are evidence. Class, contrast, target omics,
internal-standard ions, and annotation policy are scientific decisions. Keep
source facts, model inferences, and accepted decisions separate.

Before selecting units or proposing Class, ask what the user wants to learn from
the reanalysis. Record the scientific question, intended biological comparison,
whether annotation or comparative profiling is central, and required outputs as
`analysis_purpose`. Use that purpose consistently for Class, contrast, library,
QA, and reporting decisions. Do not proceed to download while it is missing.

**Peak height.** For production repository runs, use the zero-threshold
diagnostic before the main run, on a QC nearest the analytical-order midpoint,
or the non-blank sample nearest it when no QC exists. Set Minimum peak height
to keep approximately 3,000-6,000 peaks, at the lower end: the highest
threshold whose estimated count is still at least 3,000. Search in the
instrument family's step, 100 for QTOF-type data and 1,000 for
Fourier-transform data, the family read from the file first. Only when the
diagnostic count is above 6,000 and no step of the family lands in the range,
fall back to 10 (Fourier-transform: 100), never finer, and record the
fallback. Keep 0 when the diagnostic count is at most 6,000. Use
`TimeBasedLinearWeightedMovingAverage`, and retain the method, representative
sample, diagnostic count, threshold step, any fallback, accepted threshold and
the production run's own peak counts in provenance. PKH-1 holds the step.

**Automatic RT correction** is on in a campaign: MsdialWorkbench #826's local
outlier test, default window 1.5 min, 12 anchors. The plan summary states the
correction that will run. A `plan --policy` override is not refused; it is
stated in the summary and recorded in the manifest (`policy_overrides`), so
the digest the person approves covers it. A manifest that carries no automatic
RT correction statement is refused, and the person plans again. When the
Console cannot select anchors, the unit's later attempts run uncorrected, on
record. A Blank is interpolated by analytical order only where that order is
recorded (raw-header start times or the repository's sample table). The
zero-threshold diagnostic never corrects.

**Class.** A Class proposal gives one assignment per sample, the selected
source fields, the intended contrast, rationale, and warnings about
confounding or missingness. Do not use continuous fields merely because they
are available. Where the Catalog abstains because no declared factor groups
the samples, do not build Class from other columns. Show the abstention
preview (`msdial_catalog_save_class_proposal(..., abstain=True)`) and, after the
same confirmation as a proposal (boundary 3), save it: the run then carries no
contrast, every sample in the one Class `All`, and the gate's B3 accepts the
ratified abstention.

**Pairing sample rows with files.** A row pairs with a file by its exact name,
by that name behind a prefix, or by a leading identifier token that matches
exactly one file (`VV_13` with `VV_13_..._exp344`). Every pairing not made by
the exact name is an inference and is recorded (`name_pairing.paired_by`);
PAIR-1 lists them. In a unit whose download is its own, an archive member no
row pairs with is an input all the same (`unattributed_member`), on record; an
unpaired mzXML among them is converted in a campaign. The unpaired members of
an archive shared with other units, a member whose path names the opposite
polarity, and, in a unit whose Catalog declared its inputs, a member the
declaration does not name, are left out of every unit, on record
(`unattributed_members.left_out`). INP-1 FAILs any left-out member that
reaches a run.

**The one encoding rule.** When one sample's data arrive in several encodings
(`S1.raw`, `S1.mzML`, `S1.mzXML`; copies in other folders included):

1. Among the readable ones, exactly one is used: the highest in the order
   vendor format (a folder or a container), then mzML, then mzXML (converted
   in a campaign; outside a campaign not an input).
2. A tie goes to the first by relative path, `/`-separated, compared without
   case.
3. Where the chosen one cannot be read or decoded, or its conversion fails,
   the next in order is taken.
4. The file used is that sample's own input, paired to its sample row and in
   its Class, whatever encoding the row names.
5. Every file not used is recorded with its reason, naming the file used
   (`manifest.encoding_choices`).

Gate INP-1 recomputes the rule and FAILs a run that departs from it or lacks
its record. Outside the rule's words the existing behaviour stands, on record:
paths of one stem whose own tokens name opposite polarities are two
acquisitions; two sample rows naming two encodings of one stem each keep their
file; `.cdf`, `.abf` and `.ibf` rank after vendor formats and before mzML; and
a declared input pairs with its row by the declaration's `sample_id`.
Interactive's plan still excludes, before any download, a unit that lists or
names an mzXML (outside a campaign) or an mzData even where that sample has a
readable encoding. That is an implementation gap, not policy: report such an
exclusion as one.

## Annotation and private resources

**Tiers.** A campaign unit is annotated in four tiers, kept apart as evidence:

1. LBM2 rule-based lipid annotation;
2. strict search of the acquired MSP pair (`MSMS-Private-pos-VS21.msp`,
   `MSMS-Private-neg-VS21.msp`);
3. broad search of the same acquired MSP pair;
4. the in-silico MSP GLACIER, predicted MS/MS for the structures registered in
   MS-FINDER, from a non-public NIST20-trained build of coleygroup/ms-pred.
   It is searched without RT and offers no suggestions, with MS1 tolerance
   0.01 Da, MS2 tolerance 0.05 Da, weighted, simple and reverse dot product
   cutoffs 0.5/0.5/0.5 and at least 3 matched peaks. Its matches never receive
   the lipid RT check's "Confirmed" tag. Its label is "In-silico MSP
   (GLACIER)", and each run loads only its own polarity's file.
   **[pending: Interactive in-silico tier; MS-DIAL predicted-MSP semantics]**

An accepted match of a lower tier outranks a higher tier's precursor-only
suggestion. Broad candidates are not equivalent to strict matches. In-silico
matches stay distinguishable from acquired ones by their Evidence source
(`InSilico`) in every export; an accepted in-silico match may be a feature's
reported name only where no LBM2 or acquired-MSP match is accepted.

**LBM2.** The campaign LBM2 is the one the campaign profile names. Since
2026-10-14 that is `Msp20261013094720_NCDK-TUAT-LC25_no-unknown-db.lbm2`,
which replaced the official
`Msp20250303164224_NCDK-TUAT-LC25_converted_dev.lbm2`. Its cutoffs are
0.1/0.1/0.1. **[pending: Interactive #80]** Merged Interactive loads its
catalog's official LBM2 in the tiered profile whatever the profile names.

**Target omics.** Every campaign unit is an MS-DIAL Lipidomics project, and
every MSP annotator row pins its own target omics, Metabolomics. This is a
campaign setting; the repository's own omics field is kept as source evidence.

**Lipid RT check.** Its settings are written into each run's method, never left
to the Console's defaults, and the run records its setting and its review
file. **[pending: Interactive in-silico tier]**

**Records state what ran, not what was pinned.** The run records, the campaign
record and the Catalog row name the libraries the run loaded, with role, kind
and sha256, beside the libraries the approval names. Gate LIB-2, a `blocks_run`
check, stops a run whose libraries, LBM cutoffs or annotator rows differ from
the approval. **[pending: gate truthful-libraries; Interactive #80]**

**Private resources.** The VS21 MSP pair and the GLACIER files are private:
they may be used locally but are never copied into bundles, logs, repositories
or shared reports, and GLACIER is never redistributed. Their locations are
never written into code, tests, logs or shared artifacts: name a library by
file name and sha256, and give tests synthetic paths.

## Confirmation boundaries

Never perform these operations without an explicit user confirmation in the
current conversation, except where an approved campaign's approval stands in
for one (One campaign-level approval, below). Interactive work outside a
campaign is confirmed per run.

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

Dry-run previews must use `confirmed=false`. The preview must report both
selected-unit bytes and actual required bundle bytes; the latter is the
download approval and safety-limit quantity.

**One campaign-level approval.** Inside a campaign there are no per-unit
confirmations. The user approves one campaign manifest, identified by its
sha256 digest, and records the approval with `scripts/campaign-runner.py
approve`, in their own words, as an `msdial-campaign-authorization.v1` record.
For the units the manifest lists, and the parts an automatic split derives from
them, that record is the confirmation for boundaries 1, 3 and 4, for the split,
and for boundary 5 when the manifest's retention is
`delete_after_validated_output`, under the rules and pins the manifest states.
It never covers boundary 2 or 6, a unit it does not list, or anything the
manifest does not state. Only `approve` creates a campaign's ledger: no other
runner command creates one for an unapproved campaign. A pilot is a campaign
of named units (`plan --units`) with its own approval. The one per-unit
question left inside a campaign is the skip that releases a `disposition_held`
unit (Gate verdicts in a campaign).

**The go signal.** The production campaign starts only on the user's explicit
go, asked for in the conversation once everything is ready:
"本番開始前には私がGoサインを出すので、聞いてください。" (2026-10-01). No approved
manifest, pilot approval or authorization record is that go, and the runner's
`run` is never started on a real campaign without it.

**Pins.** A campaign pins the Console and the raw-metadata extractor (by
sha256), each library (by file name and sha256), and the Interactive, Catalog
and gate commits. A change to any pin pauses the campaign until a new approval
is recorded.

- **The production Console** is built from the latest MsdialWorkbench master,
  after the predicted-MSP semantics change merges, with a local RawDataHandler
  pin on a local-only branch that is never pushed. The RawDataHandler build is
  local and unpublished. The Console is pinned by sha256.
- **The raw-metadata extractor** is the build of msrawdataworkbench 5f60446.
- **The gate** is pinned by this repository's commit and a clean tree, and the
  campaign runs it from a frozen worktree at that commit. Any commit to this
  repository during a campaign is a pin change and pauses it, so this contract
  merges before a production plan is made.
- **The Catalog** is planned on as last crawled (2026-09-21 and 2026-09-22);
  no crawl starts unless the user asks.

In a campaign there is no per-unit size limit: `maximum_gb` is the disk bound,
the free space above the reserve the approved manifest's policy states, as the
runner computes it, and an unattended run may not raise it.

## Raw-data retention

The retention policy is never implied: each download's confirmation, or the
campaign approval, states it.

- **Full-repository reanalysis** (`delete_after_validated_output`): a unit's
  raw data are deleted with no further question once every planned MS-DIAL
  output is present and its mzTab-M validates, whatever the gate's verdict;
  once a failed unit has been retried twice; and when a unit is skipped or
  excluded.
- **Trials and pilots** keep their raw data (`keep`).
- **Outside a campaign** the default is `keep`, and an accepted policy records
  intent but is not deletion approval: preview `msdial_cleanup_repository_raw`
  and obtain a separate confirmation for the exact raw directory immediately
  before deletion.

In a campaign, deletion after validated output goes through
`msdial_cleanup_repository_raw`, and the other deletions through Interactive's
approval-taking discard (`discard_download_lease` given the campaign approval),
which checks boundary 5 for the unit and records the crossing before it
deletes anything. Before a finished run's raw data go, Interactive moves its
per-file and alignment containers into `output\msdial-intermediates`, where
the retained-artifact inventory lists them, and keeps the loaded-library copy
once in `_library_copies`; a container it cannot move holds the deletion. A
failed unit's mzTab-M and whatever else its run left in `output` are kept as
failure artifacts. An object in the download store goes once no unit still
claims it, and the runner claims each shared object for every sibling unit
before the first one downloads, so one archive is fetched once
**[pending: gate truthful-libraries]**. A split parent's raw tree goes once
every part has ended.

**Fallback, awaiting the user's approval.** With an Interactive that lacks the
approval-taking discard, the runner may call the discard with `confirmed=true`
only where the approved manifest's policy states
`confirmed_discard_fallback: true`, and only after boundary 5 has been
accepted and recorded for the unit. Without that field those raw data stay,
with the reason in the failure record.

## Gate verdicts in a campaign

The runner runs the gate at each of its points and keeps every report. No
verdict holds the raw-data deletion. At `before-production`:

- `blocks_run`: ELIG-1, ACQ-1, SUM-1, CNT-1, INP-1, ID-1, PRE-2 and CONV-1. A
  FAIL in one of them stops the run, and the unit is a failed unit: retried
  twice, then its raw data are deleted.
- `record_only`: CLS-1, CLS-2, CLS-3, ORD-1, PKH-1, SPL-1, PAIR-1 and PRE-1. A
  FAIL is recorded with the unit, and the unit runs. PRE-1 never FAILs.

**[pending: gate truthful-libraries]** LIB-2 joins `blocks_run`.

The gate states each check's list as its `run_policy`, and the runner holds
the `blocks_run` list itself, whatever a report states. It reads failures from
the `checks` of the `--json` report, never from the exit code.

**In neither list.** No check. A check the gate adds to `before-production` is
named here until the user places it.

**Not evaluable.** A `blocks_run` check left `not_evaluable` where it is
required stops the run exactly as its FAIL does (the report lists it in
`strict_failures`). One not required, and any `record_only` check left
`not_evaluable`, never stops the run.

**No report.** A before-production gate that gives no usable report (no answer
in time, a crash, an exit other than 0, 2, 3 and 4, exit 3, unparsable output,
or a gate that could not start) holds the unit as `gate_held`: not run, its raw
data kept, counted neither as a retry nor as a failure, while every other unit
goes on. The runner runs the gate again at each start and every few hours, so
`run --until-idle` does not return while a unit is held;
`scripts/campaign-runner.py recheck-held` asks for it at once, or says that no
runner holds the campaign and one must be started. After the run a missing
report is only recorded.

**A stop is per unit.** A FAIL, a check left unevaluated or a hold stops that
unit's analysis, never the runner. A pin change, a short disk, a repository
outage and an Interactive backend that does not answer pause the whole
campaign instead, the four pauses the status export names; each lifts by
itself. Nothing one unit does pauses the campaign: a reply or record of
Interactive's the runner cannot read for one unit, a reply that does not parse
among them, holds that unit as `contract_held`, like a missing report. A job's
poll whose reply cannot be read is never taken for a lost job.

**Held by Interactive.** A unit whose `campaign_disposition` holds it (the AIF
holds) is `disposition_held`: not run, its raw data kept, counted neither as a
retry nor as a failure, and not rechecked by itself. `recheck-held` makes its
preflight again and deletes nothing, so it needs no OK. Only an operator's
`skip` releases the hold, deleting the raw data the hold keeps, and that skip
is made only on the user's explicit word for that unit; the approval's
boundary 5 does not cover it.

The gate lifts none of Interactive's own refusals. Outside a campaign every
`before-production` refusal stops the unit.

## Batch behavior

Treat an accession range as a selection request, not an instruction to run every
record. Enumerate every analysis unit, explain exclusions, estimate bytes, and
ask the user to approve the final run manifest. Process approved units
sequentially. Use a unique output directory and exact `job_id` for each run. A
failure in one unit must not erase or mutate another unit's artifacts.

When raw preflight reports `Mixed`, preview and explicitly confirm
`msdial_split_repository_unit`; under a campaign approval the approval is that
confirmation and the split follows the unit's `campaign_disposition`. Never
execute the Mixed parent. Preflight every generated child independently and
proceed only with children whose DDA or DIA/AIF/SWATH mode is resolved and
accepted.

## Required outputs

For every attempted analysis unit retain a machine-readable run manifest,
repository/publication metadata, reviewed sample metadata, analysis CSV,
parameter file, command, software versions, logs, checksums, MS-DIAL text
outputs, mzTab-M validation, QA status, publication artifacts when requested,
and a failure record when unsuccessful.

## Merges

The user merges pull requests, in this repository and in every other
repository of the campaign (Interactive, the Catalog, msrawdataworkbench,
MsdialWorkbench). Claude opens branches and pull requests and pushes to them,
never by force. This contract merges only once the user has approved its
wording.

## Pending pull requests

Decided rules that rest on code not merged on 2026-10-27:

- **Interactive #80** (0.5.47): the tiered profile loads the LBM2 the answers
  name and records the one that ran.
- **Interactive in-silico tier** (0.5.48, branch `feat/insilico-tier`, built
  on #80; not yet opened as a pull request): the GLACIER tier, library kinds
  in every record, the lipid RT check written and recorded, target omics
  pinned per annotator row.
- **MS-DIAL predicted-MSP semantics** (MsdialWorkbench branch
  `feat/predicted-msp-semantics`; not yet opened): an MSP annotator that
  withholds suggestions, mzTab-M evidence rows for accepted in-silico matches,
  and in-silico lipid matches kept out of the lipid RT check. The production
  Console is built after it merges.
- **Gate truthful-libraries** (this repository, branch
  `feat/truthful-libraries-shared-downloads-root`; not yet opened): records of
  the libraries that ran, LIB-2, shared-download claims for sibling units, and
  the workspace root per campaign.

## Feedback to Codex

When asked to audit, do not edit source code and do not download raw data. Return
the structure in `feedback/claude-audit-template.md`. Include exact MCP tool
names, arguments with secrets removed, returned error/status, expected behavior,
severity, and the smallest reproducible sequence. Distinguish missing feature,
contract mismatch, scientific ambiguity, and implementation defect.
