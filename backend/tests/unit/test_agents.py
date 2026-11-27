"""Unit and safety-gating tests for TestingAgent, CodeReviewAgent, and ReleaseAgent."""

import json

import httpx
from app.agents import (
    AcceptanceCriterion,
    CodeReviewAgent,
    CodeReviewFinding,
    CodeReviewReport,
    ImplementationPlan,
    PlanTask,
    ReleaseAgent,
    ReleasePreparationOutput,
    RequirementSpecification,
    TestFailureDiagnosis,
    TestingAgent,
    TestIntegrityAssessment,
)
from app.core.config import load_settings
from app.execution.sandbox_client import SandboxExecutionResult, SandboxRunReport
from app.execution.schemas import PatchValidationResult, TestIntegrityFlag
from app.integrations.llm.client import LLMClient
from app.prompts import load_prompt
from cryptography.fernet import Fernet


def _make_local_settings() -> object:
    fernet_key = Fernet.generate_key().decode("utf-8")
    return load_settings(
        CREDENTIAL_ENCRYPTION_KEYS=fernet_key,
        LLM_PROVIDER="local",
        LLM_MODEL_DEFAULT="local-coder",
        LLM_BASE_URL="http://localhost:11434/v1",
    )


def _sample_requirement_and_plan() -> tuple[RequirementSpecification, ImplementationPlan]:
    req = RequirementSpecification(
        summary="Add validated limit and offset pagination to GET /items.",
        functional_requirements=["Support limit (1..100) and offset (>=0) on GET /items."],
        acceptance_criteria=[
            AcceptanceCriterion(
                id="AC-1",
                description="GET /items?limit=5&offset=5 slices accurately.",
                verification_method="pytest tests/test_items.py",
            )
        ],
    )
    plan = ImplementationPlan(
        summary="Update GET /items and add pagination tests.",
        tasks=[
            PlanTask(
                task_id="T-1",
                title="Add pagination",
                description="Add Query params to GET /items and tests.",
                expected_files=["app/main.py", "tests/test_items.py"],
                acceptance_criteria=["Pagination works and tests pass."],
                verification_steps=["Run pytest in sandbox."],
            )
        ],
        expected_files=["app/main.py", "tests/test_items.py"],
    )
    return req, plan


def _sample_patch_validation(
    *,
    test_flags: list[TestIntegrityFlag] | None = None,
) -> PatchValidationResult:
    return PatchValidationResult(
        valid=not bool(test_flags),
        base_commit_sha="a" * 40,
        patch_hash="b" * 64,
        unified_diff="--- a/app/main.py\n+++ b/app/main.py\n@@ -1 +1 @@\n-old\n+new\n",
        files_changed=["app/main.py", "tests/test_items.py"],
        lines_added=10,
        lines_removed=2,
        total_changed_lines=12,
        test_integrity_flags=test_flags or [],
    )


def _sample_sandbox_report(*, passed: bool) -> SandboxRunReport:
    test_exec = SandboxExecutionResult(
        execution_id="exec-1",
        template="pytest",
        phase="isolated_execution",
        network_policy="none",
        command_argv=["/sandbox_venv/bin/python", "-m", "pytest", "-q", "tests"],
        exit_code=0 if passed else 1,
        stdout="4 passed in 0.2s" if passed else "FAILED tests/test_items.py::test_pagination",
        stderr="" if passed else "AssertionError: assert 20 == 25",
        duration_seconds=0.2,
        timed_out=False,
    )
    return SandboxRunReport(
        run_id="sbx-1",
        workflow_id="wf-1",
        repository_id="repo-1",
        base_commit_sha="a" * 40,
        passed=passed,
        install_result=None,
        test_result=test_exec,
        executions=[test_exec],
    )


def test_testing_agent_diagnoses_sandbox_failure_with_untrusted_framing() -> None:
    assert "UNTRUSTED_DATA" in load_prompt("testing", "v1").content
    settings = _make_local_settings()
    req, plan = _sample_requirement_and_plan()
    failed_report = _sample_sandbox_report(passed=False)

    diagnosis_json = TestFailureDiagnosis(
        summary="Default limit broke baseline test expecting 25 items.",
        failure_category="assertion_failure",
        root_cause="Default limit was set to 20 instead of 25.",
        failing_tests=["tests/test_items.py::test_list_items_returns_all_seeded_items"],
        affected_files=["app/main.py"],
        coverage_gaps=["Missing test for offset beyond total items."],
        recommended_fixes=["Set default limit to 25 in app/main.py."],
        correction_instructions="Change Query(default=20) to Query(default=25) in list_items.",
        is_fixable_by_coding_agent=True,
    ).model_dump_json()

    captured_user_prompt = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_user_prompt
        body = json.loads(request.content.decode("utf-8"))
        captured_user_prompt = body["messages"][-1]["content"]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": diagnosis_json}}]},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as mock_http:
        llm = LLMClient(settings=settings, http_client=mock_http)  # type: ignore[arg-type]
        result = TestingAgent(llm).analyze_failure(
            requirement=req,
            plan=plan,
            raw_diff="--- a/app/main.py\n+++ b/app/main.py\n",
            sandbox_report=failed_report,
            is_private_repo=True,
            llm_data_sharing=False,
            attempt_number=1,
        )

    assert result.diagnosis.failure_category == "assertion_failure"
    assert result.diagnosis.is_fixable_by_coding_agent is True
    assert "<UNTRUSTED_DATA label=\"sandbox_test_output\">" in captured_user_prompt
    assert "AssertionError: assert 20 == 25" in captured_user_prompt


