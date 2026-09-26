# Disposable execution runner: first slice

The first runner slice executes only two controller-owned probes: `infinite_loop` and `success`. The former must hit the external deadline and be removed. A new success probe demonstrates recovery. These probes do not establish a passing repository baseline, dependency upgrade, or benchmark result.

Inspect the official Python 3.11 Linux base image, record its registry digest, and build with that verified digest. For the single-VM demonstration, inspect the resulting local image ID and configure `DockerRunner` with the exact `sha256:<local-image-id>` reference:

```sh
docker buildx imagetools inspect python:3.11-slim
docker build --build-arg PYTHON_IMAGE='python:3.11-slim@sha256:<verified-base-digest>' -f execution/Dockerfile -t upgrade-chamber-runner:local .
docker image inspect upgrade-chamber-runner:local --format '{{.Id}} {{.Os}}/{{.Architecture}}'
docker image inspect 'sha256:<local-image-id>' --format '{{.Id}} {{.Os}}/{{.Architecture}}'
```

The Dockerfile rejects a missing base digest or a base image that does not report Python 3.11. The local image ID is an immutable identity within this Docker daemon, but it is not a registry digest and cannot be used to pull the image on another host. Confirm that the final inspection returns the same full ID and `linux/<expected-architecture>`. Run the container with `python -c 'import sys; assert sys.version_info[:2] == (3, 11)'` to verify its runtime before probes. `DockerRunner` accepts a full local image ID or a registry digest reference, and rejects mutable tags and shortened IDs. Do not embed an unverified identity in this repository.

The worker passes a fixed command and stages a small nonce through `put_archive` after starting the container. No host path is mounted. The execution process receives no credentials, Docker socket, network, or user-selected shell command. Docker runs it as UID/GID 10001, drops capabilities, enables `no-new-privileges`, uses the default seccomp profile, and gives it a read-only root with 512 MiB work tmpfs and 128 MiB temporary tmpfs. CPU is capped at one core, memory and swap together at 1 GiB, and processes at 128. Docker logging is capped at one 2 MiB file; captured stdout and stderr each have a 2 MiB bound. Only the fixed JSON export path is read while the container is still running, with a 20 MiB maximum and strict tar entry validation. The worker then acknowledges the export so the process can exit before removal.

`run_probe` accepts a deadline above zero and at most 300 seconds. The timeout starts before container creation. On expiry the worker kills the container, and all paths remove it in `finally`. Removal is checked through Docker. Failure to observe removal raises `CleanupError` and blocks later runs on that runner instance. The runner labels containers with ownership and an expiry, and `remove_expired_containers` removes only expired, owned containers. Call it on worker startup and periodically.

The local unit suite uses a clearly fake Docker client to test policy arguments, deadline handling, follow-up execution, staging failures, bounded output, removal errors, and orphan cleanup. Real Docker validation must run on the target Linux host. Record the actual container ID, deadline, timeout, kill, removal inspection, and successful subsequent probe in deployment evidence. Docker containers share the host kernel; these controls do not establish hardened multi-tenant isolation.

## Historical profile attempts

`DockerRunner.run_profile_attempt(input_tar, phase="baseline" | "candidate")` accepts only a bounded tar with `source.zip`, `requirements.txt`, `manifest.json`, and wheel files under `wheels/`. It checks regular-file types, exact relative names, duplicate entries, a 10 MiB source archive limit, and a 128 MiB total input limit before creating a container. The source ZIP is extracted only by the fixed in-container script. The attempt starts in a fresh container with the same network-disabled execution policy as the probes. Its fixed command is `python -I /opt/upgrade_chamber/attempt.py <phase>`.

The runner reads the atomic `attempt.json` marker while the container remains alive, validates its schema and test count, and then reads fixed report files with a 20 MiB total cap. Missing report files after an installation failure remain missing in `AttemptResult`; they are not synthesized. A `passed` marker requires nonzero collected tests, successful fixed steps, and all five required reports. The runner acknowledges export completion, observes process exit, and removes the container in `finally`. The result carries the image identity, container ID, exit code, reports, missing file names, and observed cleanup separately from the test status. See [the input and marker contract](baseline-execution.md) for exact schema and evidence requirements.
