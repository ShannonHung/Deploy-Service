# Ansible runner image.
#
# Mimics the company's pre-baked image: ansible-core + collections from
# requirements.yml are installed at build time, and the playbooks + ansible.cfg
# are baked in. At runtime the inventory is bind-mounted in and the log dir is
# bind-mounted out (see run-ansible.sh).
FROM python:3.12-slim

# openssh-client: needed for ansible's ssh connection plugin.
RUN apt-get update && \
    apt-get install -y --no-install-recommends openssh-client && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir ansible-core

WORKDIR /ansible

# Install collections first so this layer is cached independently of playbooks.
COPY requirements.yml ./requirements.yml
RUN ansible-galaxy collection install -r requirements.yml

COPY ansible.cfg ./ansible.cfg
COPY playbooks ./playbooks

# ansible.cfg points ANSIBLE_LOG_PATH here; the dir is bind-mounted out at run time.
RUN mkdir -p /var/log/ansible

# Default command is overridden by run-ansible.sh, but a bare `docker run` shows help.
ENTRYPOINT ["ansible-playbook"]
CMD ["--help"]
