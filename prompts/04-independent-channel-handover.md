# Starting a new channel on this work

Paste this into a fresh Claude Code session opened in
`D:\13_MSDIAL_Public_Reanalysis\code`. It records where the work stands, what has
been settled, and what is still open, so a session with no history can continue
without re-deriving any of it.

## Read first

1. `CLAUDE.md` in this directory — the mission, the supported scope, the five
   confirmation boundaries, and the mandatory workspace root. The boundaries are
   not negotiable and no fix in this repository relaxes them.
2. `AGENTS.md` — points at the private cross-repository master prompt.
3. `.claude/skills/msdial-repository-batch/SKILL.md` — the batch procedure,
   including the three gate points.

## The repositories and who owns what

| Path | What it is |
| --- | --- |
| `D:\13_MSDIAL_Public_Reanalysis\code` | This workspace: instructions, skill, prompts, the invariant gate |
| `D:\13_MSDIAL_Public_Reanalysis\analysis` | Every download and MS-DIAL output. Never committed |
| `D:\0_SourceCode\msdial_interactive_app` | MS-DIAL Interactive MCP server |
| `D:\0_SourceCode\msdial_repository_catalog` | Repository Catalog MCP server |
| `D:\0_SourceCode\msdial_spectrum_catalog` | Spectrum catalog: Level-3 annotation records |
| `D:\0_SourceCode\MsdialWorkbench` | MS-DIAL Console. **The binary the pipeline runs comes from here** |

Two facts about that last one, both of which have already caused a wrong
conclusion:

- Interactive resolves the Console at
  `<base>/MsdialWorkbench/tests/MSDIAL5/MsdialCoreTestApp/bin/Release/net48/MSDIALCUI.exe`
  (`msdial_app/workflow.py`), which is the checkout on
  `feature/tiered-annotation-pipeline`, not `master`. A fix merged to `master`
  does not reach a reanalysis until that branch takes it.
- There are several MsdialWorkbench clones on this machine. Before claiming a
  Console-side defect is fixed, check the clone the pipeline resolves and the
  mtime of the binary, not whichever clone is convenient.

## Division of labour with Codex

Much of the Interactive app, the Repository Catalog and the reanalysis pipeline
were written by Codex. Where a design intent is not obvious from the code, ask
rather than infer: three such questions have been asked so far and all three
changed the implementation. In particular, ask not only "was this intended" but
"is this remedy sufficient" — one answer added a whole second layer of defence
that the original plan would have missed.

## What has been fixed, and the shape they share

Every defect found so far is the same shape: **something was approved, judged or
recorded, and never connected to what actually ran.** Each component was correct
in isolation and the whole was wrong.

| Where | What was wrong |
| --- | --- |
| Interactive | A retention policy chosen at download time silently pre-authorised deleting the raw data hours later |
| Interactive | The eligibility verdict was written three times and read for a decision nowhere |
| Interactive | The peak-count diagnostic rewrote the production analysis CSV, method file and run manifest |
| Interactive | The approved Class proposal was received and then ignored; grouping was re-derived from an argument |
| Interactive | A rejected call reached the client with its reason removed |
| Interactive | The list of exports a run planned was computed and never compared against what appeared |
| Console | Provenance columns asserted values the run never established: an inverted m/z, a gap-filled scan pointer, two similarity sentinels |
| Console | The mzTab-M evidence section was one column wider than its header on every row |
| Spectrum catalog | A reference-library ingest reported success on a file it could not read |
| Spectrum catalog | An ambiguity class identifier encoded a label rather than the thresholds it stood for |

When you find the next one, expect that shape. The productive question is not
"does this component work" but "does anything read what this component wrote".

## Working conventions that have held up

- **Cross-artifact checks over single-source checks.** Every fact worth trusting
  is recorded more than once by different code paths. An invariant that reads two
  of them at once cannot be satisfied by one component lying consistently to
  itself. `scripts/verify-run-invariants.py` is built entirely on this.
- **Read artifacts, not tool responses**, when the response is large enough to be
  truncated and its blockers field is serialised after its payload.
- **A check that cannot be evaluated reports `not_evaluable`, never `pass`.**
  Assert that property directly; writing that test found the checker breaking its
  own rule.
- **Measure before asserting.** Several confident claims in this project turned
  out to be wrong, including some of the assistant's own. Reproduce a defect on
  real artifacts before reporting it, and re-check a claim inherited from an
  earlier session.
- **In MsdialWorkbench, branch from `master` and target `master`**, whatever the
  purpose. A long-lived feature branch is how the mzTab fix missed the pipeline.

## Open items

Server side, in `msdial_interactive_app`, from `feedback/claude-autonomy-blockers-2026-09-05.md`:

- Bound the guided-plan response and put `blockers`, `validation` and
  `ready_to_prepare` before `workflow`. A truncated response makes "no blockers
  were reported" indistinguishable from "the blockers field did not arrive".
- Serialise Console execution, bound its runtime, and expose a cancel. Nothing
  today stops two Console processes running at once and nothing can stop a hung
  one.
- Record the Console binary's hash and source commit in the run manifest.
- Raw-header preflight cannot read Shimadzu `.lcd`, so the sanctioned route for
  resolving an unknown acquisition mode is inoperative for that vendor.
- Retention `keep` holds the archive and its extraction, and the download preview
  quotes the transfer figure rather than actual disk use.

Annotation side, in `msdial_spectrum_catalog`:

- Feeding the library consensus spectra into ambiguity classification. They are
  computed and now stored, but classification still reads the raw records. This
  changes the scientific result and is a decision to take deliberately.
- The Level-3 annotation body itself: spectrum to structure, using msemblator,
  DreaMS and FIORA. Nothing has been started here.

## A caution about this document

It was written at one point in time. Verify the current checkout, branch and
version state rather than trusting anything dated here, including the table
above.
