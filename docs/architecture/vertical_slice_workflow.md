# SDLC LangGraph Workflow Orchestration

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
      -> approved: coding (PatchService validation + atomic apply + cumulative diff/patch_hash)
 -> sandbox_test (two-phase install + offline pytest via sandbox-runner)
      -> failed: test_failure_analysis (TestingAgent) -> bounded correction -> coding
      -> passed: code_review (CodeReviewAgent + deterministic quality/integrity gates)
           -> failed: bounded correction -> coding
           -> passed: human_commit_approval (interrupt showing raw diff, flags, tests, review, cost; bound to patch_hash and base_commit_sha)
                -> rejected / hash mismatch: END
                -> approved: release_preparation (ReleaseAgent drafts PR metadata & final report) -> END
```

## State Ownership and Persistence
- Authoritative execution state: Owned exclusively by LangGraph checkpoints (`PostgresSaver` in runtime/Celery workers, `MemorySaver` in isolated unit tests).

- Application and evidence projections: `workflow_runs`, `agent_executions`, `tasks`, `task_dependencies`, `approvals`, `llm_usage_records`, `code_changes`, `test_executions`, `static_analysis_findings`, `code_reviews`, `pull_requests`, and `workflow_artifacts` are synced idempotently after each transition. `WorkflowOrchestrator.reconcile_projection(...)` repairs projection drift from the checkpoint

- PostgreSQL checkpointer bootstrap: LangGraph manages its own checkpoint tables outside Alembic via `bootstrap_postgres_checkpointer(settings.DATABASE_URL)`.

- Schema compatibility: Every checkpoint carries `state_schema_version`. `resume_workflow(...)` refuses to resume any checkpoint with an incompatible schema version.

## Safety Controls and Bounded Execution
1. Mandatory cost limit: Workflows refuse to start unless a positive `cost_limit_usd` is explicitly set.

2. Idempotent start and delivery: Re-submitting the same `idempotency_key` returns the existing run snapshot; branch push and Pull Request creation use a deterministic `operation_id` bound to (`workflow_id`, `base_commit_sha`, `patch_hash`) and fail safely if the remote base branch advanced.

3. Dual human approvals with hash binding:
     - `human_plan_approval` validates `(approved_plan_hash, approved_base_commit_sha)`.

     - `human_commit_approval` presents the raw cumulative diff, flags, test results, static-analysis report, review findings, and cost, validating `(approved_patch_hash, approved_base_commit_sha)`.

4. Non-overridable quality gates: Blocking deterministic findings from Ruff, Mypy, Bandit, pip-audit, or test-integrity checks force `CodeReviewReport.passed = False` regardless of LLM output and route back into bounded correction.

5. No merge or deployment paths: Release delivery prepares checksummed artifacts and opens a Pull Request for human review; merge and production deployment occur outside the platform.

6. Impossible infinite loops: Separate per-failure (`MAX_CORRECTION_LOOPS_PER_FAILURE`) and total workflow (`MAX_CORRECTION_LOOPS_PER_WORKFLOW`) correction limits enforce clean termination with `CORRECTION_LIMIT_EXCEEDED`.

7. Safe cancellation: `cancel_workflow(...)` transitions active or paused workflows to `cancelled` in both checkpoint and projection, preventing accidental resume.

8. Privacy-aware tracing: LangSmith tracing is opt-in, disabled by default for private repositories without `llm_data_sharing=True`, and scrubs secrets from all trace metadata.