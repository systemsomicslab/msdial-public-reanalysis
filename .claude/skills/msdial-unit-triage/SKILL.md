---
name: msdial-unit-triage
description: Triage one public-repository accession before download - decide, for each Catalog analysis unit, which sample rows and archive members it keeps, from Catalog rows and attributes, mwTab sample and factor lines, folder and file-name tokens and a zip member listing; write one compact spec per accession (msdial-unit-triage-spec.v1) that code expands into one msdial-unit-triage.v2 payload per unit, with a confidence and a rationale for every group of decisions, for scripts/validate-triage.py to check. Use before planning or downloading any accession with more than one analysis unit, or any unit whose rows may mix polarity, chromatography or acquisition.
argument-hint: "<accession> [--triage-dir <dir>]"
allowed-tools:
  - Read
  - Glob
  - Grep
  - Write
  - Edit
  - mcp__msdial-triage-spec__triage_spec_check
---

# MS-DIAL unit triage

Skill version: **1.3.0**.

## Why this exists

A Metabolomics Workbench unit usually lists every sample row of the study and one shared archive. Run as listed, every
unit of a multi-unit accession would take the whole study's files: HILIC and reversed phase, positive and negative,
MS1-only and DDA together. The triage decides, before download, which rows and members each unit keeps. Its failure
modes are written below as rules, and `scripts/validate-triage.py` checks the result deterministically before anything
is planned (docs/llm-assisted-curation.md, stages A to F).

**You decide; code transcribes.** You write one spec per accession: groups of rows and members, each with one
disposition, one set of file fields, one confidence and one rationale, plus exceptions for single items and per unit
its status, flags and questions. `scripts/expand-triage-spec.py` expands it into one v2 payload per unit, filling in
what code knows (hashes, row counts, declared settings, member paths, dates). Never write a payload yourself.

Everything the triage says is a **model inference**. It becomes an accepted decision only when a person approves the
manifest digest that carries it. Never describe it as decided, accepted or confirmed.

## Tools and boundaries

- `Read`, `Glob`, `Grep`: this skill, the spec schema (`schemas/msdial-unit-triage-spec.v1.json` in the working
  directory), the input directory, the output directory.
- `Write`, `Edit`: the spec, `<accession>.spec.json` (a revision: the name the task gives), in the output directory
  only. Write it once; fix what the check reports with `Edit`, which changes only the text it names (writing the whole
  spec again costs as much as the first draft). The last bytes count.
- `triage_spec_check` (server `msdial-triage-spec`): expands your spec over the inputs and runs the validator's
  payload checks; it returns at most 30 findings and per-unit counts. Call it after writing the spec, fix what it
  reports, and call it again until it reports no error. Holds are for a person, not for you to remove.
- No shell, no web, no other tool. Read nothing outside the session's folders: the wrapper refuses a session that does.
- **Read only.** No download, no MS-DIAL run, no Catalog write, no tool call with `confirmed=true`.
- **The Class is not the triage's.** Never read or cite a handoff's `class_proposal` (assignments, labels, contrast,
  status, ratification) or its `class_proposal:*` blocking reasons. Read `handoff.json` only for the unit, the
  accession and `technical_settings`; the rows are in `samples.json` beside it. The sidecar paths a handoff names
  point outside the session: never read them. The validator refuses a triage that cites the Class (`T-CLASS`).
- **Before download means before download.** The archive evidence is the member listing read from the zip central
  directory. A listing written after an extraction is not pre-download evidence: say so in `evidence_sources`.
- What you know beyond the skill, the prompt and the inputs is not evidence. No absolute path, user name or e-mail
  address in a spec: it is released byte for byte. Private libraries play no part in triage.

## Inputs

