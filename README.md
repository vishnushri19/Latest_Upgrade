![CI](https://github.com/vishnushri19/f5-17-to-21-upgrade-architecture/actions/workflows/ci.yml/badge.svg)
![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)
![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)

# Enterprise Upgrade Architecture: F5 BIG-IP 17.x → 21.x (Active-Standby)

## Overview

This repository implements an enterprise-grade upgrade architecture for migrating F5 BIG-IP systems from version 17.x to 21.x in active-standby high availability environments.

The focus of this project is not procedural upgrade steps, but architectural risk reduction. It is designed to address real-world failure modes observed during production upgrades, including state ambiguity, traffic disruption during planned transitions, and rollback complexity across major BIG-IP versions.

This repository accompanies a technical article published on F5 DevCentral describing the underlying upgrade architecture.

---

## Authorship and Responsibility

I designed this upgrade architecture based on hands-on experience supporting enterprise production environments undergoing major BIG-IP version transitions.

This repository reflects:
- The architectural decisions I implemented
- Validation-driven upgrade sequencing
- Rollback-first design principles
- Lessons learned from real upgrade failures

While this project may evolve through community feedback, the core architecture and framework originate from my design and implementation.

---

## Design Philosophy

- **Architecture over procedure**: Commands do not prevent outages—design does.
- **Validation before progression**: Each phase must be behaviorally verified.
- **Reversibility**: Every phase assumes rollback may be required.
- **Production realism**: The model reflects live traffic behavior, not lab-only assumptions.

---

## Supported Scenario (Current Scope)

Current focus:
- F5 BIG-IP (TMOS)
- Upgrade path: 17.x → 21.x
- High Availability: Active / Standby
- Enterprise environments targeting zero-downtime or near-zero-downtime
- Primary focus: LTM and SSL/TLS behavior

Additional scenarios (ASM/AWAF considerations, extended validations, implementation enhancements) can be added incrementally.

---

## Repository Structure

architecture/ # Architectural design and decision rationale
prechecks/ # Pre-upgrade validation model and logic
upgrade-flow/ # Upgrade sequencing and decision gates
validation/ # Post-upgrade behavioral verification model
rollback/ # Rollback strategy and execution principles
docs/ # Project summaries and versioning notes
src/ # Python implementation (lab-first, safe-by-default)
scripts/ # CLI entry points (prechecks + flow)
outputs/ # Generated reports (JSON + Markdown)


---

## Safety Notes

- This project is intended to be validated in a lab environment first.
- Early versions intentionally avoid executing destructive actions (install/reboot/failover).
- Do not publish customer identifiers, production IPs/hostnames, or sensitive config artifacts.

---

## Quick Start (Lab)

### Requirements
- Python 3.9+
- Network access to a BIG-IP device (VE or hardware)
- Lab credentials (do not use production credentials)
- BIG-IP 21.0.0.1 ISO available on your workstation, for example BIGIP-21.0.0.1-0.0.13.iso

### Setup
```bash
make venv
make install
```
### Environment Variables
- export BIGIP_HOST="10.0.0.10"
- export BIGIP_USER="admin"
- export BIGIP_PASS="password"
- export CRQ_NUMBER="CRQ123456"
- export BIGIP_VERIFY_TLS="false"
- export TARGET_IMAGE_CONTAINS="21.0.0.1"
- export TARGET_VOLUME="HD1.2"
- export AUTO_UPLOAD_ISO="true"
- export ISO_LOCAL_PATH="/path/to/BIGIP-21.0.0.1-0.0.13.iso"
- # For a combined base image + engineering hotfix install, use both instead:
- export BASE_ISO_LOCAL_PATH="/path/to/BIGIP-16.1.4.1.iso"
- export HOTFIX_ISO_LOCAL_PATH="/path/to/Hotfix-BIGIP-16.1.4.1.0.50.5-ENG.iso"
- export SCP_USER="admin"  

### Run Prechecks
Writes reports to outputs/<CRQ_NUMBER>/ in both JSON and Markdown.
make prechecks

### Outputs:
outputs/<CRQ_NUMBER>/precheck_report_<host>.json
outputs/<CRQ_NUMBER>/precheck_report_<host>.md

### Run Upgrade Flow (Skeleton)
This runs the controlled upgrade flow on the standby node:
- Prechecks and HA/discovery
- Target image presence check (REST plus filesystem)
- Optional ISO upload from your workstation to /shared/images
- Install to TARGET_VOLUME on the standby
- Wait for volume install to complete
- Reboot standby into the new volume
- Post-boot validation (device reachable, version contains TARGET_IMAGE_CONTAINS, still standby)

### Output Reports
Reports are generated in:
outputs/<CRQ_NUMBER>/precheck_report_<host>.json
outputs/<CRQ_NUMBER>/precheck_report_<host>.md
outputs/<CRQ_NUMBER>/upgrade_flow_report_<host>.json
outputs/<CRQ_NUMBER>/upgrade_flow_report_<host>.md

- If `EXEC-IMG-001` fails, see `docs/prereq-image.md` (includes `scripts/upload_iso.sh`).

### Full HA Pair (Lab) – Optional Orchestration
For a 17.x active/standby pair, you can orchestrate both nodes plus HA validation.
export BIGIP_HOST="10.0.0.10"        # first node
export PEER_BIGIP_HOST="10.0.0.11"   # second node

export BIGIP_USER="admin"
export BIGIP_PASS="password"
export VERIFY_TLS="false"

export TARGET_IMAGE_CONTAINS="21.0.0.1"
export TARGET_VOLUME="HD1.2"

export AUTO_UPLOAD_ISO="true"
export ISO_LOCAL_PATH="/path/to/BIGIP-21.0.0.1-0.0.13.iso"
export SCP_USER="admin"

### Run the orchestrated upgrade:
```bash
make upgrade-ha-pair
```

This will:
1. Run the upgrade flow on BIGIP_HOST (standby-first pattern).
2. Switch to PEER_BIGIP_HOST and run the upgrade flow again.
3. Switch back to the original BIGIP_HOST and run HA recovery validations (scripts/run_ha_recovery.py), which include trust-domain, sync-status, and traffic-group checks.

You can also call the orchestration script directly:
```bash
PYTHONPATH=src python scripts/run_all.py
```
### Intended Audience

This project is intended for:
Network and security architects
Senior F5 engineers managing production environments
Organizations planning major BIG-IP version upgrades

It is not intended as a beginner tutorial or quick-start upgrade guide.

