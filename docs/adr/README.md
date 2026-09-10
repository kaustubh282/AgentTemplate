# Architecture decision records

One record per significant decision, in the format required by master prompt §47:

```text
Status | Context | Decision | Alternatives | Consequences | Security impact | Compliance impact
```

An ADR is written when a decision is hard to reverse, constrains future work, or would
otherwise be re-litigated. Superseding an ADR means adding a new one, not editing the
old one.

| # | Decision | Status |
|---|---|---|
| [0001](0001-deterministic-workflow-vs-llm-owned-state.md) | Deterministic workflow, not LLM-owned state | Accepted |
| [0002](0002-ui-directive-contract.md) | UI directives as versioned server-owned contracts | Accepted |
| [0003](0003-strands-orchestration-pattern.md) | Strands orchestration: escalation, not default entry | Accepted |
| [0004](0004-model-provider-abstraction.md) | Model provider abstraction with a single invoker | Accepted |
| [0005](0005-rag-grounding-policy.md) | Grounding policy: absolute relevance gate and abstention | Accepted |
| [0006](0006-insuremo-adapter-boundary.md) | InsureMO adapter boundary with explicit unconfigured failure | Accepted |
| [0007](0007-session-and-state-persistence.md) | Session and state persistence behind Protocols | Accepted |
| [0008](0008-observability-approach.md) | Observability: three streams, redaction by construction | Accepted |
| [0009](0009-pii-and-redaction-approach.md) | PII: classification-driven, remove-not-mask for secrets | Accepted |
| [0010](0010-environment-strategy.md) | Environment strategy: one artifact, fail-fast configuration | Accepted |
| [0011](0011-centralized-harness-control-plane.md) | Centralized in-process Harness as the AI control plane | Accepted |
| [0012](0012-three-layer-evaluation-strategy.md) | Three-layer evaluation: native, Ragas, DeepEval | Accepted |
