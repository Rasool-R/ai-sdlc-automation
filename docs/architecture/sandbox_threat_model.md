# Sandbox Runner Threat Model and Trust Boundaries

## Architectural Isolation

- Untrusted repository code, generated patches, and dependency installation steps never execute on the host or inside the `api` or `worker` containers.
- Neither `api` nor `worker` mounts `/var/run/docker.sock`. Only the dedicated `sandbox-runner` service holds container-runtime access (via rootless Docker or a restricted Docker socket proxy).
- Communication uses a narrow internal HTTP contract (`POST /runs`, `POST /runs/{run_id}/workspace`, `POST /runs/{run_id}/execute`, `GET /runs/{run_id}/results`, `DELETE /runs/{run_id}`), optionally protected by `X-Sandbox-Token`.

## Hardening Controls

1. **Two-Phase Network Policy:**
   - Phase 1 (`install_deps`): Runs on a restricted egress network (`SANDBOX_INSTALL_NETWORK`) pointing at the configured `SANDBOX_PIP_INDEX_URL`.
   - Phase 2 (`pytest`, `ruff_check`, `mypy_check`, `bandit_check`): Enforces `--network none` unconditionally.
2. **Container Containment Flags:**
   - `--cap-drop=ALL`, `--security-opt=no-new-privileges:true`, `--read-only`, `--user 65534:65534`.
   - Ephemeral writable `/tmp` mounted with `noexec,nosuid,size=256m`; repository `/workspace` mounted read-only (`:ro`).
   - Resource bounds enforced per container: CPU (`--cpus`), RAM (`--memory` + equal `--memory-swap`), process limit (`--pids-limit`), and wall-clock timeout with forced `docker rm -f` cleanup.
3. **Command & Archive Safety:**
   - Only platform-controlled command templates are accepted; arbitrary shell strings are never executed.
   - Workspace archives are stripped of `.git`, symlinks, and `.env*`/private-key files during packaging and validated member-by-member during extraction against TarSlip and disk exhaustion.
   - No host credentials, database URLs, GitHub tokens, or LLM API keys are injected into sandbox containers.

## Residual Risks and Deployment Assumptions

- Standard Docker containers share the host Linux kernel; a kernel-level container escape vulnerability could still threaten the container runtime host.
- `sandbox-runner` itself is a privileged boundary because it can spawn containers; in production, it should run behind a restricted Docker socket proxy or rootless Docker daemon, with egress firewall rules limiting Phase-1 traffic to approved package mirrors only.
- Future hardening options include gVisor (`runsc`) or Firecracker microVMs without changing the internal `sandbox-runner` HTTP contract.