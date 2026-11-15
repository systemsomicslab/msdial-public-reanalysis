---
name: msdial-unit-triage
description: Triage one public-repository accession before download - decide, for each Catalog analysis unit, which sample rows and archive members it keeps, from Catalog rows and attributes, mwTab sample and factor lines, folder and file-name tokens and a zip member listing; write one msdial-unit-triage.v1 file per unit and check the set with scripts/validate-triage.py. Use before planning or downloading any accession with more than one analysis unit, or any unit whose rows may mix polarity, chromatography or acquisition.
argument-hint: "<accession> [--triage-dir <dir>]"
---

# MS-DIAL unit triage

Skill version: **1.0.0** (write it as `decided_by.skill_version`).

## Why this exists

On 2026-11-15 the stage3b campaign was stopped: every unit of a multi-unit accession had run on the
whole study's files. A Metabolomics Workbench unit usually lists every sample row of the study and one
shared archive, so the run admitted HILIC and reversed-phase, positive and negative, MS1-only and DDA
files together. In an 8-accession trial, an agent reading the evidence available before download put 90%
of 1,124 files on the right unit, left 6% unassigned and put 3.8% on the wrong unit. All of the wrong ones
came from a single decision, made by folder name over byte-identical content. This skill is that triage,
with the failure modes turned into rules. The validator then checks the result deterministically before
anything is planned.

The triage is stage A of the campaign design:

| Stage | Who | What |
|---|---|---|
| A | agent + this skill | `triage/<unit_id>.json` per unit (msdial-unit-triage.v1) |
| B | `scripts/validate-triage.py` | pass / hold / error for the accession |
| C | person | answers the `needs_person` questions and B's holds |
| D | plan | manifest carries the triage path and sha256; the person approves its digest |
| F | header preflight | reconciled against the triage; disagreements go back to C |

Everything a triage file says is a **model inference** (`"standing": "model_inference"`). It becomes an
accepted decision only when a person approves the manifest digest that carries it. Never describe a triage
as decided, accepted or confirmed.

## Boundaries

- **Read only.** No raw-data download, no MS-DIAL run, no Catalog write (no Class proposal or abstention
  save, no run record), no Interactive plan or download call with `confirmed=true`.
- **Before download means before download.** The only remote read of an archive allowed here is its zip
  central directory by HTTP Range requests, a few hundred KB per archive at most, with a generic
  User-Agent and no personal details, through a tool built for it. Until that tool exists, use an
  archive-members TSV that is already on disk (and say it was written after an earlier download), or go
  without a listing. Never download an archive to list it.
- **Write only triage files**, into the triage directory you were given (in a campaign:
  `<campaign dir>\triage\`). Never edit a Catalog handoff, a manifest or a ledger.
- Use an existing Catalog handoff (the campaign's `handoffs\<unit_id>\`). If a unit has none, ask before
  calling `msdial_catalog_reanalysis_handoff`.
- Private MSP or LBM2 libraries play no part in triage. Never name their paths in a triage file.
- Be gentle with public servers: one mwTab fetch per analysis, no crawling, no retries in a loop.

## Evidence, strongest first

1. **Repository declarations** (`basis: declared`):
   - the mwTab `MS_METABOLITE_DATA` **Samples** line of each analysis
     (`https://www.metabolomicsworkbench.org/rest/study/analysis_id/<AN>/mwtab/txt`). When each analysis
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

## Procedure

1. **Purpose.** Copy the person's stated `analysis_purpose` into `purpose`, verbatim, or write `null`.
   Never invent one.
2. **Enumerate** every Catalog unit of the accession. For each, record the handoff's sha256
   (`catalog_handoff_sha256`), its row count (`catalog_row_count`) and its declared settings, copied as
   they read, in `unit_technical.declared`.
3. **Read the declarations** above, then the listing, then the names. Work out the accession's structure
   once (which folders, prefixes or tokens mark which arm), then apply it to every unit.
4. **Every Catalog row** goes into exactly one of the unit's `rows` (kept) or `excluded_rows` (with a
   `reason` and a `detail`). A row that belongs to another unit says which (`belongs_to_other_unit`,
   `belongs_to_unit`). Resolve each kept row to its archive member (`member_path`). Match the exact name
   first, then the stem; record name drift (ST002283: row `D4_TS_E_Lip_Pos` is member
   `D4_TS_E__LipPos.mzML`) with the evidence that ties them.
