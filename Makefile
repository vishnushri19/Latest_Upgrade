.PHONY: help venv install prechecks flow clean ha upgrade-ha-pair state-pre state-post state-diff

help:
	@echo "Targets:"
	@echo "  venv             - Create python venv (.venv)"
	@echo "  install          - Install dependencies + editable package"
	@echo "  prechecks        - Run prechecks (writes outputs/*.json and outputs/*.md)"
	@echo "  state-pre        - Capture pre-upgrade REST state tables and take UCS/SCF backups"
	@echo "  flow             - Run upgrade flow (writes outputs/*.json and outputs/*.md)"
	@echo "  state-post       - Capture post-upgrade REST state tables"
	@echo "  state-diff       - Compare pre vs post state snapshots and generate diff report"
	@echo "  ha               - Run HA recovery validation"
	@echo "  upgrade-ha-pair  - Upgrade both HA nodes + final HA checks"
	@echo "  clean            - Remove venv + __pycache__"

PYTHON ?= $(shell which python3 2>/dev/null || which python 2>/dev/null || echo python3)

venv:
	$(PYTHON) -m venv .venv

install:
	. .venv/bin/activate && pip install -U pip && pip install -r requirements.txt && pip install -e .

prechecks:
	. .venv/bin/activate && python scripts/run_prechecks.py

state-pre:
	. .venv/bin/activate && python scripts/run_state_diff.py --phase pre --backup

state-post:
	. .venv/bin/activate && python scripts/run_state_diff.py --phase post

state-diff:
	. .venv/bin/activate && python scripts/run_state_diff.py --compare

flow:
	. .venv/bin/activate && python scripts/run_upgrade_flow.py

ha:
	. .venv/bin/activate && python scripts/run_ha_recovery.py

# Orchestrate both nodes using BIGIP_HOST and PEER_BIGIP_HOST
upgrade-ha-pair:
	. .venv/bin/activate && \
	python scripts/run_upgrade_flow.py && \
	BIGIP_HOST="$$PEER_BIGIP_HOST" python scripts/run_upgrade_flow.py && \
	BIGIP_HOST="$$BIGIP_HOST" python scripts/run_ha_recovery.py

clean:
	rm -rf .venv
	find . -type d -name "__pycache__" -prune -exec rm -rf {} \;

# ---------------------------------------------------------------------------
# Customer handover workflow targets
# ---------------------------------------------------------------------------

.PHONY: setup check-env backup snapshot-pre snapshot-post upgrade-node diff clean-outputs

setup:
	python3 -m venv .venv
	. .venv/bin/activate && python -m pip install --upgrade pip
	. .venv/bin/activate && pip install -r requirements.txt
	. .venv/bin/activate && pip install -e .

check-env:
	@test -n "$$BIGIP_HOST" || (echo "ERROR: BIGIP_HOST is not set"; exit 2)
	@test -n "$$BIGIP_USER" || (echo "ERROR: BIGIP_USER is not set"; exit 2)
	@test -n "$$CRQ_NUMBER" || (echo "ERROR: CRQ_NUMBER is not set"; exit 2)
	@test -n "$$TARGET_IMAGE_CONTAINS" || (echo "ERROR: TARGET_IMAGE_CONTAINS is not set"; exit 2)
	@test -n "$$TARGET_VOLUME" || (echo "ERROR: TARGET_VOLUME is not set"; exit 2)

backup: check-env
	. .venv/bin/activate && python scripts/run_backup_artifacts.py

snapshot-pre: check-env
	. .venv/bin/activate && SNAPSHOT_PHASE=pre python scripts/run_state_snapshot.py

snapshot-post: check-env
	. .venv/bin/activate && SNAPSHOT_PHASE=post python scripts/run_state_snapshot.py

upgrade-node: check-env
	. .venv/bin/activate && python scripts/run_upgrade_node.py

diff:
	@test -n "$$PRE_FILE" || (echo "ERROR: PRE_FILE is not set"; exit 2)
	@test -n "$$POST_FILE" || (echo "ERROR: POST_FILE is not set"; exit 2)
	. .venv/bin/activate && python scripts/run_state_diff.py --compare --pre-file "$$PRE_FILE" --post-file "$$POST_FILE"

clean-outputs:
	rm -rf outputs
	mkdir -p outputs
	find . -type d -name "__pycache__" -prune -exec rm -rf {} +
