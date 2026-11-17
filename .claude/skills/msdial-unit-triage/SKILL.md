---
name: msdial-unit-triage
description: Triage one public-repository accession before download - decide, for each Catalog analysis unit, which sample rows and archive members it keeps, from Catalog rows and attributes, mwTab sample and factor lines, folder and file-name tokens and a zip member listing; write one msdial-unit-triage.v2 file per unit, with a confidence and a rationale for every decision, for scripts/validate-triage.py to check. Use before planning or downloading any accession with more than one analysis unit, or any unit whose rows may mix polarity, chromatography or acquisition.
argument-hint: "<accession> [--triage-dir <dir>]"
allowed-tools:
  - Read
  - Glob
  - Grep
  - Write
  - mcp__msdial-repository-catalog__msdial_catalog_status
  - mcp__msdial-repository-catalog__msdial_catalog_search
  - mcp__msdial-repository-catalog__msdial_catalog_get_analysis_unit
---

# MS-DIAL unit triage

Skill version: **1.1.0** (write it as `decided_by.skill_version`).

## Why this exists

On 2026-11-15 the stage3b campaign was stopped: every unit of a multi-unit accession had run on the
whole study's files. A Metabolomics Workbench unit usually lists every sample row of the study and one
shared archive, so the run admitted HILIC and reversed-phase, positive and negative, MS1-only and DDA
files together. In an 8-accession trial, an agent reading the evidence available before download put 90%
of 1,124 files on the right unit, left 6% unassigned and put 3.8% on the wrong unit. All of the wrong ones
came from a single decision, made by folder name over byte-identical content. This skill is that triage,
with the failure modes turned into rules. The validator then checks the result deterministically before
anything is planned.

The triage is stage A of the campaign design (docs/llm-assisted-curation.md):

| Stage | Who | What |
|---|---|---|
| A | agent + this skill | `triage/<accession>/<unit_id>.json` per unit (msdial-unit-triage.v2) |
| A' | the wrapper (`scripts/run-triage-agent.py`) | `<unit_id>.record.json` beside it (msdial-llm-decision-record.v1), from the harness transcript |
| B | `scripts/validate-triage.py` | pass / hold / error for the accession |
| C | person | answers the `needs_person` questions and B's holds (`person_answer` entries of the review log) |
| D | plan | manifest carries the triage path and sha256; the person approves its digest |
| F | header preflight | reconciled against the triage; disagreements go back to C |

Everything a triage file says is a **model inference** (`"standing": "model_inference"`). It becomes an
accepted decision only when a person approves the manifest digest that carries it. Never describe a triage
as decided, accepted or confirmed.

## Allowed tools

The triage uses only the tools in this file's `allowed-tools` list, and `scripts/validate-triage.py` refuses
a decision record whose `tool_calls` name any other:

| Tool | For |
|---|---|
| `Read`, `Glob`, `Grep` | this skill, the rendered task prompt and the input directory (handoffs, mwTab text, archive listings) |
| `Write` | the triage payloads, `<unit_id>.json` (or a revision `<unit_id>.r<N>.json`), in the output directory only |
| `msdial_catalog_status`, `msdial_catalog_search`, `msdial_catalog_get_analysis_unit` (server `msdial-repository-catalog`) | the Catalog's units, rows, files and attributes, read only |

No shell, no web fetch, no other MCP server, no Catalog or Interactive tool that writes. The archive
listing and the mwTab text are fetched before the session by the wrapper (the listing from the zip central
directory, `msdial_list_remote_archive`), and the Catalog handoffs are made before it too; all of them
are in the input directory, so every byte the decision rests on is hashed in the decision record. If an
input is missing, go without it and say so (`evidence_sources`, `needs_person`); never fetch it another
way.

## Boundaries

- **Read only.** No raw-data download, no MS-DIAL run, no Catalog write (no Class proposal or abstention
  save, no run record, no handoff), no Interactive plan or download call, and no tool with `confirmed=true`.
