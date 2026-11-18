<!--
unit-triage-task.v2: the per-accession task prompt scripts/run-triage-agent.py renders for one triage session under
skill 1.2.0 (docs/llm-assisted-curation.md sections 2.2, 2.3 and 4.1). The agent writes one spec; code expands it into
the payloads. The skill states the procedure; this prompt states only the accession, the units, the directories, the
file to write and the boundaries.

Placeholders, each written as its name in double braces below this comment and replaced as plain text:
  accession         the repository accession, e.g. ST000001
  unit_ids          the Catalog analysis unit ids to triage, comma separated, e.g. AN000001, AN000002
  input_dir         the directory holding what the agent may read (digest, handoffs, mwTab texts, archive listings)
  output_dir        the directory the agent writes its spec into
  spec_name         the spec file name, <accession>.spec.json
  analysis_purpose  the person's stated analysis_purpose, verbatim, or the word null

Render both directories relative to the session's working directory. The rendered prompt is stored as
inputs\<unit_id>\prompt.md and released byte for byte, so it must hold no absolute path, user name or e-mail address
(section 5.3). The record binds this template by its sha256 at the repository commit and the rendered prompt by its
own sha256. This comment block is part of the template's bytes; the wrapper renders the whole file.
-->
# Unit triage: {{accession}}

Triage accession **{{accession}}** before any download, following the `msdial-unit-triage` skill
(`.claude/skills/msdial-unit-triage/SKILL.md`; the spec schema it names is `schemas/msdial-unit-triage-spec.v1.json`,
beside it in the working directory). Read the skill first and follow it exactly.

## Units

Triage these Catalog analysis units, and only these: {{unit_ids}}.

## Inputs

Everything you may read is in `{{input_dir}}`. Start with `{{input_dir}}/digest.md`. The raw inputs beside it are
`<unit_id>/handoff.json` with `samples.json`, `<analysis id>.mwtab.txt` and `*.members.tsv`; read them where the
digest cuts something or is unclear, and never follow the paths a `handoff.json` names. Nothing else is evidence. A
read outside `{{input_dir}}`, `{{output_dir}}`, the skill and the schema refuses the whole session.

The person's stated analysis purpose (context only; `null` means none was stated):
{{analysis_purpose}}

## Output

Write one spec, `{{output_dir}}/{{spec_name}}`, and nothing else: no payload, no decision record, no review log.
Check it with `triage_spec_check` and rewrite it whole with `Write` (there is no `Edit`) until the check reports no
error. Then reply with the single word
`done`.

## Boundaries

- Read only: no download, no MS-DIAL run, no Catalog write, no tool call with `confirmed=true`, and no tool outside
  the skill's `allowed-tools`.
- Every decision is a model inference: give each group, exception and unit a `confidence` and a `rationale`, and ask
  rather than guess.
- No absolute path, user name or e-mail address in the spec.
