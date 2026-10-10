keep this file up to date

# Dell CPU-cap override

- This standalone public copy was authorized on 2026-10-09, including hostname-independent eligibility for the existing tested hardware configuration. This supersedes inherited hostname-pair restrictions for this checkout.
- Work locally using offline filesystem fixtures. Do not administer contributor machines or infer hardware validation from a passing fixture test.
- Keep CPU, BIOS, driver, CPU topology, policy, and register guards. Other models, CPUs, BIOS versions, and restriction states require separate evidence; do not add a force option or fallback.
- Use test-first changes through the actual CLI against complete temporary machine snapshots; do not touch live CPU registers in tests.
- Preserve first per-boot baselines, invocation-specific rollback, same-machine/same-boot restore, unrelated register bits, and independent CPU policy requests.
- Document permitted ceilings separately from measured clocks and short trials separately from long-term or electrical validation.
- Keep documentation self-contained and redact identifying machine details from shared diagnostic examples. Never commit live baseline files, credentials, firmware archives, or private investigation notes.
- Tests: run `python3 -m unittest discover -p 'test_*.py' -v` from the repository root as an unprivileged user. Real MSR writes, boot behavior, and loaded performance require separate operator-run validation on supported hardware.
- A checkout update does not update installed helper or service files. Keep installation and recovery instructions explicit.