- **The Class is not the triage's.** The triage runs before any Class is saved and binds only the evidence it
  read: the Catalog's rows, files and listing, the repository's declarations, the archive listing. Never read
  or cite the handoff's `class_proposal` (its assignments, labels, contrast, status or ratification), or the
  `class_proposal:*` blocking reasons, and never bind it as a decision-record input: the Class is settled and
  ratified afterwards and binds through its own ratification (WS0 decision F). Read the sample rows from the
  handoff's `samples.json` (or its `sample_metadata`) and the files from `files.json`, the copies beside it in
  the input directory; read `handoff.json` only for the unit, the accession and `technical_settings`. The
  sidecar paths `handoff.json` names point outside the session: never read them. The validator refuses a triage
  that cites the Class (`T-CLASS`).
- **Before download means before download.** The only archive evidence is the member listing in the input
  directory, read remotely from the zip central directory before the session. Never download an archive to
  list it. A listing written after an extraction is not pre-download evidence: say so in `evidence_sources`.
  Without a listing the validator holds every unit, because byte-identical content cannot be ruled out;
  going on without one is a person's decision.
- **Write only triage payloads**, into the output directory you were given (in a campaign:
  `<campaign dir>\triage\<accession>\`). A payload is immutable once written: a later change is a revision
  `<unit_id>.r<N>.json`, never an edit. Never write a decision record, the review log, a Catalog handoff, a
  manifest or a ledger; the wrapper and the runner write those.
- **Clean context.** In a campaign the triage runs in a clean harness configuration: a working directory
  that holds only this skill, no user or project `CLAUDE.md`, no memory, no hooks, and only the
  `msdial-repository-catalog` MCP server. What you know beyond the skill, the prompt and the inputs is not
  evidence. Do not cite it. Read only the skill, the input directory and the output directory: the wrapper
  refuses a session that reads anywhere else.
- Private MSP or LBM2 libraries play no part in triage. Never name their paths, and never write an absolute
  path, a user name or an e-mail address into a triage file: payloads are released byte for byte.

## Evidence, strongest first

1. **Repository declarations** (`basis: declared`):
   - the mwTab `MS_METABOLITE_DATA` **Samples** line of each analysis (the input directory's
     `<AN>.mwtab.txt`, from
     `https://www.metabolomicsworkbench.org/rest/study/analysis_id/<AN>/mwtab/txt`). When each analysis
     names its own sample columns, that settles membership (ST003128: SP_, S_ and SL_ each belong to one
     analysis);
   - Catalog row attributes that state the arm: `Mode` ("C18 Negative", "HILIC Positive"),
     `Chromatography ion mode` ("HILIC POS"), `MS_mode` where present (the Catalog does not ingest it yet,
     so read it from mwTab), `raw data`;
   - the unit's technical settings (chromatography, ion mode, acquisition mode, target omics) and its
     `source_subrecord_id` (the AN number);
   - mwTab CH (column and gradient length), MS `ION_MODE`, `MS_COMMENTS` (DDA, DIA, targeted, inclusion
     list), SAMPLEPREP (separate polar and lipid extracts), `SUBJECT_SAMPLE_FACTORS` lines.
2. **Archive listing** (`source: archive_listing`): member paths, folder layout, sizes, CRC32. CRC32 plus
   size is the only pre-download evidence of byte-identical content.
3. **Names** (`basis: inferred`, `source: folder_token` or `filename_token`): HILIC, C18, RP, RPLC, C30;
   pos, neg, POSITIVE, NEGATIVE; DDA, DIA, DIA1/DIA2, SCAN01..06, tDDA, targeted, HERMES, MS1; date or
   batch prefixes; injection numbering.
4. **Raw headers** (`source: raw_header`) exist only after preflight (`phase: post_preflight`).

Sizes, timestamps and parity of injection numbers describe files; they do not say which unit a file
belongs to. Use them as support for a reading the names or declarations already give, never as its only
basis.

## Confidence and rationale

Every item of `rows`, `added_members`, `excluded_rows` and `excluded_members`, and the unit itself, carries
a `confidence` and a `rationale`:

- `high`: the evidence settles it (a declaration names the file's arm, or a name token and the declared
  settings agree and nothing in the listing contradicts them);
- `medium`: the evidence supports it and nothing contradicts it, but no declaration settles it;
- `low`: a reading the evidence allows but does not settle.

The `rationale` is plain language, one or two sentences: what was decided and which evidence decided it
(for example "Kept: the row's Mode attribute is C18 Negative and the member sits under RP_NEG/"). It does
not replace `evidence`, which names the source, field and value.

A kept file with `basis: inferred` and `confidence: low` cannot be `ready`: the validator holds the unit
for a person. Rather than keep such a file in a ready unit, ask (`needs_person`) or abstain.

## Procedure

1. **Purpose.** Copy the `analysis_purpose` the task prompt states into `purpose`, verbatim, or write
   `null`. Never invent one.
2. **Enumerate** every Catalog unit of the accession. For each, record the handoff's sha256
   (`catalog_handoff_sha256`: the sha256 of `handoff.json` as the input directory holds it, which the task
   prompt gives per unit; copy it, since none of your tools hashes a file), its row count
   (`catalog_row_count`) and its declared settings, copied as they read, in `unit_technical.declared`.
3. **Read the declarations** above, then the listing, then the names. Work out the accession's structure
   once (which folders, prefixes or tokens mark which arm), then apply it to every unit.
4. **Every Catalog row** goes into exactly one of the unit's `rows` (kept) or `excluded_rows` (with a
   `reason` and a `detail`). A row that belongs to another unit says which (`belongs_to_other_unit`,
   `belongs_to_unit`). Resolve each kept row to its archive member (`member_path`). Match the exact name
   first, then the stem; record name drift (ST002283: row `D4_TS_E_Lip_Pos` is member
   `D4_TS_E__LipPos.mzML`) with the evidence that ties them. With a listing, a kept row of a ready unit
   whose name resolves to no single member is an error. `not_in_archive` is for a row the listing really
   lacks, and `duplicate_content` for a member with a CRC32-and-size twin; the validator checks both
   against the listing.
