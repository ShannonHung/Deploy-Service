# Implementation Plan: SSH Command Execution API

This plan incorporates advanced security measures, process management, and connection handling based on user feedback.

## Proposed Changes

### Configurations & Credentials
#### [NEW] /data/SSH-{target}.json
- A standardized file storing SSH connection information for clusters. For example: `SSH-cluster1.json` containing the host, port, username, and authentication details.
- The API payload will specify `"ssh_config": "cluster1"`. If omitted, standardizes to `SSH-default.json`.

#### [NEW] app/core/auth/ssh.py
- Define a base `SSHAuthenticator` interface.
- Implement `SSHKeyAuthenticator` (for basic identity files) and `SSHCertificateAuthenticator` (for CA signed certificates).

#### [MODIFY] data/allow-commands-admin.json
- Refactored schema to define the command execution as `pipeline` arrays.
- Eliminates `command_pattern` string interpolation, favoring strict element-by-element parameterization.

---

### Command Execution Logic
#### [NEW] app/services/command_service.py

1. **Anti-Injection Filter**
   - Apply user regexes.
   - Run a strict blacklist across all user arguments to explicitly raise `Exception("Invalid characters")` if any `["`", ";", "&", "|", "$", "\\"]` are detected.

2. **Python-Native Pipeline Execution**
   - Instead of running a single string through bash (which is susceptible to injection), the system will use `asyncssh` to build chains.
   - We will use `bash -c` strictly as a wrapper to retrieve the PGID and isolate the process group, but the user commands will be passed as positional arguments (`$@`).
   - E.g.: `asyncssh.create_process("bash", "-c", 'echo $$; exec setsid "$@"', "_", *command_list)`
   - For pipelines, the output of the previous process is mapped to the input of the next (`stdin=prev.stdout`), fully replacing shell piping functionality. Python stitches the pipeline.

3. **Robust Kill Mechanisms**
   - Capturing the Process Group ID (PGID) enables precise process killing.
   - When a job exceeds `timeout_seconds`, the system soft-kills via `kill -TERM -<pgid>`, then falls back to `kill -KILL -<pgid>` if required.

4. **Fire-And-Forget (e.g. Reboot)**
   - When `disconnects_ssh` is true, the API immediately responds with `status: "disconnected_expected"`.
   - The command is executed and then the TCP exception or `ChannelClose` is ignored, effectively fire-and-forget. The task is skipped from entering `running_commands_pool`.

---

### API Schemas
#### [NEW] app/api/v1/schemas/command.py
- Structure the new `PipelineStep` (e.g. `{"command": ["ls", "-al"]}`).
- Ensure requests define `"ssh_config": str` (defaulting to `"default"`).

---

## Verification Plan

### Automated Tests
1. **Unit Tests**:
   - Verify injection blacklists correctly deny `$`, `;`, etc.
   - Mock `asyncssh.create_process` to prove pipelines inject `stdin=prev.stdout` properly.
2. **Integration Tests**:
   - Bring up tests using `make test-int`. Asserts that pipelines properly query docker nodes.

### Manual Verification
1. Spin up the nodes: `docker-compose -f docker-compose-nodes.yml up -d`.
2. Generate CA configs and standard key configs in `/data/` (or app data dir).
3. Throw dangerous inputs (e.g., `& rm -rf /`) into arguments to confirm they fail validation correctly.
4. Execute `sleep 30` with `timeout_seconds: 5`, then check target node `ps` output to confirm no orphaned sleep process remains (verifying PGID `-TERM` works).
5. Trigger `reboot` and observe immediate `disconnected_expected` API response without error stack traces tracking the task loop.
