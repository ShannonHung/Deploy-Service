# Ansible runner image — mirrors the company's pre-baked image.
#
# Shape we're imitating:
#   * Plain Ubuntu with ansible installed, nothing fancy.
#   * Everything lives at the root: /ansible.cfg, /playbooks, /collections.
#     /inventory is supplied at run time (bind-mounted over this path).
#   * NO ENTRYPOINT — the container runs whatever command run-ansible.sh passes
#     (e.g. `ansible-playbook ...`) and exits when it finishes.
#   * Does NOT write an ansible log file; output goes to stdout and the runner
#     tees it to the host (see run-ansible.sh).
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# ansible + openssh-client (for the ssh connection plugin). pipx/pip via apt to
# keep it close to a "plain ubuntu with the tools installed" image.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ansible \
        openssh-client \
        ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /

# Collections at /collections (ANSIBLE_COLLECTIONS_PATH is set at run time so we
# don't have to touch /ansible.cfg).
COPY requirements.yml /requirements.yml
RUN ansible-galaxy collection install -r /requirements.yml -p /collections

# Config + playbooks baked at root, as in the company image.
COPY ansible.cfg /ansible.cfg
COPY playbooks /playbooks

# No ENTRYPOINT and no long-running CMD: the container does one job and exits.
