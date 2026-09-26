# Upgrade Chamber — Mission

Status: proposed implementation specification, not a record of completed work.
Companion specifications: [technical design](tech-stack.md) and [24-hour delivery plan](roadmap.md).
Time budget: 24 elapsed hours. Plan assumes one experienced full-stack builder; additional teammates can divide work without expanding scope.

## Mission

Help Python maintainers evaluate one dependency upgrade by executing the repository's tests in disposable Vultr containers, attempting a bounded compatibility repair, and returning a downloadable patch with inspectable evidence.

Product promise: **A dependency upgrade, tested against your repository, with the patch and evidence to review.**

Passing tests establishes compatibility with the executed suite under the recorded environment. It does not prove complete application correctness, exploitability, or absence of vulnerabilities.

## Problem and user

The primary user is a maintainer of a small Python project who sees a dependency advisory but cannot quickly tell whether the upgrade will break their code. A version recommendation alone leaves the installation, test execution, failure diagnosis, and patch verification to the maintainer.

Upgrade Chamber performs that bounded workflow. Its distinguishing result is a compatibility patch backed by an independently scheduled rerun, not merely a vulnerability list or an edited requirement.

The hackathon entry targets Pattern A: sandboxed code execution. Browser automation and vision are unnecessary for this product.

## Product scope

### Required for the submission

- A public HTTPS web application hosted on Vultr.
- A Vultr VM hosting the controller, persistent run records, and orchestration.
- All agent reasoning through Vultr Serverless Inference.
- Real package installation and test execution in disposable containers outside the application process.
- One working repository profile, one selected direct dependency, and a reproducible baseline.
- A visible baseline, upgrade attempt, optional repair, verification, and cleanup sequence.
- A patch download and an evidence bundle containing real execution results.
- A recorded containment probe showing a timeout, container removal, and a successful subsequent job.
- Public GitHub source, setup instructions, architecture explanation, public demo URL, and recorded video.

### Supported input contract

The interface accepts a public GitHub repository URL, an optional commit/ref, and a dependency name. The server resolves the ref to an immutable commit. During the hackathon, execution is enabled only for published, validated repository profiles. Unsupported repositories receive a clear explanation before execution.

Each profile specifies one Python version, dependency files, the fixed test command, required fixture packages, and expected test scope. The default runtime is Python 3.11 on Linux. Requirements files are the first supported editable dependency format. Profiles may supply a clearly disclosed requirements snapshot for historical reproductions.

No claim of arbitrary-repository support is permitted. A reproduction fork must retain attribution and explicitly describe any dependency pins or setup adjustments added for the benchmark.

### Excluded from the 24-hour build

Private repositories, GitHub App installation, automatic pull requests or merges, arbitrary test commands, arbitrary Dockerfiles, multi-language support, external databases, browser testing, unbounded dependency resolution, lockfile ecosystem coverage, and multi-user billing.

The agent cannot edit tests, test configuration, CI configuration, the sandbox policy, or the runner. Package upgrades and bounded application-code repairs are the only accepted mutations.

## User journey

1. Choose a validated example or paste its public repository URL.
2. Select a dependency and inspect the resolved commit and supported test scope.
3. Start the run. The interface explains that uploaded source and logs may be sent to Vultr inference for repair.
4. Watch installation, baseline tests, advisory lookup, upgrade, and repair events.
5. Inspect the final result: original and target versions, collected tests, passes/failures/skips, advisory changes, and remaining limitations.
6. Download the patch and evidence bundle. Apply the patch manually after review.

The UI has three primary surfaces: start page, live run page, and result page. The run page contains a timeline, test comparison, patch viewer, and a compact containment panel. Raw logs are expandable. Do not make users decode infrastructure details to understand the outcome.

## Results and honest language

| Outcome | Meaning |
| --- | --- |
| Verified upgrade | Fresh rerun passes the required checks and advisory lookup completed. |
| Tests passed; advisory check unavailable | Execution succeeded, but security data could not be retrieved. |
| Upgrade failed | Candidate or repaired code did not satisfy the checks within the budget. |
| Baseline failed | The original repository cannot establish a passing comparison. No compatibility repair is attempted. |
| Unsupported setup | The repository does not match an enabled execution profile. |
| Timed out / cancelled | The execution was interrupted; cleanup status is reported separately. |
| Infrastructure failure | The runner, container runtime, or provider failed independently of a test assertion. |