5. **Members no row names** (pooled QC, blanks) go into `added_members` only when the folder and the name
   tie them to exactly one unit.
6. **Account for every raw-looking member** of the listing: kept by some unit, or in some unit's
   `excluded_members` with a reason: `archive_metadata` (`__MACOSX/`, `._*`), `not_raw_data` (`.meth`,
   `.xlsx`, `.Rhistory`), `alternative_format_copy` (an mzML copy beside the vendor files),
   `requires_conversion` (mzXML, mzData), or `duplicate_content`.
7. **Per kept file**, set `ion_mode`, `chromatography` (HILIC, RP, normal_phase, other, unknown),
   `acquisition_mode` (DDA, DIA, SWATH, AIF, MS1, targeted_DDA, unknown), `role`, `basis`, at least one
   `evidence` item `{source, field, value}`, a `confidence` and a `rationale`.
8. **Flags, questions and status**, by the rules below, and the unit's `confidence` and `rationale`.
9. **Write** one `<unit_id>.json` per unit (msdial-unit-triage.v2, `schemas/msdial-unit-triage.v2.json`)
   into the output directory. The validator runs after the session (the wrapper runs it, or the operator
   in an interactive session); an error makes the unit a revision, a hold is the review queue, not a
   failure.
10. **Report** per unit: status, confidence, rows kept and dropped, the questions for the person. Keep
    source facts, inferences and open decisions apart.

## Hard rules

1. **Shared byte-identical content is never settled by name.** When two members have the same CRC32 and
   size (in two folders, or under two units), flag `shared_content` (`certainty: confirmed`) on every unit
   involved, set each to `needs_person`, and say what would settle it: the mzML `sourceFile` location and
   name, or the elution order after preflight. ST002283's `Human_Hilic_Neg` and `Human_Lipid_Neg` were
   identical. The folder name pointed to HILIC, but the headers and the elution order showed reversed-phase
   data, so the HILIC-negative unit had no data at all. Keeping the HILIC folder "at medium confidence" was
   the trial's only wrong decision.
