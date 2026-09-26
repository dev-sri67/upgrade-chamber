# Implementation checklist

Status reflects recorded evidence, not intended behavior. A Vultr VM and Serverless Inference subscription are provisioned. The private API responds on the VM's loopback interface. A live `glm-5.3-flash` structured-response smoke call succeeded at `2026-09-26T19:08:28Z` (operator record excluded from source control). The first live probe failed during Docker input staging because its root filesystem was read-only; the container was removed. A subsequent disposable smoke proved that fixed, nonprivileged exec-stdin can write to `/work` under the unchanged isolation policy, while Docker `get_archive` returns 404 for files in that tmpfs. The local baseline framework and synthetic tests do not complete any submission requirement.

| ID | Required evidence | Current status |
| --- | --- | --- |
| M01 | Resolved commit, profile ID, source digest | Open; no run records |
| M02 | Passing baseline with install, test IDs, report, exit status | Open; fixed offline attempt code and synthetic tests only |
| M03 | Actual Vultr inference selection and bounded repair | Open; live smoke call passed, selection and repair not recorded |
| M04 | Distinct disposable containers for each attempt | Open; runner path implemented, live attempts pending |
| M05 | Protected test/config changes rejected; stable collection | Open |
| M06 | Independent verifier with installed-version scan | Open |
| M07 | Downloadable patch and complete evidence bundle | Open |
| M08 | External resource and job limits | Open; a disposable smoke observed the configured CPU, memory, PID, and tmpfs limits, but no complete job ran |
| M09 | No keys, Docker socket, or app mounts in containers | Open; disposable container inspection found no mounts and confirmed network isolation and nonprivileged settings, but no complete job ran |
| M10 | Observed cleanup and orphan recovery | Open; one failed-staging container removal observed, orphan recovery and normal-path cleanup pending |
| M11 | Public Vultr-hosted browser workflow | Open |
| M12 | Honest failure and unavailable-check reporting | Open |

Next gate: preserve the read-only root policy while replacing input staging and tmpfs evidence export, then rerun timeout and recovery probes before a pinned baseline and upgrade attempt in fresh Vultr containers. Record real reports and cleanup evidence. The user requested a safe stop before further deployment or experiment work. Do not enable the research candidate or claim a successful upgrade before those records exist.
