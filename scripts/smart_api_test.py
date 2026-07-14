#!/usr/bin/env python3

import argparse
import json
import os
import re
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import mkdtemp
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "tests" / "api_manifest.toml"
DEFAULT_REPORT_DIR = ROOT / "docs" / "api-test-runs"
DEFAULT_BASE_URL = "https://video-audit.duanju.com"


@dataclass(frozen=True)
class BlackboxProbe:
    method: str
    path: str
    expect_status: int | None = None
    expect_statuses: tuple[int, ...] = ()
    headers: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] | None = None


@dataclass(frozen=True)
class TestCase:
    id: str
    name: str
    description: str
    endpoints: tuple[str, ...]
    tags: tuple[str, ...]
    pytest: tuple[str, ...] = ()
    blackbox: tuple[BlackboxProbe, ...] = ()
    writes: bool = False
    costly: bool = False
    callback: bool = False
    generated: bool = False


@dataclass(frozen=True)
class ChangeRule:
    patterns: tuple[str, ...]
    tags: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class Manifest:
    default_tags: tuple[str, ...]
    change_rules: tuple[ChangeRule, ...]
    test_cases: tuple[TestCase, ...]


@dataclass(frozen=True)
class SelectedItem:
    case: TestCase
    reason: str


@dataclass(frozen=True)
class SkippedItem:
    case: TestCase
    reason: str


@dataclass(frozen=True)
class TestPlan:
    changed_files: tuple[str, ...]
    matched_tags: set[str]
    matched_reasons: tuple[str, ...]
    selected: tuple[SelectedItem, ...]
    skipped: tuple[SkippedItem, ...]


@dataclass(frozen=True)
class ExecutionResult:
    name: str
    command: list[str]
    exit_code: int
    output: str


def load_manifest(path: Path = DEFAULT_MANIFEST) -> Manifest:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    rules = tuple(
        ChangeRule(
            patterns=tuple(rule.get("patterns", ())),
            tags=tuple(rule.get("tags", ())),
            reason=str(rule.get("reason", "")),
        )
        for rule in data.get("change_rules", ())
    )
    cases = tuple(_load_case(raw) for raw in data.get("test_cases", ()))
    return Manifest(default_tags=tuple(data.get("default_tags", ("smoke",))), change_rules=rules, test_cases=cases)


def _load_case(raw: dict[str, Any]) -> TestCase:
    return TestCase(
        id=str(raw["id"]),
        name=str(raw.get("name", raw["id"])),
        description=str(raw.get("description", "")),
        endpoints=tuple(raw.get("endpoints", ())),
        tags=tuple(raw.get("tags", ())),
        pytest=tuple(raw.get("pytest", ())),
        blackbox=tuple(_load_probe(item) for item in raw.get("blackbox", ())),
        writes=bool(raw.get("writes", False)),
        costly=bool(raw.get("costly", False)),
        callback=bool(raw.get("callback", False)),
        generated=bool(raw.get("generated", False)),
    )


def _load_probe(raw: dict[str, Any]) -> BlackboxProbe:
    statuses = raw.get("expect_statuses", ())
    return BlackboxProbe(
        method=str(raw["method"]).upper(),
        path=str(raw["path"]),
        expect_status=raw.get("expect_status"),
        expect_statuses=tuple(int(status) for status in statuses),
        headers={str(k): str(v) for k, v in raw.get("headers", {}).items()},
        body=raw.get("body"),
    )


def build_plan(
    manifest: Manifest,
    *,
    changed_files: list[str] | tuple[str, ...],
    mode: str,
    allow_write: bool = False,
    allow_costly: bool = False,
    allow_callback: bool = False,
    generated_cases: list[TestCase] | tuple[TestCase, ...] = (),
) -> TestPlan:
    matched_tags: set[str] = set()
    matched_reasons: list[str] = []

    if not changed_files:
        matched_tags.update(manifest.default_tags)
        matched_reasons.append("No changed files detected; using default smoke coverage.")

    for changed in changed_files:
        for rule in manifest.change_rules:
            if any(_matches(changed, pattern) for pattern in rule.patterns):
                matched_tags.update(rule.tags)
                if rule.reason and rule.reason not in matched_reasons:
                    matched_reasons.append(rule.reason)

    if mode == "smoke":
        matched_tags.update(manifest.default_tags)
    elif mode == "full":
        matched_tags.update(tag for case in manifest.test_cases for tag in case.tags)

    cases = tuple(manifest.test_cases) + tuple(generated_cases)
    selected: list[SelectedItem] = []
    skipped: list[SkippedItem] = []
    seen: set[str] = set()
    covered_endpoints: set[str] = set()

    for case in cases:
        if case.id in seen or not matched_tags.intersection(case.tags):
            continue
        if case.generated and covered_endpoints.intersection(case.endpoints):
            continue
        seen.add(case.id)
        block_reasons = _guard_reasons(case, allow_write=allow_write, allow_costly=allow_costly, allow_callback=allow_callback)
        if block_reasons:
            skipped.append(SkippedItem(case=case, reason=", ".join(block_reasons)))
            continue
        selected.append(SelectedItem(case=case, reason=f"matched tags: {', '.join(sorted(matched_tags.intersection(case.tags)))}"))
        covered_endpoints.update(case.endpoints)

    return TestPlan(
        changed_files=tuple(changed_files),
        matched_tags=matched_tags,
        matched_reasons=tuple(matched_reasons),
        selected=tuple(selected),
        skipped=tuple(skipped),
    )


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if "*" in pattern:
        from fnmatch import fnmatch

        return fnmatch(path, pattern)
    return path == pattern or path.startswith(pattern.rstrip("/") + "/")


