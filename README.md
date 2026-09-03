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
- accepting a Class proposal;
- starting each production MS-DIAL run;
- deleting downloaded raw data.

The default retention policy is `keep`. Never combine different analysis units
in one MS-DIAL run. Never redistribute the laboratory's private MSP libraries.

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
