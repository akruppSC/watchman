## Categories, severity, confidence

Categories: hallucinated_dependency, weak_test, swallowed_error, duplication,
over_abstraction, scope_creep, silent_behavior_change, security, convention_violation,
comment_noise, correctness, other.

Severity:
- blocker: will break production, leak secrets, or corrupt data; must not merge.
- major: a real defect or risk a senior reviewer would request changes for.
- minor: should fix, but not merge-blocking.
- nit: style or polish.

Confidence is your calibrated probability that a senior reviewer would agree this is a
real problem. Line numbers refer to the NEW version of the file (the `+` side of the diff).