2. **Abstain rather than guess.** When names carry no polarity or chromatography signal (ST004544:
   `Sample_11_1..6`), set the unit to `abstained`: no rows kept, every row excluded as
   `no_signal_abstained`, and a `no_signal` question saying which header evidence will settle it. A
   hypothesis such as "odd suffixes look positive" belongs in `notes`, not in `rows`.
3. **Flag what makes a run inhomogeneous,** even when membership is clear. Each of these blocks `ready`
   until a person answers:
   - `ms1_only`: full-scan files without product-ion spectra (ST002798's dilution series, ST002796's
     biological samples, ST004544's `_1`..`_4`, ST003128's S_);
   - `mixed_acquisition`: MS1 with DDA or DIA in one unit, different DIA window schemes (DIA1 vs DIA2),
     m/z-segmented DDA (SCAN01..06), batches acquired years apart;
   - `targeted_dda`: inclusion-list DDA (tDDA, HERMES) is a scope question for a person;
   - `polarity_switching`: a unit declared UNSPECIFIED or Both whose files may switch polarity (ST003128's
     SP_). Such files never go to a run.
   Use `certainty: suspected` until headers confirm. Name the question in `needs_person` (`scope_ms1_only`,
   `scope_targeted_dda`, `polarity_switching`, `mixed_acquisition`).
4. **A folder and a header that disagree go to a person.** Neither wins silently. In ST002796 the header
   heuristic called variable-window QCDIA files AIF and an inclusion-list file DIA, and the folder was
   right both times. Flag `folder_header_disagreement` and ask. The same applies to a name token against
   a Catalog declaration (`name_token_disputed`) and to a unit whose files contradict its declared
   settings (`declared_technical_disputed`).
5. **One polarity per run.** A unit whose files are of both polarities can be `ready` only with `children`,
   one per polarity and acquisition mode, that partition its kept files. A unit declared positive or
   negative never keeps files of the other polarity, in a child or otherwise.
6. **No file in two units** unless the study really shares it (one blank serving two arms). Then both
   units declare it in `shared_with`.
7. **The Catalog can be wrong.** It does not ingest `MS_mode`, it truncates `Ratio` at ';', some of its
   abstentions are wrong (ST003964, ST003128 declare Treatment and Genotype), and some `target_omics`
   values are mislabelled. Record `catalog_defect` or `target_omics_doubt` and keep going. Do not correct
   the Catalog.
8. **Say where pre-download evidence came from.** A member listing written after an extraction is not
   pre-download evidence. Say so in `evidence_sources`.