5. **Members no row names** (pooled QC, blanks) go into `added_members` only when the folder and the name
   tie them to exactly one unit.
6. **Account for every raw-looking member** of the listing: kept by some unit, or in some unit's
   `excluded_members` with a reason: `archive_metadata` (`__MACOSX/`, `._*`), `not_raw_data` (`.meth`,
   `.xlsx`, `.Rhistory`), `alternative_format_copy` (an mzML copy beside the vendor files),
   `requires_conversion` (mzXML, mzData), or `duplicate_content`.
7. **Per kept file**, set `ion_mode`, `chromatography` (HILIC, RP, normal_phase, other, unknown),
   `acquisition_mode` (DDA, DIA, SWATH, AIF, MS1, targeted_DDA, unknown), `role`, `basis` and at least one
   `evidence` item `{source, field, value}`.
8. **Flags, questions and status**, by the rules below.
9. **Write** one `<unit_id>.json` per unit and run the validator (below). Fix every error and run it again.
   A hold is not a failure. It is the review queue.
10. **Report** per unit: status, rows kept and dropped, the questions for the person, and the validator's
    verdict. Keep source facts, inferences and open decisions apart.

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
   one per polarity and acquisition mode, that partition its kept files.
6. **No file in two units** unless the study really shares it (one blank serving two arms). Then both
   units declare it in `shared_with`.
7. **The Catalog can be wrong.** It does not ingest `MS_mode`, it truncates `Ratio` at ';', some of its
   abstentions are wrong (ST003964, ST003128 declare Treatment and Genotype), and some `target_omics`
   values are mislabelled. Record `catalog_defect` or `target_omics_doubt` and keep going. Do not correct
   the Catalog.
8. **Say where pre-download evidence came from.** A member listing written after an extraction is not
   pre-download evidence. Say so in `evidence_sources`.
9. **Answers are a person's.** `person_answers` holds only what a named person said in the conversation,
   with their name. The agent never writes an answer for itself.

## Status

| status | meaning | validator |
|---|---|---|
| `ready` | the selection can go to the plan; no question open, no blocking flag unanswered | pass |
| `needs_person` | at least one question in `needs_person` | hold |
| `abstained` | nothing assigned, because names give no signal | hold |
| `out_of_scope` | outside the campaign (MS1 only, polarity switching, GC, SRM); a person accepts the exclusion | hold |

## Validator

```text
<python> scripts/validate-triage.py <triage dir>
    --catalog <campaign handoffs dir>          (or a catalog.json in the eval-fixture shape)
    --archive-listing <archive-members.tsv>    (repeat per archive; omit when there is none)
    [--json] [--ground-truth <ground_truth.json>]
```

Use an absolute interpreter path on this machine, never a bare `python`. Exit status: 0 pass, 1 hold,
2 error. It checks:

- the schema (`schemas/msdial-unit-triage.v1.json`), one accession per directory, files named by unit;
- that every Catalog row is kept or excluded exactly once, matches the handoff row for row, and that the
  handoff sha256 is the one triaged;
- that no file is in two units without `shared_with` on both, and names every set of units that keep an
  identical file set (the stage3b failure);
- CRC32-and-size twins: a kept file with a byte-identical twin anywhere in the listing is an error unless
  `shared_content` is flagged and the unit waits for a person;
- polarity and chromatography of every kept file against its unit, child, declared settings and name
  tokens, and raw-header evidence against the row;
- that MS1-only, targeted, mixed-acquisition and polarity-switching files are flagged, that `ready` has no
  open question or unanswered blocking flag, and that nothing ready rests on size or timestamp alone;
- members that exist in the listing but that no unit kept or excluded (hold).

An error means the triage must be redone. A hold goes to the person's queue with its questions.

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
