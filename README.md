# Upgrade Chamber

Upgrade Chamber is an in-progress demonstration of one Python dependency upgrade tested in disposable Vultr containers. The product specification is in [mission.md](mission.md), with [technical design](tech-stack.md) and [delivery gates](roadmap.md).

This repository currently contains the controller scaffold, a fixed Docker runner for probes and profile attempts, offline baseline preparation code, and research candidate metadata. No repository is enabled for execution yet. The historical `requests-unixsocket` case remains unvalidated; the API lists it as a research candidate, not a supported run. A Vultr VM and Serverless Inference subscription have been provisioned. A live structured-response smoke call using `glm-5.3-flash` succeeded at `2026-09-26T19:08:28Z`; the local operator record is excluded from source control. No historical test result is recorded yet. See the [runner notes](docs/runner.md), [baseline execution contract](docs/baseline-execution.md), and [baseline feasibility record](docs/baseline-feasibility.md) for current evidence and limits.

## Local setup

Use Python 3.11. With `uv`, install locked dependencies, run tests, and start the API:

```sh
uv sync --frozen --extra test
uv run --frozen --extra test python -m pytest
uv run --frozen uvicorn upgrade_chamber.api:app --host 127.0.0.1 --port 8000
```

Alternatively, with an existing Python 3.11 interpreter:

```sh
python -m pip install -e '.[test]'
python -m pytest
uvicorn upgrade_chamber.api:app --host 127.0.0.1 --port 8000
```

`GET /healthz` checks that the API is serving. `GET /api/profiles` shows enabled profiles and separately labels research candidates. There is no run submission endpoint until the execution path and a profile pass the acceptance gates.

Copy `.env.example` to a local `.env` and supply a **Serverless Inference** key and chosen model ID when available. Do not commit `.env`. Load the variables into the process environment or use a local environment-file tool; the application does not read `.env` automatically. To inspect available models and make a real structured-response smoke call, run:

```sh
uv run --frozen --env-file .env upgrade-chamber-inference models
uv run --frozen --env-file .env upgrade-chamber-inference smoke
```

The `models` command requires `VULTR_INFERENCE_API_KEY`. The `smoke` command also requires `VULTR_MODEL_ID`. Both call only Vultr's inference API. Smoke output records the configured model and call outcome; it never prints the key or response body. A real smoke result requires a live Vultr subscription and network access. Mocked unit tests do not establish provider availability.

## Current gates

The [implementation checklist](docs/implementation-checklist.md) tracks acceptance requirements. Vultr inference connectivity is verified, but G1 remains open until a Vultr-hosted container records a passing baseline and real upgrade result. Local tests cover the preparation and attempt contract with synthetic inputs; they are not evidence of a real repository run. Containment and cleanup remain unverified on a real Docker host. Orchestration, patch evidence, web UI, public deployment, and video are later gates. The execution image must be pinned by digest before a public profile is enabled.
