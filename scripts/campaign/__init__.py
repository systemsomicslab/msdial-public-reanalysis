"""The unattended full-repository campaign runner: ledger, state machine, ports, policy and plan.

scripts/campaign-runner.py is the entry point. Nothing here decides whether a unit is eligible to run:
Interactive's classify_preflight writes that decision into the unit manifest as campaign_disposition,
and this package only reads it (policy.read_disposition).
"""
