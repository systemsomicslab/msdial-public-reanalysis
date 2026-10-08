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
waits for the user's approval, made only where the approved manifest's policy
states `confirmed_discard_fallback: true` (Raw-data deletion in a campaign).
The runner decides no eligibility: it reads the unit's `campaign_disposition`,
which only Interactive's `classify_preflight` writes, and runs, splits, skips
or excludes the unit as that record says. It stops a run on a gate verdict
only as Gate verdicts in a campaign says.

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
- **Acquisition, header first (rule B2).** The user decided it on 2026-10-06.
  In a campaign, each file's acquisition is read from its raw header with the
  raw-metadata extractor the campaign pins, and the header decides. A header
  verdict with product-ion spectra (DDA, DIA, AIF or SWATH) wins over the
  repository's declaration, which is only the Catalog's keyword inference from
  the record's text. The extractor's `confidence` is no reason to set a verdict
  aside: it is a constant for each heuristic branch, not a probability. A file
  whose header leaves its acquisition unknown is excluded with its reason
  recorded, and the rest of the unit runs; a unit with no file left does not
  run. In a unit declared DIA, AIF or SWATH, an MS1-only file is not folded
  into a DDA run. ACQ-1 FAILs a row run as another type than its header gives
  (Interactive #62, gate #31). Interactive's `classify_preflight` applies this
  as the unit's `campaign_disposition`, which lists each excluded input with
  its reason. One case B2 does not reach: where no input's header could be
  read and the repository declares a mode, merged Interactive takes the unit at
  its declaration, warning `acquisition_declared_only`, so every file runs as
  the declared mode (or is excluded where that mode is out of scope) with no
  header behind it. Merged Interactive did this first, following the
  2026-09-30 rule, which read headers only where the metadata left the mode
  unknown; the user decided on 2026-10-08 that such a unit runs so (answer 4,
  option A, "メタデータで解析＋記録": "メタデータの取得モードで解析し、ヘッダーが読めなかったことを記録します。"),
  and the warning, kept in the unit's `campaign_disposition`, is that record.
- **AIF.** The user decided it on 2026-10-07 and 2026-10-08. An AIF unit is
  judged by the MS2 collision energies its inputs record (a Waters LockSpray
  reference function records none, msrawdataworkbench #43). With one energy it
  runs as SWATH (`single_ce_aif_as_swath_2026_10_07`), whatever the Console:
  the user's proposal of 2026-10-06, adopted once the kept pilot data
  (ST004304, MTBKS281) gave the same results. With more than one, the user held
  the unit until a patched Console; it runs as AIF only on a Console that has
  MsdialWorkbench #825, which deconvolutes each energy and represents a peak by
  the energy of its MS/MS reference match, else by the energy with the most
  product ions (the user's choice). On a Console without #825 the unit is held,
  its raw data kept (`aif_multi_ce_awaiting_console`). Interactive #64, #67
  and #69 and gate #32, #34 and #37 implement these rules; Gate verdicts in a
  campaign says how a held unit ends.
  - **Energy sets that differ between inputs.** The user decided on 2026-10-08
    that such a unit runs as is and that the difference is recorded (answer 6,
    option C, "そのまま解析する": "ファイルごとの代表 CE でそのまま解析し、CE がそろっていないことを記録します。"),
    over the hold the agent had recommended (option A, "ユニットごと保留").
    #825 chooses among one file's energies, never across files, so each file
    is processed with its own per-file representative collision energy, and
    the representative energies can differ between files. Interactive records the warning
    `aif_energy_sets_differ_between_inputs`, each distinct set with its file
    count and each input's own set, and ACQ-1 holds each row to its own
    input's set and WARNs, which stops no run (Interactive #69, 0.5.36, and
    gate #37, both open on 2026-10-08). The answer replaces the hold
    `aif_collision_energies_differ_between_inputs` that Interactive 0.5.34
    (#67) and gate #34 implemented, which the user had not decided. Until both
    are merged and the campaign pins Interactive 0.5.36, merged code still
    holds such a unit, and gate #37 merges first: the gate of #34 FAILs ACQ-1,
    a `blocks_run` check, for such a unit run under 0.5.36, so the unit would
    fail, be retried and lose its raw data without a run. A unit the old hold
    kept is taken again by a recheck on the #825 Console once both are in.
  - **Inputs that share a file name.** Two inputs of one unit, in different
    folders, may share a file name and record different sets. Question 6 did
    not cover this case. As first opened, Interactive #69 and gate #37 keyed
    each input's set by its file name alone, so such inputs shared one entry,
    and the unit would have been refused before the Console started, FAILed
    ACQ-1, a `blocks_run` check, and lost its raw data without a run. The user
    decided on 2026-10-08, in the second round of questions (Evidence and
    decisions), that each input's set is keyed by its relative path in
    Interactive #69 and gate #37 before #69 merges (second-round answer 2,
    "A: 相対パスで区別するよう直してから使う"). Interactive #69 now records each input's own set
    (`aif_collision_energies_by_input`) under the input's path relative to the
    unit's raw data root, the manifest's `input_directory` (for a split part,
    its parent's), `/`-separated and compared without case, and names the
    scheme (`aif_collision_energies_by_input_key`
    `path_relative_to_input_directory`); an input directly in the root keeps
    its basename as its key, so records of that shape do not change.
    Interactive's own check before the Console starts looks each input up by
    the same key. Gate #37 reads the record so: `POS/QC_01.mzML` and
    `NEG/QC_01.mzML` are each held to their own set, and a record keyed by
    basename, or by a scheme it does not name, FAILs ACQ-1 for an input in a
    subfolder. Answer 6 then holds for such a unit too. Until both are merged
    and the campaign pins Interactive 0.5.36, merged Interactive (0.5.35) holds
    every unit whose inputs record different sets, raw data kept, whatever
    their names.
  - **An input that records no energy.** The whole unit is held, its raw data
    kept, when any one input that runs records no energy above 0
    (`aif_collision_energy_unrecorded`): a header that lists no energy, lists
    only energies of 0 or below, or has a reference function with an MS
    level. One such file among forty holds all forty. No one energy can be
    shown, and a #825 Console stops on such a file. Merged Interactive held
    so first (#64, #67); the user decided on 2026-10-08 that this hold governs
    (answer 3, option A, "ユニットごと保留": "生データを残して保留にします。").
    It replaces the user's rule of 2026-09-30 that an AIF file with an empty
    collision-energy list gets a recorded warning only. The two read the same
    header field (the extractor's `collisionEnergies`), so Interactive records
    that warning (`aif_collision_energy_targets_empty`) and then holds the
    unit.
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
  it into mzML with every inference the converter offers switched off but the
  polarity imputation the next item states, and records the conversion as
  that input's own provenance (source and output sha256, converter identity,
  validation). The mzML, written into the unit's
  own raw tree under `raw\converted` and never into the download store, is the
  input. An mzXML runs only as the mzML its recorded conversion made: until
  the unit's manifest records that conversion it stays `requires_conversion`,
  and a file whose conversion fails is excluded with its reason recorded while
  the rest of the unit runs. mzData has no converter: where it is the encoding
  a sample has, it is `requires_conversion` and excludes the unit.
- **mzXML without a polarity.** The user decided it on 2026-10-02. A scan
  whose mzXML records no polarity (the attribute is optional, and may be
  `any`) is given one by a campaign's conversion only when the unit's Catalog
  handoff declares exactly one polarity, Positive or Negative, in
  `technical_settings.ion_mode`, and the file records no scan of the opposite
  polarity (the next item). The polarity is then imputed from that
  declared ion mode, and the conversion records the imputation as an
  inference, with the number of spectra it gave a polarity to; CONV-1 holds
  it to the declared ion mode. In any other unit nothing is imputed. The scan
  becomes a spectrum with no polarity, which MS-DIAL would skip as it skips
  any spectrum whose polarity is not the method's ion mode, so CONV-1 FAILs
  the conversion. CONV-1 is a `blocks_run` check (Gate verdicts in a
  campaign), so the unit does not run, and it is a failed unit under the
  deletion rule.
- **mzXML with scans of both polarities.** The runner's default, which the
  user did not object to on 2026-10-03. In a unit whose handoff declares one
  polarity, an mzXML whose scans mix the opposite polarity with scans that
  record none is excluded with its reason recorded, and the rest of the unit
  runs, as a file whose conversion fails is. Its scans without a polarity are
  not given the declared one: the file itself records the other polarity, so
  nothing says which of the two they were. Interactive's conversion makes the
  exclusion and records its reason.
- One project type, ion mode, acquisition mode, chromatography regime, and ion
  mobility regime per MS-DIAL run.
- GC-MS, SRM/MRM, SIM, DI-MS, imaging MS, product-ion-only
  experiments, and unresolved mixed-polarity units are review or exclusion
  cases, not silent conversions. MS-DIAL Interactive can analyze some of these
  modes outside this campaign; that broader capability does not expand this scope.
- LC-IM-MS is excluded from this campaign, which is LC-MS only. Ion-mobility
  data (Bruker TDF/timsTOF, Waters and Agilent ion mobility) are excluded with
  the reason recorded, whether the unit's own repository evidence or the raw
  headers show it.
- **Ion mobility from unit-level evidence.** The user decided it on 2026-10-03
  (option A). A unit counts as ion mobility only on its own evidence: its rows
  or assay fields, such as an instrument that names a timsTOF, Synapt HDMS,
  Vion IMS, Agilent 6560 or Cyclic IMS, or an explicit ion-mobility parameter,
  and the formats of its own containers as the Catalog reads them. A mention in
  the study-level text, a title, abstract or description that many units
  share, is not such evidence. MTBKS217, a Waters Xevo G2 QTOF whose stored ion
  mobility is Enabled only because the shared MS-DIAL 4 lipidome-atlas abstract
  mentions it, is therefore planned, as are MTBKS218, MTBKS221 and the other
  declared MetaboBank units flagged the same way. The plan excludes a unit
  only where its own evidence is ion mobility and nothing else: the Catalog's
  `ion_mobility_evidence` state `enabled`, or, without that helper, an
  instrument or a row's instrument field naming an ion-mobility instrument
  with no Bruker BAF or TSF container among the unit's inputs. A mixed unit,
  whose ion-mobility evidence sits beside containers that hold no mobility
  data, is not excluded at plan time: MTBKS219 and MTBKS220 list Bruker BAF
  folders beside TDF folders, with rows naming a timsTOF. Such a unit passes
  to the raw-header check, where Interactive's per-file reading and split
  exclude the ion-mobility files or parts with their reason, and the rest of
  the unit runs. So does a unit whose own evidence says nothing, and so does a
  unit whose Catalog record the plan cannot read: the plan's own row of a unit
  holds neither its rows nor its inputs, so the unit is never excluded for ion
  mobility on that row, and the manifest says its reading was not made.

## Evidence and decisions

Repository fields and raw headers are evidence. Class, contrast, target omics,
internal-standard ions, and annotation policy are scientific decisions. Keep
source facts, model inferences, and accepted decisions separate.

On 2026-10-08 the user answered eight questions the draft of this amendment
had left open. The user asked for them in a structured question form
("すみません、判断をしないといけない点に関して、A～Dあたりの質問形式で、順番に出してもらえますか？"). The agent put
them in Japanese, one at a time and numbered 1 to 8, each with two or three
options lettered from A, and each option a short label with a description.
The agent marked option A of every question as its recommendation; the user
chose it for seven questions, and for question 6 chose option C over it. This
contract cites each answer by its number and letter and quotes the chosen
option's label, and its description where that carries the rule, verbatim
from the question, without the letter and the recommendation mark. English
beside such a quote is the agent's rendering, not a quote. The options' words
are the agent's; the choice is the user's. Where an answer settles what
merged code did or the agent had read or proposed, the passage says so.

**The second round.** Review after the answers found three points that no
answer of 2026-10-08 covered, and gate #36 had left one choice to the user. The
agent put the four to the user the same day in the same question form, a
second round numbered 1 to 4, and the user answered all four on 2026-10-08.
This contract cites each as a second-round answer by its number and quotes the
chosen option's label exactly as the question showed it, its letter included;
English beside such a quote is the agent's rendering, not a quote. Interactive
#69 (at `35d75ad`) and gate #37 (at `e7b2901`), both open, and gate #36 (merged
as `fe3f346`) implement the answers:

- second-round answer 1, the members of an archive shared by several units that
  no sample row pairs with: "A: 除外して記録する", leave them out of every unit, on record,
  as merged code does (the pairing of sample rows, below), which in a
  campaign's lease ran all the same a shared archive's readable twin of a
  sample's admitted mzXML for that sample (question 1 under Open for the
  user);
- second-round answer 2, a multi-energy AIF unit whose inputs share a file name
  in different folders and record different energy sets: "A: 相対パスで区別するよう直してから使う", key
  each input's energy set by its relative path in Interactive #69 and gate #37
  before #69 merges (Supported production scope, AIF);
- second-round answer 3, the members the implementation left out of a
  unit-scoped archive: "A: mzXMLは変換、同名2形式は1つ選ぶ", in a campaign convert an unpaired mzXML
  like any other mzXML input, of one name in two encodings take one by the
  existing encoding order, keep leaving out a member whose path names the
  opposite polarity and the members a Catalog declaration does not name, and
  keep all of it on record (the pairing of sample rows, below);
- second-round answer 4, whether a manifest planned before the plan stated the
  automatic RT correction stays approvable: the user answered in words of the
  user's own rather than by an option, "なんのことかわからないのですが、デフォルトでは補正ON、ということで良いんじゃないですか？".
  The answer begins by saying the user did not see what the question was
  about. The agent reads the rest as the decision that the correction is on by
  default, so that a manifest stating no correction is never taken as
  uncorrected: approval refuses every manifest that carries no automatic RT
  correction statement, with no exception for one planned before the
  statement, and the person plans again (the automatic RT correction, below).

**The extra question.** Review after the second round found a case that
neither answer 3 nor the rule of 2026-09-30 covered: a sample row admits an
mzML that RawDataHandler cannot decode, and a twin of the same sample, a
vendor file, folder or container or an mzXML, can be read. Interactive #69 as
it then stood (`4722776`) took the twin in the mzML's place, a vendor twin as
an unattributed input outside the sample's Class and an mzXML twin, converted,
as the sample's own input by its converted name. The agent put the case to the
user on 2026-10-08 in the same question form, and the user answered
"A: 読める方をそのサンプルとして使う". This contract quotes the label exactly as the question
showed it, its letter included, and reads it as it was put: the readable twin
runs as that sample's own input, paired to its sample row and in its Class,
never as an unattributed input, and the undecodable mzML is recorded with that
reason. Interactive #69 implements it from `06d2891` on, and gate #37 holds the
run to its record (the pairing of sample rows, below).

**Open for the user.** What is open for the user is the approval of this
amendment's wording as a whole, and four questions about a readable twin that
the wording does not settle and approving it does not answer. Where the
wording states what Interactive #69 or gate #37 does in a case no answer
covered, it says that this is the agent's reading in the code, kept from the
behaviour before the answers where it was so, which within one folder it was
and across folders it was not (the pairing of sample rows, below): an encoding
order that ranks two encodings of a sample equal where no sample row admits
either, copies of one encoding taken nearest the raw data root, and a sample
row's admission over an order that prefers its twin. Approving the
wording leaves each as the code does it, on record, until the user says
otherwise. The four questions are the user's to answer. In each, two answers
of the user's meet, or the extra answer says to use the readable twin and does
not say which or whether it reaches the case; the agent does not choose between
them. Until the user answers, the behaviour before #69 is the one to keep,
on record, wherever no answer of the user's rules it out. That behaviour is
not one rule: Interactive 0.5.35 told one name in two encodings apart only
within one folder, so across folders (`RAW/x.raw` beside `mzXML/x.mzXML`) it
ran, as an unattributed input, a twin that within one folder it left out, and
in a campaign's lease it ran a shared archive's readable twin of an admitted
mzXML for its sample. Each question says what ran before #69 and what #69
does.

1. A readable twin of an admitted mzML that cannot be decoded, in an archive
   shared by several units. Second-round answer 1, "A: 除外して記録する", leaves out of
   every unit the members of a shared archive that no sample row pairs with,
   as merged code does; the extra answer, "A: 読める方をそのサンプルとして使う", runs the readable
   twin of an admitted mzML that cannot be decoded and names no archive's
   scope. The merged code answer 1 endorsed did not leave out every such
   member: in a campaign's lease Interactive 0.5.35 already ran a shared
   archive's readable twin of a sample's admitted mzXML for that sample (the
   convert stage's `encoding_choice`, `stands_for` the mzXML), while its record
   listed the same member as left out (`two_encodings_of_one_name`, or
   `shared_archive` across folders). Interactive #69 at `35d75ad` keeps running
   that twin, records it as `analysed_for_an_admitted_sample`, and gate #37's
   INP-1 accepts it; it does not hold #69. The twin of an undecodable mzML no
   version before #69 ran from a shared archive, in one folder or across
   folders. Interactive #69 at `35d75ad` runs it for its sample, and gate #37
   at `e7b2901` accepts it where its records agree, whatever the archive's
   scope. Until the user answers, that twin is to stay out, as before, and #69
   does not merge running it before the user answers (Merges).
2. A readable twin of a declared mzML that cannot be decoded, in a unit whose
   Catalog declared its inputs, where no declaration names the twin. The extra
   answer makes no exception for a declared unit; second-round answer 3 keeps
   out the members a declaration does not name. #69 keeps the twin out, on
   record. In a campaign the convert stage already analyses a readable twin no
   declaration names in a declared mzXML's place, a precedent #69 names for
   running it.
3. Two readable twins of an admitted mzML that cannot be decoded, which the
   encoding order ranks equal (`S1.raw` and `S1.d`). The extra answer does not
   say which to use. #69 takes neither, and the sample has no input. Within
   one folder that is as before (both `two_encodings_of_one_name`). Across
   folders (`RAW/S1.raw` beside `WIFF/S1.wiff`) Interactive 0.5.35 ran both as
   unattributed inputs, with no sample row, which second-round answer 3 (take
   one) and the extra answer (never as an unattributed input) both rule out,
   so no behaviour before #69 is left to keep, and #69's is the one kept until
   the user answers.
4. A readable twin of an admitted mzXML outside a campaign, where nothing
   converts the mzXML. The extra answer names an mzML that cannot be decoded,
   and second-round answer 3 the members a unit-scoped archive left unpaired.
   #69 keeps the twin out (`admitted_mzxml_not_converted`), and the sample has
   no input. That is as before only where the twin lies in the mzXML's
   folder (`two_encodings_of_one_name`). In another folder (`RAW/S2.raw`
   beside `mzXML/S2.mzXML`) Interactive 0.5.35 saw no twin, and in a
   unit-scoped archive ran it as an unattributed input, with no sample row;
   #69 at `35d75ad` no longer runs it, so data 0.5.35 analysed is not
   analysed. No answer of the user's rules the earlier behaviour out, and #69
   does not merge with that change before the user answers (Merges).

Before selecting units or proposing Class, ask what the user wants to learn from
the reanalysis. Record the scientific question, intended biological comparison,
whether annotation or comparative profiling is central, and required outputs as
`analysis_purpose`. Use that purpose consistently for Class, contrast, library,
QA, and reporting decisions. Do not proceed to download while it is missing.

For production repository runs, use the zero-threshold diagnostic before the
main run. Select a QC nearest the analytical-order midpoint, or a non-blank
sample nearest that midpoint when no QC exists. Set Minimum peak height to keep
approximately 3,000-6,000 peaks, and aim for the lower end of that range: the
highest threshold whose estimated count is still at least 3,000 (the user's
decision of 2026-10-06, "High qualityのMS2を取りたい"; gap-filling recovers
peaks below the threshold). Search in the instrument family's step, 100 for
QTOF-type data and 1,000 for Fourier-transform data, the family read from the
file before the repository's text. Only when the diagnostic count is above
6,000 and no step of the family lands in the range, fall back to 10 (for
Fourier-transform data, 100), never finer, and record the fallback and its
reason. Keep 0 when the diagnostic count is at most 6,000. Use
`TimeBasedLinearWeightedMovingAverage` and retain the method, representative
sample, diagnostic count, threshold step, any fallback, accepted threshold and
the production run's own peak counts in provenance: in the first pilot,
production kept 61-88% of the diagnostic's estimate. Interactive #61 implements
the step rule, and #59 runs the diagnostic without annotation libraries, which
leaves its count unchanged; PKH-1 holds the step and its floor (gate #31).

In a campaign, automatic alignment RT correction is on: MsdialWorkbench #826's
local outlier test at its default window of 1.5 min, with 12 anchors (the
user's decision of 2026-10-07, "12で確認解析を回してください。#826のマージもOKです。",
after an evaluation on MTBLS417 and confirmatory runs on MTBKS236 and
MTBKS217). The runner sends it to every Console start, and a manifest whose
profile says otherwise, or whose Console lacks #826's local test, is not
approvable (Interactive #65, gate #33). The campaign policy can still be
overridden at plan time (`plan --policy`): an override may turn the correction
off or change the anchor count, and it is not refused. Gate #36 (merged as
`fe3f346`) makes such an override visible to the person who approves: the
plan summary states the correction that will run and where the setting came
from, a difference from this decision is the summary's second line, marked
`!!` and repeated on stderr, and the manifest records the overridden fields
(`policy_overrides`) and the statement itself, so the sha256 digest the person
approves covers them. A manifest whose stored statement no longer matches its
own policy and profile is not approvable. The user decided on 2026-10-08
that the correction is on by default (second-round answer 4, above): gate #36
refuses the approval of every manifest that carries no automatic RT correction
statement, with no exception for one planned before the statement and whatever
its policy and profile say, and tells the person to plan again, while a
manifest planned now with the default policy states the correction on, pinned
by the campaign policy. Two defaults the user accepted as
recommended on 2026-10-07 ("推奨でお願いします"): when the Console cannot
select anchors and exits before alignment, the unit's later attempts run
uncorrected, on record (`automatic_rt_correction_fallback`); and a Blank is
interpolated by analytical order only where that order is recorded, from the
raw headers' start times or the repository's sample table, and otherwise keeps
its measured RTs. The zero-threshold diagnostic never corrects.

For Class proposals, include one assignment per sample, selected source fields,
the intended contrast, rationale, and warnings about confounding or missingness.
Do not use continuous fields merely because they are available.

A sample row is paired with its raw file by its exact name, by that name behind
a prefix, or, since the user's decision of 2026-10-06, by a leading identifier
token that matches exactly one file (`VV_13` with `VV_13_..._exp344`). Every
pairing not made by the exact name is an inference, and the user asked that
each be kept on record ("必ず記録として残してください"): the input lineage
records how each input was paired (`name_pairing.paired_by`), and PAIR-1 lists
every inferred pairing (Interactive #58, gate #31). For ST001264, where 3 of 31
rows paired, the user asked on 2026-10-07 that the unit be analysed
"無理やりにでも". The agent proposed a rule for it, which Interactive #64 and
gate #32 implement: in a unit whose download is its own alone, an archive
member that no row pairs with is an input all the same
(`unattributed_member`), in the abstention's Class or `Unattributed`, on
record, and the members of an archive shared with other units are left out,
on record, and INP-1 FAILs any that reach a run. Question 2 of 2026-10-08
asked what to do where only some of an archive's files pair with the sample
table ("ST001264 のように、アーカイブ内のファイルの一部しかサンプル表と対応が取れない場合、どうしますか？"), and the
user accepted it on 2026-10-08 so far as the question reached (answer 2, option
A, "出所不明の入力として含める": "対応が取れないファイルも、出所不明の入力として解析に含め、記録に残します。"). That settles the
unit-scoped half, for the members the implementation takes. The question did
not distinguish an archive that is the unit's own from one shared with other
units, and read literally the answer could reach a shared archive's members
too. Leaving those members out was the implementation's rule, and no decision
of the user's stood behind it until the second round: the user decided the
same day that they are left out of every unit, on record (second-round answer
1, "A: 除外して記録する"), as merged code does. Interactive records them in the manifest's
`unattributed_members`, with `applied` false, the reason `shared_archive` and
each member in `left_out`, and INP-1 FAILs any that reach a run. Interactive
leaves out the same way, with the reason `download_scope_not_unit_scoped`, the
members where the Catalog's download scope does not show the download to be
the unit's own. Two kinds of member of such an archive run all the same. In a
campaign's lease, where a sample row admits an mzXML and a readable twin of it
came out of the archive, the convert stage analyses the twin for that sample
(`encoding_choice`, `stands_for` the mzXML), whatever the archive's scope.
Interactive 0.5.35 already did so, while its record listed the same member as
left out (`two_encodings_of_one_name`, or `shared_archive` across folders);
Interactive #69 records it as `analysed_for_an_admitted_sample`, and INP-1
does not FAIL it. And in Interactive #69 at `35d75ad`, the readable twin of a
sample's admitted mzML that cannot be decoded runs for that sample, whatever
the archive's scope, which no version before it did, and INP-1 does not FAIL
it. Whether answer 1 or the extra answer governs that twin is question 1
under Open for the user, above.

Within a unit-scoped archive, too, Interactive #64 did not take every member no
row pairs with. It left out, on record (`unattributed_members.left_out`, each
with its reason), an mzXML or another member that only converts to mzML
(`requires_conversion`), which a campaign's conversion did not convert either;
a member whose path names the polarity opposite to the unit's ion mode by a
token of its own (`polarity_token_contradicts_ion_mode`); and a member whose
name another member carries in another encoding (`two_encodings_of_one_name`),
so that one name in two encodings MS-DIAL opens, neither paired, left both out.
It compared each member's whole path below the data root less its suffix, so
it saw one name in two encodings only within one folder: across folders
(`RAW/x.raw` beside `mzML/x.mzML` or `mzXML/x.mzXML`) it saw no twin, and a
member no row paired ran as an unattributed input, with no sample row, beside
or instead of the encoding a row admitted. Interactive #69 pairs encodings
across folders as the convert stage does (below), so where an item says that
#69 keeps the behaviour before it, that holds within one folder, and the item
says what changes across folders.
It took none where the Catalog declared the unit's analysis inputs. No decision
of the user's stood behind these exclusions either, until the user decided on
2026-10-08 (second-round answer 3, "A: mzXMLは変換、同名2形式は1つ選ぶ"). Interactive #69 and gate #37
implement it, and the user's answer to the extra question of the same day
(above). Where they decide a case neither answer covered, the item says that
it is the agent's reading in the code; where the case is one of the four
questions the answers leave to the user, the item names the question and what
the code does until the user answers it:

- **An unpaired mzXML.** In a campaign's lease it is converted like any other
  mzXML input, as the rule of 2026-09-30 converts mzXML-only data: the mzML
  written from it is the unattributed input, its lineage row carries the
  conversion and the mzXML's name (`name_pairing.member_name`), and
  `unattributed_members.converted` lists it. Outside a campaign nothing is
  converted, and it stays left out as `requires_conversion`.
- **One name in two encodings.** Members are one sample's where their names
  agree once the container suffix is set aside and their folders agree once
  the words naming an encoding are, so `RAW/x.raw` and `mzML/x.mzML` are one
  sample and `POS/x.raw` and `NEG/x.raw` are two. Of one sample's unpaired
  encodings, the existing encoding order (a vendor folder or container, then
  mzML, then mzXML) takes one, and each other is left out as
  `chosen_other_encoding`, with the one taken (`chosen`, `chosen_by`
  `encoding_order`). That much is the answer. Copies of one encoding in two
  folders (`x.raw` and `RAW/x.raw`) count as one: the copy nearest the unit's
  raw data root, then the first by path, is taken, the agent's reading where
  the order cannot tell copies apart, and each other copy is left out as
  `copy_of_the_chosen_member` (`chosen_by` `nearest_the_data_root`, or
  `admitted_by_the_unit` where the copy taken is the unit's own). A `chosen`
  names the member the lease takes to run for the sample, decided before
  anything is converted or its raw header read, and never one the lease has
  already set aside (an mzML that cannot be decoded, or an mzXML a lease
  outside a campaign does not convert). Nothing re-points it afterwards: where
  the member chosen does not run after all, because its conversion fails (an
  mzXML whose scans mix the opposite polarity with scans of none) or the
  per-file raw-header preflight excludes it (a header that leaves its
  acquisition unknown, or ion-mobility data), the convert stage's or the preflight's record says so, each other
  encoding of the sample still names it as `chosen`, and the sample can have
  no input. A twin's own record shows it (`replacement_excluded`, below).
  An mzML RawDataHandler cannot decode competes with no other encoding of its
  sample, since a convertible mzXML outranks an unreadable twin (the rule of
  2026-09-30), and its record says that it could not be decoded whatever stands
  beside it. An unpaired one is left out as `chosen_other_encoding` with
  `chosen_by` `undecodable_mzml_set_aside`, `chosen` naming what runs for the
  sample, wherever another encoding of the sample runs, an unpaired twin or the
  unit's own admitted encoding alike; and as `undecodable_mzml`, with nothing
  chosen, where nothing of the sample runs: the order ties between its readable
  encodings, none of them can be decoded, or the unit's own is an mzXML that a
  lease outside a campaign does not convert. Alone, with no other encoding of
  its sample, it is an unattributed input the lease excludes
  (`unattributed_members.excluded`, `unsupported_mzml_encoding`).
  Where a sample row admits a readable encoding of that sample, that one stays
  the sample's input, and an unpaired twin is left out as
  `chosen_other_encoding` naming it, with `chosen_by` `encoding_order` where
  the order puts the admitted one first and `admitted_by_the_unit` where the
  order puts the twin first or ranks the two equal, since there the admission
  chose. Keeping the admitted one over a twin the order prefers is the agent's
  reading, kept from the behaviour before the answer within one folder; across
  folders Interactive 0.5.35 ran the twin as well, as an unattributed input,
  which answer 3 (take one) rules out. In a campaign, where
  every encoding a sample row admits is an mzXML and the order puts one
  readable unpaired twin before it, the convert stage analyses that twin
  instead (`analysed_for_an_admitted_sample`, with `stands_for` the mzXML), and
  each other encoding names the twin as chosen. Outside a campaign nothing
  converts an admitted mzXML, and a readable twin of it is left out as
  `admitted_mzxml_not_converted` (`twin_of` the mzXML, `twin_of_reason`
  `requires_conversion`), so that sample has no input, unless the row pairs by
  a leading identifier token, where the readable encoding is paired in the
  mzXML's place. Whether such a twin runs for its sample outside a campaign
  neither answer says: it is question 4 under Open for the user. Within one
  folder #69 keeps the behaviour before it; across folders it no longer runs a
  twin Interactive 0.5.35 ran as an unattributed input, and it does not merge
  with that change before the user answers (Merges).
  Where the order cannot choose and no sample row admits either, as between
  two vendor containers of one name, no encoding is taken: both are still left
  out as `two_encodings_of_one_name`, and that sample is not analysed, also
  the agent's reading, kept from before within one folder; across folders
  0.5.35 ran each as an unattributed input, which answer 3 (take one) rules
  out.
- **A readable twin of an admitted mzML that cannot be decoded** (the extra
  question, "A: 読める方をそのサンプルとして使う"). Where a sample row admits an mzML RawDataHandler
  cannot decode and a twin of that sample can be read, a vendor file, folder or
  container or, in a campaign's lease, an mzXML the convert stage converts, the
  twin the encoding order takes runs as that sample's own input, paired to its
  sample row and in its Class, never as an unattributed input. Its lineage row
  carries `replaces_undecodable` (the mzML's `path`, `reason`
  `undecodable_mzml`, `rule` `readable_twin_runs_as_the_sample_2026_10_08`)
  and takes the mzML's sample and pairing; the mzML's excluded row
  (`unsupported_mzml_encoding`) names the twin as `replaced_by`;
  `unattributed_members.left_out` lists the twin as
  `analysed_for_an_admitted_sample`, with `stands_for` the mzML and
  `stands_for_reason` `undecodable_mzml`; and
  `unattributed_members.replaced_undecodable` lists the mzML with
  `replaced_by` and either `replaced_by_input`, the input that runs, or
  `replacement_excluded` where the twin did not run (its conversion failed).
  Where the order ties between readable twins, none is taken, and outside a
  campaign an mzXML twin stays left out (`requires_conversion`); either way the
  sample has no input. Which of two twins the order ranks equal should run, if
  either, the answer does not say (question 3 under Open for the user, above).
  A twin in two folders is one twin, as above. Before #69, within one folder,
  such a sample had no input: Interactive 0.5.35 left a vendor twin out as
  `two_encodings_of_one_name` and an mzXML twin as `requires_conversion`.
  Across folders (`RAW/S1.raw` beside `mzML/S1.mzML`) 0.5.35 saw no twin and,
  in a unit-scoped archive, ran a vendor twin as an unattributed input, with no
  sample row; #69 runs it as the sample's own, as the answer has it. At
  `4722776`, #69 ran a vendor twin as an unattributed input outside the
  sample's Class and listed it in `taken_instead_of_undecodable`, a list its own
  count contradicted; that list is gone. Beside a shared archive, too, #69 at
  `35d75ad` runs the twin for its sample, which no version before it did,
  although second-round answer 1 leaves a shared archive's unpaired members
  out of every unit: whether the twin runs there is question 1 under Open for
  the user, and until the user answers it the twin of an undecodable mzML that
  is a member of a shared archive is to stay out, as before (Merges). A shared
  archive's readable twin of an admitted mzXML, which the convert stage
  analyses for its sample in a campaign, ran before #69 and still runs (the
  archive shared by several units, above).
  Gate #37's INP-1 accepts a twin only
  where the four records above agree and the twin is an encoding of the mzML it
  replaces, and FAILs a twin that runs unattributed, as no sample or as another
  sample, and an input that runs for an undecodable mzML with no record; PAIR-1
  lists each twin (`readable_twins`) and WARNs, and CONV-1 holds a converted
  twin to the mzXML its record names.
- **A member whose path names the opposite polarity** is still left out, on
  record (`polarity_token_contradicts_ion_mode`).
- **A unit whose Catalog declared its inputs** takes the declared ones,
  matched by path, and no member as unattributed. One kind of member no
  declaration names runs all the same, as it did before #69: in a campaign,
  where a declared mzXML has a readable twin that came out of an archive, the
  convert stage analyses the twin in the mzXML's place by the encoding order
  (`encoding_choice`, `stands_for` the mzXML), as for any mzXML input.
  Interactive #69 records the members no declaration names: each analysable
  member that no declaration names and that reaches nothing of the lease is in
  `unattributed_members.left_out` with the reason
  `not_named_by_the_catalog_declaration`, its basename and its path under the
  data root, and `unattributed_members` gives `applied` false, the reason
  `catalog_declared_inputs` and `count` 0. Before #69 such a unit's manifest
  carried no `unattributed_members`, and the archive's member listing (its
  extraction record's `members_tsv`) was the only trace of those members.
  Where such a member is a readable twin of a declared mzML the lease excluded
  as undecodable, its entry says so (`twin_of`, `twin_of_reason`
  `undecodable_mzml`), and it does not run. Second-round answer 3 keeps out the
  members a declaration does not name; the extra answer runs the readable twin
  of a sample's admitted mzML and makes no exception for a declared unit, and a
  declared mzXML's twin already runs, as above. Which governs is question 2
  under Open for the user. Until the user answers, #69 keeps the declaration,
  as before, and gate #37 FAILs INP-1 for such a twin that runs.

INP-1, a `blocks_run` check, FAILs a left-out member that reaches a run, but
for a twin recorded as `analysed_for_an_admitted_sample`, which runs as its
sample's own input; a converted unattributed input that is not the conversion
of the mzXML it names; and one sample run in two encodings beside an
unattributed member. PAIR-1, `record_only`, holds the record itself to what
ran, and each left-out reason to what it must give (gate #37). Interactive's
0.5.31 changelog and the gate's PAIR-1 docstring called the rule the user's
decision a day before the user accepted it.

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
A pilot is such a manifest of named units (`scripts/campaign-runner.py plan
--units`), drawn from both pools, each held to its own pool's rules and
recorded with it. The user started one on 2026-10-03, 15 units with their raw
data kept, so its approval covers boundaries 1, 3 and 4 and the split, never 5.
On 2026-10-08 the user decided that pilot's approval be revoked (answer 7,
option A, "取り消してよい"), and it has been revoked. The pilot run again on the
software of 2026-10-08 covers the first pilot's 10 units that did not end
done (answer 8, option A, "未完了の10ユニット": "前回完了しなかった10ユニットを新しい構成（自動 RT 補正 ON）で回します。"), planned from it with
`plan --replan-from`; it is a new manifest with its own approval. The first
pilot's profile, which turned RT correction off, is no longer approvable
(Evidence and decisions). The production campaign starts only
on the user's explicit go, asked for in the conversation once everything is
ready: "本番開始前には私がGoサインを出すので、聞いてください。" (2026-10-01).
No approved manifest, pilot approval or authorization record is that go, and
the runner's `run` is never started on a real campaign without it.

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

The production Console is a local build that is never published: MsdialWorkbench
master with #825 and #826, and a local, unobfuscated RawDataHandler 1.3.9776.346
built from msrawdataworkbench master with #42 and #43 (the user, 2026-10-07:
"まだ公開しませんし、大丈夫です"). The raw-metadata extractor is the build of
msrawdataworkbench 5f60446 (Interactive #68, gate #35); on 21 real files its
verdicts equal those of the a12293c61 build it replaces. Each is pinned by its
sha256, never by its location. The campaign plans on
the Catalog as last crawled, on 2026-09-21 and 2026-09-22, and no crawl is
started unless the user asks (2026-10-08: "再クロールは必要ないです！").

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
that discard and passes it no `confirmed=true`. Interactive has had it since
0.5.22.

**Fallback, awaiting the user's approval.** With an Interactive that lacks
the approval-taking discard, the runner may call the discard with
`confirmed=true`, but only in a
campaign whose approved manifest states `confirmed_discard_fallback: true` in
its policy, and only after `campaign_authorization.authorize` has accepted
boundary 5 for that unit and the crossing is recorded in the unit's manifest.
That field carries the user's approval of the fallback: the approval's digest
covers the manifest's policy, so approving a manifest that states it approves
the fallback for that campaign, and a session that asks for the approval names
the field and its value. The field is absent, or false, unless the user asks
for it, and then the runner never makes that call: the raw data of a failed,
skipped or excluded unit stay, with the reason
recorded in the unit's failure record and its bytes counted against the disk
budget. Either way the runner never makes that call where Interactive's
discard takes the approval.

For a failed unit whose output holds an mzTab-M, because the mzTab-M did not
validate or because the run missed another planned export, the deletion rule
wins: once its retries are spent its raw tree is discarded, and the mzTab-M,
its validation and whatever else the run left in `output` are kept as the
failure record's artifacts. The approval-taking discard accepts such a unit
and keeps them (`failure_artifacts`); the `confirmed=true` discard refuses
any unit whose output holds an mzTab-M, and cleanup accepts only a validated
run. Where no discard accepts it, its raw data stay, the refusal is recorded as
the reason in its failure record, and its bytes count against the disk budget.
The runner never works around the guard, by deleting the mzTab-M or the raw
tree itself.

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
user's decision. The rule of 2026-10-01 stops a run only for a FAIL that breaks
the results, and on 2026-10-02 the user placed every check the gate then ran
in one of two lists (PAIR-1, added on 2026-10-06, is placed below):

- `blocks_run`: ELIG-1, ACQ-1, SUM-1, CNT-1, INP-1, ID-1, PRE-2 and CONV-1. A
  FAIL in one of them breaks the results, so it stops the run, and the unit is
  a failed unit under the deletion rule: it is retried twice, and its raw data
  are then deleted.
- `record_only`: CLS-1, CLS-2, CLS-3, ORD-1, PKH-1, SPL-1, PAIR-1 and PRE-1. A
  FAIL in one of them is recorded with the unit, and the unit runs. Its raw data
  then go once its outputs are present and its mzTab-M validates, so a Class,
  grouping, order, threshold or split-coverage error it carries is corrected
  only from a new download. PRE-1 never FAILs: it reports PASS, WARN or
  not_evaluable. PAIR-1 came with the decision of 2026-10-06 that every
  inferred name pairing be kept on record; the gate placed it here as the
  agent's reading of that decision (gate #31), and the user decided that place
  on 2026-10-08 (answer 1, option A, "記録だけ": "不一致は必ず記録に残し、そのユニットの解析は続けます。").

The gate states each check's list as its `run_policy`, and the runner holds
the `blocks_run` list itself, whatever a report states.

**In neither list.** No check, since 2026-10-02. The gate's `RUN_POLICY`
places every `before-production` check it runs as the two lists do, and its
tests refuse one the user has not placed. A check the gate adds to
`before-production` is named here until the user places it.

**Not evaluable.** A `blocks_run` check left `not_evaluable` where it is
required stops the run exactly as its FAIL does. The unit is then a failed
unit, retried twice before its raw data are deleted. A required check is one
`--strict` counts: the report lists it in `strict_failures`, and the gate's
`run_blocked_by` names it beside the FAILs. A check left `not_evaluable` where
it is not required never stops the run, such as INP-1 for a unit that declares
no analysis inputs, and neither does a `record_only` check left
`not_evaluable`. Either is recorded with the unit, and the unit runs.

**No report.** Before production, a gate that gives the runner no usable
report holds the unit. That is a gate that gave no answer within the runner's
time limit, that crashed or exited with a code other than 0, 2, 3 and 4, that
exited 3 (the workspace is unusable), that printed output that does not parse
as a report, or that the runner could not start. Without a report the runner
cannot tell whether a `blocks_run` check FAILed, and the gate's silence is not
the unit's failure. A held unit is not run and keeps its raw data, and it is
counted neither as a retry nor as a failure. Its state is `gate_held`, with a
warning in the ledger and in the status export, and every other unit goes on.
The runner runs the gate again for held units at each of its starts and every
few hours. A held unit is therefore not idle: `run --until-idle`, the
scheduled task's command, does not return while a unit is held, and the
runner sleeps between polls until the recheck is due. A unit that stays held
keeps the runner going until a recheck gives a report or an operator skips
the unit. An operator can ask for the recheck at once with
`scripts/campaign-runner.py recheck-held`. Like every request, it is acted on
by the runner that holds the campaign, and the command says when no runner
does and one must be started. Once a report exists, the unit goes on as any
other. After the run, at `pre_cleanup` and `final`, a missing report is only
recorded, and the raw data go by the deletion rule whatever the gate says.

**A stop is per unit.** A FAIL, a check left unevaluated or a hold stops that
unit's analysis, never the runner, which goes on with the other units. A pin
change, a short disk, a repository outage and an Interactive backend that does
not answer pause the whole campaign instead: the four pauses the status export
names, each what every unit would meet alike. Each pause lifts by itself once
its cause has gone, the last two at the runner's hourly recheck. The user's
decision of 2026-10-02 names the first three, and the backend pause is the
runner's default, which the user did not object to on 2026-10-03. A backend
does not answer when a call to it is refused, times out, or is reset or broken
off mid-reply, whichever call it is, a job's poll or the diagnostic's estimate
among them, and none of these counts against the unit.
Nothing one unit does pauses the campaign. A reply or a record of Interactive's
that the runner cannot read or act on for one unit holds that unit as
`contract_held`, as the no-report rule holds a unit whose gate gave no report:
the second default the user did not object to on 2026-10-03. It covers a reply
that does not parse, a missing or malformed `campaign_disposition`, one that
nothing applied, one another extractor made, an extractor Interactive refuses
or does not find, and a reply of another shape. The unit goes no further,
keeps its raw data, is counted neither as a retry nor as a failure, is warned
about in the ledger and the status export, and is rechecked as a `gate_held`
unit is. A job's poll whose reply cannot be read is never taken for a lost
job: the unit waits while the job's Console or download still runs, keeping
the Console slot, and is then held at that poll, so no Console is started on
it again until a recheck reads how the job ended. A cleanup or discard that
Interactive cannot make as called leaves that unit's raw data held, looked at
again at each start and every few hours, and the unit ends as it was going
to.

**Held by Interactive.** A unit whose `campaign_disposition` holds it (the AIF
holds, Supported production scope) is `disposition_held`: not run, its raw
data kept, counted neither as a retry nor as a failure, and named apart in the
status export. Unlike `gate_held` and `contract_held` it is not rechecked by
itself and keeps no runner going. `recheck-held --unit` or `recheck-held
--disposition-held` makes its preflight again, which runs it where the pinned
Console now allows; a campaign pinned to another Console takes it again only as
a new manifest (`plan --replan-from`). Otherwise only an operator's `skip`
releases the hold, and its discard records `disposition_hold_released_by`
`operator_skip` before deleting the raw data; a split parent's raw data go
only once every held part has been skipped so (gate #32 and #34, Interactive
#64). The runner does not check who skips (`skip --by` is optional), and the
skip deletes raw data the hold keeps, as the user ordered for multi-energy AIF
on 2026-10-07. So a skip that releases a `disposition_held` unit is made only
on the user's explicit word in the conversation for that unit; the campaign
approval's boundary 5 does not cover it. The agent read the user's "raw kept"
so, and the user decided it on 2026-10-08 (answer 5, option A, "1件ずつ先生の了承":
"スキップ（生データ削除）は1件ごとに了承をいただきます。修正後の Console での再判定（recheck）は了承不要です。"):
a recheck deletes nothing, so
`recheck-held` is made without asking.

The gate lifts none of Interactive's own refusals: a unit Interactive refuses
to start does not run, whatever the gate said.

The runner reads which checks failed, and which were left unevaluated, from
the `checks` of the gate's `--json` report, never from the exit code. The exit
code is 2 for any FAIL, in either list, and a FAIL outranks a strict refusal,
so one `record_only` FAIL gives 2 however many required checks were left
unevaluated. Outside a campaign every `before-production` refusal still stops
the unit.

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

## Merges

Since 2026-10-07 the user has left the merging of the campaign's pull requests
(this repository, Interactive, the Catalog, msrawdataworkbench) to Claude, once
a review comes back clean and the change is validated
("マージについては、お任せできますか"; for msrawdataworkbench,
"PR/Mergeはお願いします"). This replaces the rule of 2026-10-06 that the user
merges. A merge into MsdialWorkbench master still takes the user's explicit
OK, as #825 and #826 each had, and this amendment merges only once the user
has approved its wording. A merge here changes the gate pin and pauses a
running campaign (Confirmation boundaries). Gate #37 merges before, or with,
Interactive #69, and no campaign pins Interactive 0.5.36 before it is in: the
next plan pins what Interactive main carries, and the gate of #34 FAILs ACQ-1, a
`blocks_run` check, for a multi-energy AIF unit whose inputs record different
sets and runs under 0.5.36 (Supported production scope, AIF). The two go in
together, at heads that read each other's records, as Interactive #69 at
`35d75ad` and gate #37 at `e7b2901` do: gate #37 at that head reads the record
#69 keeps of a readable twin (`replaces_undecodable`,
`unattributed_members.replaced_undecodable`), and FAILs INP-1 for an input that
runs for an undecodable mzML without it.
Both key each input's energy set by its relative path, as the user's
second-round answer 2 of 2026-10-08 asked before #69 merges, and #69 runs a
readable twin as that sample's own input, as the user's answer to the extra
question of the same day has it (Evidence and decisions). Two things still
hold #69 at `35d75ad`, each a change from the behaviour before it that no
answer of the user's settles (Open for the user, in Evidence and decisions).
It runs the readable twin of an undecodable mzML that is a member of a shared
archive for its sample, which second-round answer 1 leaves out of every unit
and which no version before it ran (question 1). And outside a campaign it
leaves out the readable twin of an admitted mzXML that lies in another folder,
which Interactive 0.5.35 ran as an unattributed input in a unit-scoped archive
(question 4). It merges once the user has answered both questions, or once
#69 keeps the behaviour before it in those two cases, on record, until the
user does; the agent does not choose between the answers. A shared archive's
readable twin of an admitted mzXML, which the convert stage analyses for its
sample in a campaign, ran before #69 and does not hold it. Nor do the other
two questions: the twin of an undecodable mzML a Catalog declaration names,
one of the two #69 names as its own, which #69 keeps out as before; and two
readable twins the order ranks equal, of which #69 runs neither, where
0.5.35 ran both across folders, which both answers rule out. All four stay the
user's to answer; approving the amendment's wording does not answer them.

## Feedback to Codex

When asked to audit, do not edit source code and do not download raw data. Return
the structure in `feedback/claude-audit-template.md`. Include exact MCP tool
names, arguments with secrets removed, returned error/status, expected behavior,
severity, and the smallest reproducible sequence. Distinguish missing feature,
contract mismatch, scientific ambiguity, and implementation defect.
