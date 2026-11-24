# Phase 4 Vertical-Slice LangGraph Orchestration

## Deterministic StateGraph Topology

```text
START
 -> requirement_analysis
      -> needs_clarification=true (<= 2 attempts): human_clarification (interrupt) -> requirement_analysis
      -> needs_clarification=false: repository_analysis
 -> repository_analysis (bounded hybrid/structural retrieval pinned to base_commit_sha)
 -> planning (consolidated TechnicalDesign + ImplementationPlan + deterministic plan_hash)
 -> human_plan_approval (interrupt bound to plan_hash and base_commit_sha)
      -> rejected / hash mismatch: END
      -> approved: coding (PatchService validation + atomic apply)
 -> sandbox_test (two-phase install + offline pytest via sandbox-runner)
 -> END
```

## State Ownership and Persistence
- Authoritative execution state: Owned exclusively by LangGraph checkpoints (PostgresSaver in runtime/Celery workers, MemorySaver in isolated unit tests).

- Application projection: workflow_runs stores user-facing metadata (status, current_node, idempotency_key, cost_limit_usd, base_commit_sha) synced idempotently after each invocation. VerticalSliceOrchestrator.reconcile_projection(...) repairs projection drift from the checkpoint.

- PostgreSQL checkpointer bootstrap: LangGraph manages its own checkpoint tables outside Alembic via bootstrap_postgres_checkpointer(settings.DATABASE_URL).

- Schema compatibility: Every checkpoint carries state_schema_version. resume_workflow(...) refuses to resume any checkpoint with an incompatible schema version.

## Safety Controls Enforced in Phase 4
1. Mandatory cost limit: Workflows refuse to start unless a positive cost_limit_usd is explicitly set.

2. Idempotent start: Re-submitting the same idempotency_key returns the existing run snapshot without duplicating LLM calls or workspace mutations.

3. Human approval binding: human_plan_approval validates both approved_plan_hash and approved_base_commit_sha; any drift or mismatch aborts before coding runs.

4. Isolated execution: Untrusted repository code and tests run only through SandboxClient (sandbox-runner) using the two-phase network policy (restricted_egress -> none).

## Known Limitations
- Testing failure-analysis correction loops, deterministic static-analysis quality gates, Code Review Agent, and Release/GitHub PR automation are introduced in Phases 5, 6, and 7.

- FastAPI workflow endpoints (POST /api/v1/workflows, /approve, /reject, /resume, /cancel) and Celery job dispatch wiring are delivered in Phases 5 and 8.