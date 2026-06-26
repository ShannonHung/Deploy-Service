# Makefile for the ansible runner (deploy-service/ansible/).
#
# Wraps docker build/push and run-ansible.sh so you don't have to remember the
# image name or the flag spelling. Everything below is override-able on the
# command line, e.g.:
#
#   make build
#   make push                 # needs `make login` first
#   make run INVENTORY=taipei/bm_multinode.ini LIMIT=node1
#   make run PLAYBOOK=ping.yml EXTRA_VARS="foo=bar" PULL=0
#
# Run `make help` for the full list.

# ─── Overridable config ──────────────────────────────────────────────────────
IMAGE        ?= shannonhung/ansible-runner
TAG          ?= latest
IMAGE_REF     := $(IMAGE):$(TAG)

# run-ansible.sh arguments (only non-empty ones are passed through).
PLAYBOOK     ?= ping.yml
INVENTORY    ?= taipei/multinode.ini
INVENTORY_REF ?=
LIMIT        ?=
TAGS         ?=
EXTRA_VARS   ?=
# PULL=0 adds --no-pull (use the locally-built image instead of pulling).
PULL         ?= 1

# ─── Assemble run-ansible.sh flags from the variables above ──────────────────
RUN_FLAGS := --playbook $(PLAYBOOK) --inventory $(INVENTORY) --image $(IMAGE_REF)
ifneq ($(strip $(INVENTORY_REF)),)
  RUN_FLAGS += --inventory-ref $(INVENTORY_REF)
endif
ifneq ($(strip $(LIMIT)),)
  RUN_FLAGS += --limit $(LIMIT)
endif
ifneq ($(strip $(TAGS)),)
  RUN_FLAGS += --tags $(TAGS)
endif
ifneq ($(strip $(EXTRA_VARS)),)
  RUN_FLAGS += --extra-vars "$(EXTRA_VARS)"
endif
ifeq ($(strip $(PULL)),0)
  RUN_FLAGS += --no-pull
endif

.DEFAULT_GOAL := help

# ─── Targets ─────────────────────────────────────────────────────────────────
.PHONY: help
help:
	@echo "Ansible runner — make targets"
	@echo ""
	@echo "  make build      Build the runner image ($(IMAGE_REF))"
	@echo "  make login      docker login (needed before push)"
	@echo "  make push       Push the image to the registry"
	@echo "  make release    build + push"
	@echo "  make run        Run a playbook (clones inventory, pulls image)"
	@echo "  make run-local  Run using the locally-built image (no pull)"
	@echo "  make shell      Open a shell in the runner image (debug)"
	@echo "  make clean      Remove ./logs"
	@echo ""
	@echo "Overridable variables (current defaults):"
	@echo "  IMAGE=$(IMAGE)  TAG=$(TAG)"
	@echo "  PLAYBOOK=$(PLAYBOOK)  INVENTORY=$(INVENTORY)"
	@echo "  INVENTORY_REF=$(INVENTORY_REF)  LIMIT=$(LIMIT)  TAGS=$(TAGS)"
	@echo "  EXTRA_VARS=$(EXTRA_VARS)  PULL=$(PULL)"
	@echo ""
	@echo "Examples:"
	@echo "  make run INVENTORY=taipei/bm_multinode.ini LIMIT=node1"
	@echo "  make run-local EXTRA_VARS=\"foo=bar\""

.PHONY: build
build:
	docker build -t $(IMAGE_REF) .

.PHONY: login
login:
	docker login

.PHONY: push
push:
	docker push $(IMAGE_REF)

.PHONY: release
release: build push

.PHONY: run
run:
	./run-ansible.sh $(RUN_FLAGS)

# Convenience: run against the image you just built, skipping the registry pull.
# Recurses into `run` with PULL=0 so RUN_FLAGS is re-evaluated with --no-pull.
# Pass-through overrides still work, e.g. `make run-local LIMIT=node1`.
.PHONY: run-local
run-local:
	$(MAKE) run PULL=0 \
	  PLAYBOOK='$(PLAYBOOK)' INVENTORY='$(INVENTORY)' INVENTORY_REF='$(INVENTORY_REF)' \
	  LIMIT='$(LIMIT)' TAGS='$(TAGS)' EXTRA_VARS='$(EXTRA_VARS)' \
	  IMAGE='$(IMAGE)' TAG='$(TAG)'

.PHONY: shell
shell:
	docker run --rm -it --entrypoint /bin/bash $(IMAGE_REF)

.PHONY: clean
clean:
	rm -rf logs
