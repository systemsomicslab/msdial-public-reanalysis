# Laboratory lipidomics workflow — independent audit

Give this to a fresh session that has not seen the development conversation. It says
what to do and what to report. It deliberately does not say what you should find: an
audit told the expected answers proves only that the auditor can read.

## Your position

You are a Claude Code session acting as an independent reviewer of a laboratory
lipidomics workflow. You are not its author. Treat every claim the tooling makes as
something to check, including its successes — a step that reports success without
having done the work is the failure this audit exists to catch.

## Scope of action

- Read anything. Run the workflow.
- Do **not** edit source code, and do not fix what you find. Report it.
- Do not delete raw data.
- Analysis outputs go under a directory you create for this audit, not into anyone
  else's results.
- Every confirmation the tooling asks for, answer as a careful analyst would, and
  record what you were asked and what you answered.

## What to run

Run the workflow the way a user would, using the `msdial-lipidomics-workflow` skill,
on the lipidomics dataset you are given. Take it from raw data through to a merged,
concentration-normalized lipidome.

Do not skip the confirmations, and do not repair a step that goes wrong by hand. If a
step needs manual intervention to proceed, that is a finding: record what you had to
do and why, then continue.

## What to check, at each stage

**File recognition.** Does every file get recognised, with the right vendor, format,
polarity and acquisition type? Is the blank identified as a blank?

**The proposed sample table.** Is the proposed grouping the comparison the experiment
is about, or something else the file names happen to contain? Were you told how the
proposal was reached and what the alternatives were? Check the analytical order and
the dilution factor: both are silently consequential and both should have been put to
you rather than assumed.

**The analysis settings.** Were you asked which annotation library to use, and whether
retention time should be used for annotation? Were the answers recorded somewhere that
a second dataset would inherit? Read the parameter file that was actually generated and
confirm it carries what you agreed — not what you were told it carries.

**The run.** Does the recorded command reproduce the run? Does the manifest name the
Console binary that actually executed, and is its provenance verified?

**The internal standards.** Were you shown which standards were found and which were
not? For any not found, is the reason given, and is it a reason that makes sense?

**Normalization.** Take one internal standard and check by hand that its normalized
value equals the concentration the standards table assigns it, in every sample. Take
one ordinary lipid and check its value against the height ratio to its class standard.
Confirm the unit is recorded in the output. Confirm the raw matrix is available beside
the normalized one — a concentration you cannot trace back to a measurement is a
number to be taken on trust.

**The merge.** Are both polarities represented? Is the count of kept lipids accompanied
by an account of what was dropped and why? Try to establish whether the sample pairing
was checked rather than assumed — the merged table pairs the polarities by position.

## Cross-checks worth making

A fact recorded in several places by different code paths cannot be satisfied by one
component being consistently wrong about itself. Where you can, compare:

- the file count in the sample table, the parameter file, the run log and the exported
  matrix header;
- the class labels in the sample table and in the exported matrix;
- the sample order in the positive and negative matrices;
- the internal standard concentrations in the standards table and in the normalized
  output.

## What to report

Write a readable report, then the YAML block from `feedback/claude-audit-template.md`,
into `feedback/claude-lipidomics-<date>.md`.

For each finding give: what you ran, with which arguments (secrets removed), what came
back, what you expected instead, how severe it is, and the smallest sequence that
reproduces it. Separate these four, because they call for different fixes:

- a missing feature,
- a contract mismatch between two parts that disagree,
- a scientific ambiguity that no code change settles on its own,
- an implementation defect.

State your overall status as PASS, CONDITIONAL or FAIL, and say plainly what would have
to change for it to become PASS. If you could not check something, record it as not
checked rather than as passing; absence of evidence is its own result, and reporting it
as success is the one outcome that makes the audit worthless.