- `digest.md`: **read it first.** It is a view of the other inputs, built by code: the units and their declared
  settings, one table of every row (row index per unit, varying Catalog attributes, mwTab factors that differ from the
  Catalog, which analysis's Samples line names it, name tokens, and the member it resolves to), the mwTab method lines
  per analysis, the listing by folder with `[row]`, `[twin:Tn]`, `[small:<size>]` (far below its folder's median: a
  failed or cut injection), `[archive]` (an archive inside the archive, unopened) and `[nonraw]` marks, the
  byte-identical groups, the MS-level words of raw names and vendor method folders (MS, MS1, Prof, Profile against
  MSMS, MS2, with counts and examples), and what does not pair. It states facts only, never a grouping.
- The raw inputs: `<unit_id>/handoff.json` and `samples.json`, `<analysis id>.mwtab.txt`, `*.members.tsv`. Read them
  where the digest cuts a value (`[cut; <file> line N]`) or leaves something unclear. **Where the digest and a raw
  input disagree, the raw input wins**; say so in the unit's `notes`.

## Evidence, strongest first

1. **Repository declarations** (`basis: declared`): an analysis's MS_METABOLITE_DATA Samples line (when each analysis
   names its own samples, that settles membership); Catalog row attributes that state the arm (`Mode`, `Chromatography
   ion mode`, `raw data`); mwTab `MS_mode` and other sample data the Catalog does not ingest; the unit's technical
   settings and analysis id; mwTab CH (column, gradient), MS `ION_MODE`, `MS_COMMENTS` (DDA, DIA, inclusion list),
   SAMPLEPREP (separate extracts).
2. **The archive listing** (`archive_listing`): paths, folder layout, sizes, CRC32. CRC32 and size are the only
   pre-download evidence of byte-identical content.
3. **Names** (`basis: inferred`; `folder_token`, `filename_token`): HILIC, C18, RP, RPLC; pos, neg; DDA, DIA, DIA1,
   DIA2, SCAN01..06, tDDA, HERMES, MS1; PRM, SRM, MRM, SIM, tMS2 (a targeted method); batch or date prefixes. A token
   starts a word: `Media` carries no DIA.
4. **Raw headers** (`raw_header`) exist only after preflight (`phase: post_preflight`).

Sizes, timestamps and injection-number parity support a reading the names or declarations already give; they are
never its only basis.

## Confidence and rationale

Each group and exception, and each unit, carries one `confidence` and one `rationale`:

- `high`: the evidence settles it (a declaration names the arm, or tokens and declared settings agree and nothing
  in the listing contradicts them);
- `medium`: the evidence supports it and nothing contradicts it, but no declaration settles it;
- `low`: a reading the evidence allows but does not settle. A kept file inferred at low confidence keeps its unit
  from passing until a person looks; rather than keep such files in a ready unit, ask or abstain.

The rationale is one or two plain sentences: what was decided and which evidence decided it. It is inherited by every
item of the group, so it must be true of every item. **Split a group rather than over-generalise**: a low-confidence
inference stays its own group, and a kept group whose items carry different polarity, chromatography or acquisition
tokens, extensions or top folders is refused unless it states why (`heterogeneous`).

## Procedure

1. Read `digest.md`. Work out the accession's structure once: which folders, prefixes, tokens or declarations mark
   which arm. Write it as `structure`.
2. Read the raw mwTab text and samples where the digest cuts something or is unclear.
3. **Groups of rows** (`applies_to: rows`). Every Catalog row of every unit's table must be matched by exactly one row
   group or one exception. Match with the closed vocabulary of the schema, all predicates together: `sample_id`,
   `raw_file`, `attribute`, `mwtab_samples`, `unit`, `rows`, `resolution`, `resolved_member` (`glob`, `regex`,
   `folder`, `ext`, `token`, `paths`, `twin`), `token`. There is no negation and no catch-all: describe each subset
   positively. Use `resolve.within` when a row's file name exists in more than one folder.