def _guard_reasons(case: TestCase, *, allow_write: bool, allow_costly: bool, allow_callback: bool) -> list[str]:
    reasons: list[str] = []
    if case.writes and not allow_write:
        reasons.append("requires --allow-write")
    if case.costly and not allow_costly:
        reasons.append("requires --allow-costly")
    if case.callback and not allow_callback:
        reasons.append("requires --allow-callback")
    return reasons


def generate_safe_blackbox_cases(openapi: dict[str, Any]) -> list[TestCase]:
    cases: list[TestCase] = []
    for path, methods in sorted(openapi.get("paths", {}).items()):
        if not path.startswith("/api/v1/"):
            continue
        if "{" in path or "}" in path:
            continue
        get_spec = methods.get("get") if isinstance(methods, dict) else None
        if get_spec is None:
            continue
        case_id = "generated_get_" + path.strip("/").replace("/", "_").replace("-", "_")
        cases.append(
            TestCase(
                id=case_id,
                name=str(get_spec.get("summary") or f"GET {path}"),
                description="Generated safe blackbox GET probe from OpenAPI.",
                endpoints=(path,),
                tags=("smoke", "api", "generated"),
                blackbox=(BlackboxProbe(method="GET", path=path, expect_statuses=(200, 401, 403, 404, 409)),),
                generated=True,
            )
        )
    return cases


def load_local_openapi() -> dict[str, Any]:
    os.environ.setdefault("VIDEO_REVIEW_DATA_DIR", mkdtemp(prefix="smart-api-openapi-"))
    os.environ.setdefault("VIDEO_REVIEW_USE_REDIS_QUEUE", "0")
    sys.path.insert(0, str(ROOT / "src"))
    from video_review.main import app

    return app.openapi()


def discover_changed_files(explicit: list[str] | None = None) -> list[str]:
    if explicit:
        return explicit
    try:
        completed = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return []
    if completed.returncode != 0:
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def execute_plan(plan: TestPlan, *, base_url: str, dry_run: bool) -> list[ExecutionResult]:
    results: list[ExecutionResult] = []
    pytest_nodes = sorted({node for item in plan.selected for node in item.case.pytest})
    if pytest_nodes:
        command = ["uv", "run", "pytest", "-q", *pytest_nodes]
        results.append(_run_command("pytest", command, dry_run=dry_run))

    for item in plan.selected:
        for probe in item.case.blackbox:
            results.append(_run_blackbox_probe(item.case.id, probe, base_url=base_url, dry_run=dry_run))
    return results


def _run_command(name: str, command: list[str], *, dry_run: bool) -> ExecutionResult:
    if dry_run:
        return ExecutionResult(name=name, command=command, exit_code=0, output="dry-run")
    completed = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    return ExecutionResult(name=name, command=command, exit_code=completed.returncode, output=completed.stdout.strip())


def _run_blackbox_probe(case_id: str, probe: BlackboxProbe, *, base_url: str, dry_run: bool) -> ExecutionResult:
    url = base_url.rstrip("/") + probe.path
    command = ["HTTP", probe.method, url]
    if dry_run:
        return ExecutionResult(name=f"blackbox:{case_id}", command=command, exit_code=0, output="dry-run")

    body_bytes = None
    headers = dict(probe.headers)
    if probe.body is not None:
        body_bytes = json.dumps(probe.body, ensure_ascii=False).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")

    request = urllib.request.Request(url, data=body_bytes, headers=headers, method=probe.method)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = response.status
            text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        text = exc.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return ExecutionResult(name=f"blackbox:{case_id}", command=command, exit_code=1, output=f"request failed: {exc}")

    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    expected = _expected_statuses(probe)
    ok = not expected or status in expected
    output = f"status={status} elapsed_ms={elapsed_ms} body={_redact_body(text)}"
    return ExecutionResult(name=f"blackbox:{case_id}", command=command, exit_code=0 if ok else 1, output=output)


