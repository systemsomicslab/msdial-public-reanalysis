# Claude compatibility feedback template

Return this YAML block after the readable audit report.

```yaml
audit_schema: msdial-claude-compatibility-audit.v1
auditor: Claude Code
audited_at: "ISO-8601 timestamp"
overall_status: PASS | CONDITIONAL | FAIL
environment:
  catalog_database: "absolute path"
  catalog_status: {}
  interactive_status: {}
  console_status: {}
scope:
  repository: mb_post
  requested_accessions:
    first: MPST000001
    last: MPST000010
  accessions_found: []
  accessions_missing: []
  analysis_units_total: 0
  units_supported: []
  units_excluded: []
  units_requiring_review: []
checks:
  - id: MCP-CATALOG-STATUS
    status: PASS | WARN | FAIL
    evidence: "tool result summary"
  - id: MCP-ANALYSIS-UNIT-HANDOFF
    status: PASS | WARN | FAIL
    evidence: "whether the exact unit and files survive into Interactive"
  - id: MCP-CONFIRMATION-BOUNDARIES
    status: PASS | WARN | FAIL
    evidence: "confirmed=false behavior"
  - id: OUTPUT-JOB-SCOPING
    status: PASS | WARN | FAIL
    evidence: "job_id and artifact ownership behavior"
findings:
  - finding_id: CLAUDE-001
    severity: blocker | high | medium | low
    category: contract-mismatch | implementation-defect | missing-feature | scientific-ambiguity | documentation
    title: "short title"
    observed: "what actually happened"
    expected: "what should happen"
    reproduction:
      - "MCP tool call 1 with non-secret arguments"
      - "MCP tool call 2 with non-secret arguments"
    affected_components: []
    proposed_change: "smallest testable correction"
    acceptance_test: "observable condition that closes the finding"
execution_authorized: false
notes_for_codex: []
```

Do not include API keys, private MSP contents, personal tokens, or raw spectra.
File paths and non-secret accession/unit identifiers are acceptable.