4. **Groups of members** (`applies_to: members`) for every raw-like listed member no row resolves to: pooled QC and
   blanks a unit keeps (only when folder and name tie them to exactly one unit, or `keep: [units]` with
   `shared_reason` when the study really shares one), and exclusions: `archive_metadata`, `not_raw_data`,
   `alternative_format_copy` (an mzML copy beside vendor files), `requires_conversion` (mzXML, mzData),
   `duplicate_content` (a CRC32-and-size twin), `ms1_only_no_msms` (rule 3), and `no_sample_metadata`: a raw file of
   a sample no Catalog row and no mwTab Samples line names (a repeat injection, another submission's run) is left out
   with a detail saying what it seems to be; samples need metadata, so it is never a question. A member is in the
   archive: `not_in_archive` is a row's reason only.
   An `[archive]` member may hold files no row resolves to; a row excluded as `not_in_archive` then waits for a person.
5. **Dispositions.** `keep: <unit>` (with `child` when the unit has children), or `exclude: <reason>` with a `detail`.
   A group one unit keeps appears in every other unit as `belongs_to_other_unit` by itself. A unit's own `exclude`
   list leaves a group out of that unit for its own reason (`needs_person`, `ms1_only_no_msms`,
   `shared_content_unassigned`, `polarity_switching`, ...). These reasons are checked on members as on rows:
   `needs_person` only in a unit that is not `ready` and asks, `ms1_only_no_msms` only with a `confirmed`
   `ms1_only_excluded` flag, never at low confidence and never on a file whose own name says product-ion spectra,
   `shared_content_unassigned` only in a unit excluded for it and only for a file with a twin in another arm's place,
   `polarity_switching` only with its flag.
   A group's own `exclude` applies in every unit, so give `needs_person` in the `exclude` list of the unit that asks,
   and flag `ms1_only_excluded` on every unit (`shared_flags`) for a group excluded as `ms1_only_no_msms`.
6. **File fields** of a kept group: `role`, `ion_mode`, `chromatography` (HILIC, RP, normal_phase, other, unknown),
   `acquisition_mode` (DDA, DIA, SWATH, AIF, MS1, unknown), `basis`; and at least one `evidence` item `{source, field,
   value}`. A value may use placeholders the expander fills per item, so evidence stays true per row:
   `{attribute:<name>}`, `{sample_id}`, `{raw_file}`, `{member_path}`, `{folder}`, `{token:polarity}`,
   `{token:chromatography}`, `{token:acquisition}`, `{mwtab_samples:<analysis id>}`.
7. **Exceptions** for single items: name drift (row `X_Lip_Pos` whose member is `X__LipPos.mzML`: `row`,
   `member_path`, the `group` it joins), or an outlier a group must not sweep in. An exception is one row of a unit's
   table: by `sample_id` or `raw_file` it applies in every unit whose table holds that row; by `index` (indices are
   per unit) it names exactly one `unit`. Every match and target names at least one predicate.
8. **Units**: `status`, `confidence`, `rationale`, `unit_technical`; `exclusion` (`reason`, `detail`) exactly when
   the status is `excluded`; `children` for a unit of both polarities, of more than one acquisition mode, or of
   separately acquired DIA batches; `flags` and `needs_person` naming the `groups` they concern; `shared_with` for a
   deliberately shared file; `notes`.
   A flag raised the same way on several units is written once in `shared_flags`.
9. Write the spec, call `triage_spec_check`, fix every error with `Edit`, and repeat until it reports none.
10. Reply with the single word `done`. The wrapper summarises from the payloads.

## Hard rules

1. **Shared byte-identical content is never settled by name.** When a member a unit keeps has a CRC32-and-size twin
   elsewhere in the listing (another folder, another unit), flag `shared_content` (`confirmed`) on every unit
   involved, naming its `groups`. Example of the failure: two arm-named folders held byte-identical files; the folder
   pointed one way, the headers the other, and keeping one folder's copy by its name put the other arm's data in the
   wrong unit. **Copies in two arms' places exclude both units** (policy of 2026-11-18, P5): until a post-download
   reassignment exists, every unit whose arm holds such copies is `excluded` with `exclusion.reason`
   `shared_content_unassigned`; its affected rows and members are excluded as `shared_content_unassigned`, its other
   files with their own reasons, and the `shared_content` flag stays as evidence, saying what would settle it (the
   mzML `sourceFile`, the elution order after preflight). It asks nothing.
   Which copies count is unchanged. Copies **excluded for good** hold nothing: copies only among excluded members (for
   example an mzXML copy and its twin, both excluded), or one kept file whose twin no unit keeps and which lies in no
   other arm's place (a backup folder, a second export: not in a folder where another unit keeps or holds files, and
   not under another arm's polarity or chromatography tokens). Exclude such a copy as `duplicate_content` and flag
   `duplicate_copies_excluded` (informational), never `shared_content`. A twin in another arm's place stays
   `shared_content`; copies held as `needs_person` or `shared_content_unassigned` count as kept here.
2. **Abstain rather than guess.** When names carry no polarity or chromatography signal (rows such as `Sample_1_1`
   .. `Sample_1_6` with nothing else to go on), set the unit `abstained`: one row group excluded as
   `no_signal_abstained`, and a `no_signal` question saying which header evidence will settle it. A hypothesis such as
   "odd suffixes look positive" belongs in `notes`.
3. **Acquisition, by the policy of 2026-11-18.** The campaign annotates every experimental MS/MS spectrum, so a file
   without product-ion spectra adds nothing to it. None of these is a question:
   - **MS1-only files are left out** (P1): exclude them as `ms1_only_no_msms` and flag `ms1_only_excluded`
     (informational; its detail states the evidence, and its certainty is `confirmed`: the evidence that counts
     settles it). Evidence that counts: mwTab `MS_mode` or `MS:` lines (`MS_COMMENTS`, the method) declaring MS1 or full
     scan for those runs; a name or method-folder word that contrasts with sibling MS/MS runs of the same study (`_MS_`
     against `_MSMS_`, `MSProfile` or `Prof` against `MSMS`; the digest counts them); declared method text. Profile-mode
     MS1 injections of pools are MS1-only files too. When the evidence is weak (a name alone, with no contrasting
     MS/MS sibling and no declaration), keep the file with its declared mode and flag `ms1_only_suspected`: the header
     preflight decides. A file whose own name says product-ion spectra (an MS/MS word such as MSMS, AutoMSMS, MS2,
     ddMS2, MSe, HDMSe, SONAR, MSn or IDA, or a DDA, DIA, SWATH or AIF token) is never left out as MS1-only; if the
     evidence still reads MS1 only, flag `name_token_disputed` and ask. If no file with product-ion spectra is left
     once the MS1-only files are out, the unit is `excluded` (`no_product_ion_files`): its own rows go only as
     `ms1_only_no_msms`, `belongs_to_other_unit`, `not_in_archive` or `duplicate_content`, and a raw file of its arm
     that names product-ion spectra but is dropped for another reason holds it for a person. Keeping an MS1-only file (`acquisition_mode` `MS1`) needs the blocking
     `ms1_only` flag and a person: do not.
   - **Mixed acquisition is split into children** (P2): one child per acquisition mode (DDA, DIA, SWATH, AIF) and
     polarity, and a child of its own for each separately acquired DIA folder or batch (different window schemes,
     batches months apart). Children of one mode each are not a mix. Flag `mixed_acquisition` and ask it only when the
     evidence cannot assign the files to a mode.
   - **m/z-segmented DDA** (`SCAN01`..`SCAN06` files of one sample) is kept as separate files, with the informational
     flag `segmented_acquisition` (P3).
   - **Polarity switching** stays blocking: a unit declared UNSPECIFIED or Both whose files may switch polarity flags
     `polarity_switching` and asks it; such files never go to a run.
   **Inclusion- or exclusion-list DDA is untargeted DDA and in scope** (AcquireX, inclusion-list-prioritised MS2,
   background exclusion lists, tDDA, HERMES): set `acquisition_mode` `DDA` and add the informational flag
   `targeted_dda` (`suspected` or `confirmed`, naming its groups) so reports can note the MS2-selection bias. Never
   ask a question about it. A kept file whose name carries a tDDA, HERMES or targeted ddMS2 token needs that flag.
   A method with no MS1 survey (SRM/MRM, SIM, PRM, tMS2 only) stays `out_of_scope`, and a kept file named so is
   refused. A kept file's acquisition token (MS1, DDA, DIA, SWATH, AIF) must agree with its `acquisition_mode` unless
   `name_token_disputed` is flagged; a group of untokened files that sweeps in one tokened file is refused.
4. **File evidence wins over the Catalog's declared settings** (P4; header-first acquisition since 2026-10-06): when
   mwTab, method or name evidence contradicts the Catalog's declared acquisition, chromatography, polarity or
   instrument model, follow the file evidence and record the disagreement as `declared_technical_disputed`
   (informational, never a question); the raw header decides at preflight. **A folder and a header that disagree go
   to a person** (`folder_header_disagreement`), and so does a name token against a file-level declaration
   (`name_token_disputed`): neither wins silently.
5. **One polarity per run.** A unit of both polarities can be `ready` only with `children`, one per polarity and
   acquisition mode, that partition its kept files. A unit declared positive or negative never keeps the other
   polarity.
6. **No file in two units** unless the study really shares it; then `keep: [units]` with a `shared_reason`.
7. **The Catalog can be wrong.** It does not ingest `MS_mode`, it truncates `Ratio` at `;`, some of its abstentions
   are wrong (a study that declares factors), and some `target_omics` values are mislabelled. Record `catalog_defect`
   or `target_omics_doubt` (informational) and go on. Do not correct the Catalog.
8. **Answers are a person's.** You never write a person's answer or the review log. A blocking flag stays in the spec;
   a unit is `ready` with it only once the review log holds a person's answer to that code.
9. **Revisions.** When the task asks for a revision it names the spec file to write, the current spec, each unit's
   current payload and the review-log entry that causes it. Write the whole spec again, changing only what the cause
   settles: the expansion of a unit the task does not name must stay as it was (`X-SCOPE`). A blocking flag is dropped,
   or a question answered, only on a person's answer to that code, followed as worded.
10. **A unit the deposit cannot serve is excluded, not asked.** A unit with no raw file in the archive (its rows name
    files the listing lacks, or only another unit's: a polarity never deposited) is `excluded` with
    `exclusion.reason` `no_files`; its rows stay `not_in_archive` or `belongs_to_other_unit`. When an unopened archive
    inside the archive may hold them, or a raw file left out (`no_sample_metadata`, `other`, ...) may be one of its
    files under a name the Catalog writes differently (case, a polarity infix), the validator holds the unit for a
    person anyway: resolve such a file to its row instead.
    An excluded unit carries no blocking flag (except `shared_content` in a `shared_content_unassigned` unit): a unit
    in doubt is `needs_person` with the question, not excluded.
11. **What stays a question.** Only what the evidence cannot settle and no rule above decides: which of two batches or
    injections of one sample belongs to an analysis; which archive file is which Catalog row when no name, mwTab line
    or listing ties them; a listing that cannot be read before download; a folder against a header; a name token
    against a file-level declaration; polarity switching; files no evidence assigns to an acquisition mode.

## Status

| status | meaning | validator |
|---|---|---|
| `ready` | can go to the plan: no question open, no blocking flag unanswered, no kept file inferred at low confidence | pass |
| `needs_person` | at least one question in `needs_person` | hold |
| `abstained` | nothing assigned, because names give no signal | hold |
| `out_of_scope` | outside the campaign (polarity switching, GC, SRM); a person accepts the exclusion | hold |
| `excluded` | left out by the policy of 2026-11-18 (`exclusion`: `no_product_ion_files`, `no_files`, `shared_content_unassigned`); keeps, asks and holds nothing | pass; the plan leaves it out (`triage_unit_excluded`) |

## What is checked

The expander refuses a spec that leaves a row or a raw-like member uncovered or covered twice, matches with an empty
predicate, names a group, unit or child that does not exist, has an exception that is not one item, keeps a row whose
file resolves to no single member, leaves a placeholder empty, keeps a heterogeneous group without a reason, or flags
`shared_content` about files no unit keeps or holds for a person. The validator then checks
every payload as it checks any: coverage against the handoff, files in two units, byte-identical twins, exclusion
reasons against the listing and the unit's own flags, polarity and chromatography against tokens and declarations,
acquisition flags, the Class boundary, and status against open questions and answers. It also re-runs the expansion
from the spec your session wrote (`T-SPEC`). An error means the triage is redone; a hold goes to a person. Details:
docs/llm-assisted-curation.md sections 2.2 to 2.4.
