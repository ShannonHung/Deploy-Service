# Ansible runner (proof-of-path)

Standalone ansible image + launcher used to validate the eventual deploy-service
ansible feature. Nothing here is wired into the FastAPI app yet — it only proves
the chain:

```
run-ansible.sh -> clone inventory -> docker pull image -> ansible-playbook -> SSH -> node1/node2
```

## Layout

| File | Purpose |
|------|---------|
| `Dockerfile` | Builds the runner image: `ansible-core` + collections from `requirements.yml`, with `ansible.cfg` + `playbooks/` baked in. Mirrors the company's pre-baked image. |
| `ansible.cfg` | `host_key_checking=false`, logs to `/var/log/ansible/run.log`. |
| `requirements.yml` | Collections installed at build time (placeholder: `ansible.posix`). |
| `playbooks/ping.yml` | Runs `ansible.builtin.ping` against the `nodes` group. |
| `run-ansible.sh` | Clones inventory fresh, pulls the image, runs the playbook, cleans up. |

## "Always latest" behavior

Two things are refreshed on **every** run so stale artifacts can't leak in:

- **Inventory** — the inventory repo
  (<https://gitlab.com/ShannonHung/my-ansible-inventory.git>, URL fixed in the
  script) is `git clone --depth 1`-ed to a fresh temp dir, mounted to
  `/inventory`, and `rm -rf`-ed on exit via a `trap`. You never point at a
  local checkout; you select a file by its path **relative to the repo root**.
- **Image** — `docker pull shannonhung/ansible-runner:latest` runs before
  `docker run`, because the team re-tags `latest` weekly after code review
  (same name, new content). Use `--no-pull` to test a locally-built image.

## Prerequisites

The local SSH test nodes must be running (from `deploy-service/`):

```bash
make setup-ssh-nodes      # builds + starts ssh_node_1 (2222) and ssh_node_2 (2223)
```

## Build & publish the image

```bash
cd deploy-service/ansible
docker build -t shannonhung/ansible-runner:latest .
docker push shannonhung/ansible-runner:latest
```

For purely local testing without DockerHub, build the tag and pass `--no-pull`.

## Run

`--inventory` is a path **relative to the inventory repo root** (one repo can
hold many envs / files, e.g. `taipei/multinode.ini`, `taipei/bm_multinode.ini`):

```bash
# Ping both nodes (taipei standard inventory)
./run-ansible.sh --playbook ping.yml --inventory taipei/multinode.ini

# Bare-metal inventory, limited to one node
./run-ansible.sh --playbook ping.yml --inventory taipei/bm_multinode.ini --limit node1

# Tags / extra-vars / a non-default inventory branch
./run-ansible.sh --playbook ping.yml --inventory taipei/multinode.ini \
    --tags smoke --extra-vars "foo=bar" --inventory-ref main

# Use a locally-built image instead of pulling
./run-ansible.sh --playbook ping.yml --inventory taipei/multinode.ini --no-pull
```

Logs are written to `./logs/run.log` on the host (bind-mounted from
`/var/log/ansible/run.log` in the container) and stream to stdout as well.

## How it reaches the nodes

`node1`/`node2` publish their SSH ports to the host (`localhost:2222` / `2223`).
The container reaches them via `host.docker.internal` (added explicitly with
`--add-host host.docker.internal:host-gateway` so it also works on Linux). Auth
uses the plain private key `data/ssh_keys/client_key`, whose public half is the
nodes' `authorized_keys` — no CA cert involved.