Show “target advisory no longer reported for this installed version,” not “secure.” Show the advisory query timestamp and remaining advisories. A package advisory does not establish that the application reaches the vulnerable behavior.

## Acceptance requirements

| ID | Requirement | Observable acceptance evidence |
| --- | --- | --- |
| M01 | Immutable input | Result includes repository URL, commit SHA, profile ID, and source digest. |
| M02 | Real baseline | Installation record, process exit status, collected test IDs, and test report exist. Zero collected tests cannot pass. |
| M03 | Agent execution | Vultr inference selects an eligible upgrade and, when needed, proposes a bounded source repair. Model identity and call outcome are recorded without credentials. |
| M04 | Fresh attempts | Baseline, candidate, each repair, and verification use distinct disposable containers. |
| M05 | Protected tests | Changes to tests/configuration are rejected. Final collected test IDs match baseline; no new skips or xfails are accepted. |
| M06 | Rechecked result | A fresh verifier reruns the accepted patch with the controller-owned command and scans actual installed versions. |
| M07 | Inspectable output | Download includes patch, source/environment identity, reports, logs, advisory snapshots, and attempt history. |
| M08 | Bounded execution | CPU, memory, process, output, storage, per-step, and job limits are enforced outside generated code. |
| M09 | Secret hygiene | Execution containers have no inference key, provider credentials, Docker socket, or application filesystem mount. |
| M10 | Cleanup | Container removal is observed after normal completion, error, timeout, and cancellation; orphan cleanup exists. |
| M11 | Public product | A browser can start an allowed demo run and inspect its real results on Vultr. |
| M12 | Honest failure | Baseline errors, timeouts, and unavailable advisory checks remain visible; no fabricated successful counts. |

## Demo candidate and evidence status

The primary research candidate is `msabramo/requests-unixsocket` at historical tag `v0.3.0`, commit `8449bc0f76a2ce410644b5e8aab45829ddca54f7`. Requests 2.31.0 is affected by CVE-2024-35195. Later Requests changed the adapter API; current upstream UnixAdapter contains a compact compatibility implementation. The repository has pytest tests using a local Unix socket fixture.

This is a research candidate, not an executed benchmark. The historical repository's exact dependency pins, compatible fixture versions, passing baseline, failing upgrade, and passing repaired suite still require verification. Introducing a baseline Requests pin requires a labeled reproduction profile or fork. Current tests must not be substituted silently for historical tests.

Mozilla PollBot's historical Requests and Jinja2 upgrade changes provide backup research leads, but its offline test suitability is also unverified. Do not spend the entire hackathon supporting either repository if setup remains unstable.

The fallback is a small public reference application with a real vulnerable dependency and meaningful tests, labeled “maintained demo fixture.” Its failure must arise from a real dependency compatibility change. Do not manufacture failing logs or weaken tests to create a repair story.

## Containment demonstration

Run a clearly labeled infinite-loop fixture using the same execution policy as ordinary tests. Record the external watchdog timeout, process termination, container removal, and a subsequent successful job. A shorter probe deadline is allowed if displayed explicitly. This demonstrates bounded execution and recovery, not proof against all container escapes.

## Success criteria

The minimum successful entry is one reproducible public repository workflow, a real generated patch, immutable evidence, and a successful containment/recovery demonstration. The preferred demo includes a real compatibility failure repaired by the agent. A straight-through upgrade remains a valid result if no repair is required.

Performance targets, to validate during the build: an ordinary prepared demo completes within five minutes, status events appear within three seconds, and no job exceeds fifteen minutes. These are targets rather than measured claims.

## References

- Challenge requirements supplied by the user: Blast Radius Zero — Safe Agent Execution on Vultr.
- [Requests advisory in OSV](https://osv.dev/vulnerability/GHSA-9wx4-h78v-vm56).
- [Requests API migration history](https://github.com/psf/requests/blob/main/HISTORY.md).
- [requests-unixsocket tags](https://github.com/msabramo/requests-unixsocket/tags).
- [Current adapter implementation](https://github.com/msabramo/requests-unixsocket/blob/master/requests_unixsocket/adapters.py).
- [PollBot release history](https://github.com/mozilla/PollBot/releases).