def _expected_statuses(probe: BlackboxProbe) -> set[int]:
    statuses = set(probe.expect_statuses)
    if probe.expect_status is not None:
        statuses.add(int(probe.expect_status))
    return statuses


def _redact_body(text: str) -> str:
    sensitive = {"access_key_id", "access_key_secret", "security_token", "callback_secret"}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        for key in sensitive:
            text = re.sub(rf'("{re.escape(key)}"\s*:\s*")[^"]*(")', rf'\1***REDACTED***\2', text)
        return text[:1000]
    return json.dumps(_redact_json(value, sensitive), ensure_ascii=False)[:1000]


def _redact_json(value: Any, sensitive: set[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: "***REDACTED***" if str(key).lower() in sensitive else _redact_json(item, sensitive)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json(item, sensitive) for item in value]
    return value


def render_markdown_report(
    *,
    plan: TestPlan,
    results: list[ExecutionResult],
    target: str,
    base_url: str,
    dry_run: bool,
) -> str:
    failed = [result for result in results if result.exit_code != 0]
    lines = [
        "# Smart API Test Report",
        "",
        f"Generated at: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"Target: `{target}`",
        f"Base URL: `{base_url}`",
        f"Dry run: `{dry_run}`",
        f"Result: `{'FAILED' if failed else 'PASSED'}`",
        "",
        "## Discovery",
        "",
        f"Changed files: {', '.join(plan.changed_files) if plan.changed_files else 'none detected'}",
        f"Matched tags: {', '.join(sorted(plan.matched_tags)) if plan.matched_tags else 'none'}",
        "",
    ]
    if plan.matched_reasons:
        lines.extend(["Matched reasons:", ""])
        lines.extend(f"- {reason}" for reason in plan.matched_reasons)
        lines.append("")

    lines.extend(["## Selected Tests", ""])
    if plan.selected:
        for item in plan.selected:
            generated = " generated" if item.case.generated else ""
            endpoints = ", ".join(item.case.endpoints)
            lines.append(f"- `{item.case.id}`{generated}: {item.case.name} ({endpoints})")
    else:
        lines.append("- none")
    lines.append("")

    lines.extend(["## Skipped Tests", ""])
    if plan.skipped:
        lines.extend(f"- `{item.case.id}`: {item.reason}" for item in plan.skipped)
    else:
        lines.append("- none")
    lines.append("")

    lines.extend(["## Execution", ""])
    if results:
        for result in results:
            lines.extend(
                [
                    f"### {result.name}",
                    "",
                    "```bash",
                    " ".join(result.command),
                    "```",
                    "",
                    f"Exit code: `{result.exit_code}`",
                    "",
                    "```text",
                    result.output or "",
                    "```",
                    "",
                ]
            )
    else:
        lines.append("- no commands were executed")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_reports(markdown: str, *, plan: TestPlan, results: list[ExecutionResult], report_dir: Path) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    md_path = report_dir / f"smart-api-test-{stamp}.md"
    json_path = report_dir / f"smart-api-test-{stamp}.json"
    md_path.write_text(markdown, encoding="utf-8")
    payload = {
        "changed_files": list(plan.changed_files),
        "matched_tags": sorted(plan.matched_tags),
        "selected": [item.case.id for item in plan.selected],
        "skipped": [{"id": item.case.id, "reason": item.reason} for item in plan.skipped],
        "results": [
            {"name": result.name, "command": result.command, "exit_code": result.exit_code, "output": result.output}
            for result in results
        ],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return md_path, json_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discover, generate, execute, and report SN2S API tests.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--target", choices=("local", "remote"), default="remote")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--mode", choices=("safe", "smoke", "full"), default="safe")
    parser.add_argument("--changed", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-generate", action="store_true")
    parser.add_argument("--allow-write", action="store_true")
    parser.add_argument("--allow-costly", action="store_true")
    parser.add_argument("--allow-callback", action="store_true")
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    args = parser.parse_args(argv)

    manifest = load_manifest(args.manifest)
    generated_cases: list[TestCase] = []
    if not args.no_generate:
        generated_cases = generate_safe_blackbox_cases(load_local_openapi())
    changed_files = discover_changed_files(args.changed)
    plan = build_plan(
        manifest,
        changed_files=changed_files,
        mode=args.mode,
        allow_write=args.allow_write,
        allow_costly=args.allow_costly,
        allow_callback=args.allow_callback,
        generated_cases=generated_cases,
    )
    results = execute_plan(plan, base_url=args.base_url, dry_run=args.dry_run)
    markdown = render_markdown_report(
        plan=plan,
        results=results,
        target=args.target,
        base_url=args.base_url,
        dry_run=args.dry_run,
    )
    md_path, json_path = write_reports(markdown, plan=plan, results=results, report_dir=args.report_dir)
    print(f"Markdown report: {md_path}")
    print(f"JSON report: {json_path}")
    failed = [result for result in results if result.exit_code != 0]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