def test_code_review_agent_deterministically_blocks_critical_defects_and_unaddressed_flags()->None:
    assert "UNTRUSTED_DATA" in load_prompt("code_review", "v1").content
    settings = _make_local_settings()
    req, plan = _sample_requirement_and_plan()
    passing_sandbox = _sample_sandbox_report(passed=True)

    # Simulate an overly permissive LLM setting passed=True despite a High confirmed defect
    # and an unaddressed deterministic test-integrity flag
    permissive_llm_report = CodeReviewReport(
        passed=True,
        summary="Looks good overall.",
        findings=[
            CodeReviewFinding(
                id="CR-1",
                severity="High",
                classification="confirmed_defect",
                category="logic",
                file_path="app/main.py",
                start_line=18,
                end_line=22,
                title="Negative offset accepted",
                description="Offset allows negative indexing.",
                recommendation="Add ge=0 validation to offset Query parameter.",
            )
        ],
        test_integrity_assessments=[],
    ).model_dump_json()

    validation_with_flag = _sample_patch_validation(
        test_flags=[
            TestIntegrityFlag(
                file_path="tests/test_items.py",
                rule_id="skip_marker_added",
                severity="high",
                message="Skip marker increased from 0 to 1.",
            )
        ]
    )

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(
                200,
                json={"choices": [{"message": {"content": permissive_llm_report}}]},
            )
        )
    ) as mock_http:
        llm = LLMClient(settings=settings, http_client=mock_http)  # type: ignore[arg-type]
        result = CodeReviewAgent(llm).run(
            requirement=req,
            plan=plan,
            patch_validation=validation_with_flag,
            sandbox_report=passing_sandbox,
            is_private_repo=True,
            llm_data_sharing=False,
        )

    assert result.report.passed is False
    assert any("Critical/High confirmed defect" in r for r in result.report.blocking_reasons)
    assert any("Unaddressed deterministic test-integrity" in r
                for r in result.report.blocking_reasons)


def test_code_review_passes_potential_risk_only_and_release_agent_prepares_pr() -> None:
    assert "UNTRUSTED_DATA" in load_prompt("release", "v1").content
    settings = _make_local_settings()
    req, plan = _sample_requirement_and_plan()
    passing_sandbox = _sample_sandbox_report(passed=True)
    clean_validation = _sample_patch_validation()

    clean_review_json = CodeReviewReport(
        passed=True,
        summary="Pagination implementation matches acceptance criteria with clean tests.",
        requirement_coverage_notes=["limit/offset validation and total count preserved."],
        test_quality_notes=["Baseline and boundary pagination tests pass."],
        findings=[
            CodeReviewFinding(
                id="CR-2",
                severity="Low",
                classification="potential_risk",
                category="performance",
                file_path="app/store.py",
                title="In-memory slicing scales linearly",
                description="Full list copy occurs before slicing.",
                recommendation="Move slicing to SQL query when DB backing is added.",
            )
        ],
        test_integrity_assessments=[
            TestIntegrityAssessment(
                file_path="tests/test_items.py",
                rule_id="none",
                addressed=True,
                justification="No tests were removed or weakened.",
            )
        ],
    ).model_dump_json()

    release_json = ReleasePreparationOutput(
        pr_title="feat(items): add validated limit/offset pagination to GET /items",
        pr_description="Adds validated `limit` and `offset` query parameters to `GET /items`.",
        release_notes="Added pagination support to catalog listing endpoint.",
        changed_files_summary=[
            "app/main.py: added Query-validated limit/offset slicing",
            "tests/test_items.py: added pagination and 422 validation tests",
        ],
        test_results_summary="Isolated sandbox pytest passed (4 passed in 0.2s).",
        review_summary="Passed with 0 blocking defects and 1 Low potential risk.",
        limitations=["In-memory store only; no database cursor pagination yet."],
        final_report_markdown="# Final Report\n\nAll tests and review gates passed.",
    ).model_dump_json()

    staged = iter([clean_review_json, release_json])

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(
                200,
                json={"choices": [{"message": {"content": next(staged)}}]},
            )
        )
    ) as mock_http:
        llm = LLMClient(settings=settings, http_client=mock_http)  # type: ignore[arg-type]

        review_res = CodeReviewAgent(llm).run(
            requirement=req,
            plan=plan,
            patch_validation=clean_validation,
            sandbox_report=passing_sandbox,
            is_private_repo=True,
            llm_data_sharing=False,
        )
        assert review_res.report.passed is True
        assert review_res.report.blocking_reasons == []

        release_agent = ReleaseAgent(llm)
        assert not hasattr(release_agent, "merge_pull_request")
        assert not hasattr(release_agent, "deploy")

        release_res = release_agent.run(
            repository_full_name="acme/demo-catalog-api",
            base_branch="main",
            requirement=req,
            plan=plan,
            patch_validation=clean_validation,
            sandbox_report=passing_sandbox,
            review_report=review_res.report,
            is_private_repo=True,
            llm_data_sharing=False,
        )

    assert release_res.output.pr_title.startswith("feat(items):")
    assert len(release_res.output.changed_files_summary) == 2
    assert "Final Report" in release_res.output.final_report_markdown