9. **Answers are a person's.** A v2 payload has no `person_answers`. A person's answer is a `person_answer`
   entry of the accession's `review-log.jsonl`, written from a request that names that person and carries
   their words verbatim (`scripts/record-person-answer.py` before the plan, the runner's requests after it);
   the agent never writes one, and never writes the review log. A blocking flag stays on the payload; a unit
   is `ready` with it only once the review log holds a person's answer to that flag's code for this payload or
   one it supersedes. A kept file inferred at low confidence in a ready unit waits for an answer with the code
   `inferred_low_confidence`.
10. **Revisions.** A revision is a new file `<unit_id>.r<N>.json`, written only when the task prompt asks for
    one and names its cause (a `person_answer` or a validator verdict; `run-triage-agent.py --revise`). The
    prompt gives the current payload (`previous.json`) and the cause entry (`cause.json`). Revise only what
    the cause settles and carry the rest over. A revision may drop a blocking flag, or answer a question,
    only when the cause is a person's answer to that code, followed as worded. The wrapper writes its
    decision record and the review log's `revision` entry.

## Status

| status | meaning | validator |
|---|---|---|
| `ready` | the selection can go to the plan; no question open, no blocking flag unanswered, no kept file inferred at low confidence | pass |
| `needs_person` | at least one question in `needs_person` | hold |
| `abstained` | nothing assigned, because names give no signal | hold |
| `out_of_scope` | outside the campaign (MS1 only, polarity switching, GC, SRM); a person accepts the exclusion | hold |

## Validator

```text
<python> scripts/validate-triage.py <campaign dir>\triage\<accession>
    --catalog <campaign handoffs dir>          (or a catalog.json in the eval-fixture shape)
    --archive-listing <archive-members.tsv>    (repeat per archive)
    [--json] [--ground-truth <ground_truth.json>]
    [--code-root <released code>] [--release-bundle <bundle>] [--emit-selection]
```

The handoffs directory may hold many accessions; only the triaged accession's units are checked, and one
of them with no triage file holds. Use an absolute interpreter path on this machine, never a bare
`python`. Exit status: 0 pass, 1 hold, 2 error. A check that cannot run is not passed: without `--catalog`,
or without `--archive-listing`, every unit holds. It checks:

- the schema (`schemas/msdial-unit-triage.v1.json` or `.v2.json`, by the payload's `schema`), one accession
  per directory, files named by unit; a v1 payload is reported `rationale_not_recorded`;
- that every Catalog row is kept or excluded exactly once, matches the handoff row for row, and that the
  handoff sha256 is the one triaged;
- that no file is in two units without `shared_with` on both, and names every set of units that keep an
  identical file set (the stage3b failure). A row named only by `raw_file` is the same file as the member
  it names, and its name tokens and format are read from that member's path. No unit keeps one
  acquisition twice (a vendor file and its mzML copy), or keeps a member it also lists in
  `excluded_members`;
- CRC32-and-size twins: a kept file with a byte-identical twin anywhere in the listing is an error unless
  `shared_content` is flagged and the unit waits for a person;
- that `not_in_archive` and `duplicate_content` exclusions agree with the listing, and that a
  `belongs_to_other_unit` row or member is kept by that unit (by file, not row index);
- that no exclusion reason contradicts the unit: `needs_person` only with a question open and the unit not
  `ready`, `no_signal_abstained` only in an abstained unit (or one asking `no_signal`),
  `polarity_switching` and `out_of_scope_ms1_only` only with their flag, and `requires_conversion` only for
  a file that is mzXML or mzData (a row named only by `raw_file` is read through the listing);
- polarity and chromatography of every kept file against its unit, child, declared settings and name
  tokens, children's polarity against the unit's, and raw-header evidence against the row;
- that no unit outside `out_of_scope` keeps mzXML or mzData;
- that MS1-only, targeted, mixed-acquisition and polarity-switching files are flagged, that `ready` has no
  open question or unanswered blocking flag (answers from the review log for v2), that nothing ready rests
  on size or timestamp alone, and that no kept file of a ready unit is inferred at low confidence (hold);
- that no evidence and no decision-record input cites the Class (`T-CLASS`);
- the decision record beside each payload (a v2 payload without one holds): `decision_id` and
  `payload.sha256`, every input's sha256 and bytes, `SKILL.md` at `repo_commit`, the prompt template and the
  rendered prompt, every tool result, the tool calls against this skill's `allowed-tools`, the released
  transcript where there is one, and a clean context (else a hold);
- that a revision drops a blocking flag only on a person's answer its review-log cause cites;
- members that exist in the listing but that no unit kept or excluded (hold).

An error means the triage must be redone. A hold goes to the person's queue with its questions.
`--emit-selection` adds each unit's selection (`selection_sha256` over the listing its record binds) to the
report and appends a `validator_verdict` per decision to the accession's review log.

## Eval

`tests/fixtures/triage-eval/<accession>/` holds the eight trial accessions (ST002798, ST002796,
ST004606, ST003964, ST002283, ST002747, ST004544, ST003128): Catalog rows, the archive listing, and the
ground truth the checker agents established from headers. In the fixtures, units are named by their public
analysis id. To score a triage of one of them:

```text
<python> scripts/validate-triage.py <your triage dir for ST002283>
    --catalog tests/fixtures/triage-eval/ST002283/catalog.json
    --archive-listing tests/fixtures/triage-eval/ST002283/archive-members.tsv
    --ground-truth tests/fixtures/triage-eval/ST002283/ground_truth.json
```

A good pre-download triage gets every unit's `expected_status_pre_download` with no wrong inclusions.
Abstaining (ST004544) leaves files unassigned and scores as left out, which is the right outcome before
headers exist.
