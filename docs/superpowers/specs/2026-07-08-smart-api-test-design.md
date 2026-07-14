# One-Stop Smart API Test Design

## Goal

Provide one command that can discover likely affected API areas, apply safety gates, execute the relevant tests, and write a traceable report for the SN2S video review `/api/v1` surface.

## Scope

The first version is intentionally small:

- A manifest maps API capabilities to endpoints, tags, local pytest nodes, and safety flags.
- A script reads changed files or explicit `--changed` inputs and turns them into a test plan.
- Safe mode excludes tests that write real data, call costly model chains, or send real callbacks unless explicitly allowed.
- The runner can execute local pytest nodes and remote blackbox HTTP probes.
- Reports are written as Markdown plus JSON for later CI use.

## Architecture

`tests/api_manifest.toml` is the source of truth. Each test case declares:

- `id`, `name`, `description`
- `endpoints`
- `tags`
- optional `pytest` node ids
- optional `blackbox` probe definitions
- safety flags: `writes`, `costly`, `callback`

`scripts/smart_api_test.py` contains four separable parts:

1. Discovery: `git diff` or explicit `--changed` paths.
2. Planning: match changed paths to manifest tags and select tests.
3. Guarding: skip unsafe tests unless `--allow-write`, `--allow-costly`, or `--allow-callback` is present.
4. Execution and reporting: run selected pytest nodes and blackbox probes, then write Markdown and JSON reports.

## Safety Rules

Default mode is safe. It must not submit real review jobs, invoke Gemini review, delete data, or call real callbacks. Expensive or mutating flows require explicit flags.

If no changed files are available, the script falls back to smoke tests instead of pretending it knows the affected scope.

## Acceptance Criteria

- A unit test proves changed files select the expected API tests.
- A unit test proves safe mode skips risky tests with clear reasons.
- A unit test proves reports include selected tests, skipped tests, commands, and failure status.
- `python scripts/smart_api_test.py --target remote --mode safe --dry-run` produces a plan and report without mutating data.
- Existing project tests still pass.
