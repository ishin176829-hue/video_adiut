import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "smart_api_test.py"
MANIFEST_PATH = ROOT / "tests" / "api_manifest.toml"


def _load_smart_api_test():
    assert SCRIPT_PATH.exists(), "scripts/smart_api_test.py must exist"
    spec = importlib.util.spec_from_file_location("smart_api_test", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_changed_files_select_relevant_api_tests():
    smart_api_test = _load_smart_api_test()

    manifest = smart_api_test.load_manifest(MANIFEST_PATH)
    plan = smart_api_test.build_plan(
        manifest,
        changed_files=["src/video_review/oss.py", "src/video_review/db.py"],
        mode="safe",
    )

    selected_ids = {item.case.id for item in plan.selected}
    assert "oss_init_mov_blackbox" in selected_ids
    assert "platform_admin_database_local" in selected_ids
    assert "real_review_e2e" not in selected_ids
    assert {"oss", "database"}.issubset(plan.matched_tags)


def test_safe_mode_skips_costly_or_mutating_tests_until_allowed():
    smart_api_test = _load_smart_api_test()

    manifest = smart_api_test.load_manifest(MANIFEST_PATH)
    safe_plan = smart_api_test.build_plan(
        manifest,
        changed_files=["src/video_review/tasks.py"],
        mode="safe",
    )

    skipped = {item.case.id: item.reason for item in safe_plan.skipped}
    assert "real_review_e2e" in skipped
    assert "--allow-costly" in skipped["real_review_e2e"]
    assert "--allow-write" in skipped["real_review_e2e"]

    full_plan = smart_api_test.build_plan(
        manifest,
        changed_files=["src/video_review/tasks.py"],
        mode="safe",
        allow_costly=True,
        allow_write=True,
        allow_callback=True,
    )

    assert "real_review_e2e" in {item.case.id for item in full_plan.selected}


def test_markdown_report_explains_plan_commands_and_skips(tmp_path):
    smart_api_test = _load_smart_api_test()

    manifest = smart_api_test.load_manifest(MANIFEST_PATH)
    plan = smart_api_test.build_plan(
        manifest,
        changed_files=["src/video_review/tasks.py"],
        mode="safe",
    )
    results = [
        smart_api_test.ExecutionResult(
            name="pytest",
            command=["uv", "run", "pytest", "-q", "tests/test_platform_api.py"],
            exit_code=0,
            output="2 passed",
        )
    ]

    markdown = smart_api_test.render_markdown_report(
        plan=plan,
        results=results,
        target="remote",
        base_url="https://video-audit.duanju.com",
        dry_run=False,
    )

    assert "real_review_e2e" in markdown
    assert "--allow-costly" in markdown
    assert "uv run pytest -q tests/test_platform_api.py" in markdown
    assert "https://video-audit.duanju.com" in markdown
    assert "2 passed" in markdown


def test_openapi_schema_generates_safe_blackbox_cases():
    smart_api_test = _load_smart_api_test()

    cases = smart_api_test.generate_safe_blackbox_cases(
        {
            "paths": {
                "/api/v1/health": {"get": {"summary": "Health"}},
                "/api/v1/reviews/history": {"get": {"summary": "History"}},
                "/api/v1/reviews/{review_id}": {"get": {"summary": "Status"}},
                "/api/v1/reviews": {"post": {"summary": "Create review"}},
                "/video/admin/stats": {"get": {"summary": "Internal stats"}},
            }
        }
    )

    generated = {case.id: case for case in cases}
    assert "generated_get_api_v1_health" in generated
    assert "generated_get_api_v1_reviews_history" in generated
    assert "generated_get_api_v1_reviews_review_id" not in generated
    assert "generated_post_api_v1_reviews" not in generated
    assert "generated_get_video_admin_stats" not in generated
    assert generated["generated_get_api_v1_health"].blackbox[0].method == "GET"


def test_blackbox_output_redacts_entire_sensitive_values():
    smart_api_test = _load_smart_api_test()

    output = smart_api_test._redact_body(
        '{"credentials":{"access_key_id":"STS.visible","access_key_secret":"secret-value","security_token":"token-value"}}'
    )

    assert "STS.visible" not in output
    assert "secret-value" not in output
    assert "token-value" not in output
    assert "***REDACTED***" in output


def test_generated_cases_do_not_duplicate_manifest_endpoints():
    smart_api_test = _load_smart_api_test()

    manifest = smart_api_test.load_manifest(MANIFEST_PATH)
    generated_cases = smart_api_test.generate_safe_blackbox_cases(
        {
            "paths": {
                "/api/v1/health": {"get": {"summary": "Health"}},
                "/api/v1/new-safe-endpoint": {"get": {"summary": "New endpoint"}},
            }
        }
    )
    plan = smart_api_test.build_plan(
        manifest,
        changed_files=[],
        mode="smoke",
        generated_cases=generated_cases,
    )

    selected_ids = {item.case.id for item in plan.selected}
    assert "health_blackbox" in selected_ids
    assert "generated_get_api_v1_health" not in selected_ids
    assert "generated_get_api_v1_new_safe_endpoint" in selected_ids
