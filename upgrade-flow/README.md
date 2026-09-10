# Controlled Upgrade Flow (BIG-IP 17.x → 21.x)

## Purpose

This document defines the controlled upgrade flow for transitioning F5 BIG-IP systems from version 17.x to 21.x in an active-standby HA environment.

The goal of this flow is not to automate speed, but to enforce safe sequencing, explicit validation gates, and clear decision points throughout the upgrade lifecycle.

This upgrade flow assumes that all pre-upgrade validation checks have passed and that rollback readiness is maintained at every phase.

---

## Design Principles

The upgrade flow is governed by the following principles:

- Change only one variable at a time
- Validate behavior before proceeding
- Treat failover as a risk event, not a formality
- Preserve rollback options at all stages
- Prefer pause and analysis over blind progression

The flow is intentionally conservative to reduce the likelihood of cascading failures.

---

## Upgrade Phases Overview

The upgrade process is divided into the following phases:

1. Upgrade Preparation Confirmation
2. Standby Node Upgrade
3. Controlled Failover and Validation
4. Former Active Node Upgrade
5. Post-Upgrade Stabilization

Progression between phases is conditional and requires explicit validation.

---

## Phase 0 — Upgrade Preparation Confirmation

### Objectives
Confirm that the environment remains in a known-good state immediately before execution.

### Required Actions
- Re-run critical prechecks if significant time has passed
- Confirm HA roles (Active vs Standby)
- Confirm monitoring and alerting visibility
- Confirm change window and communication readiness

**Decision Gate:**  
Proceed only if environment state matches pre-upgrade baseline.

---

## Phase 1 — Standby Node Upgrade

### Objectives
Upgrade the standby node while minimizing impact and preserving rollback options.

### Key Actions
- Isolate change to the standby node only
- Perform software install and reboot as required
- Monitor boot and service initialization behavior
- Verify configuration loads without critical errors

### Validation Requirements
- Standby node reaches stable operational state
- No unexpected config rewrite or load failures
- SSL/TLS services initialize correctly
- No errors that would prevent assuming active role

**Decision Gate:**  
If standby node cannot safely assume active role, do not proceed.

---

## Phase 2 — Controlled Failover and Behavioral Validation

### Objectives
Transition traffic to the upgraded node in a controlled and observable manner.

### Key Actions
- Notify stakeholders of controlled failover window
- Initiate planned failover
- Closely observe system behavior during transition

### Validation Requirements
- Traffic resumes within acceptable thresholds
- Error rates and latency remain within baseline tolerance
- SSL/TLS handshakes succeed consistently
- Persistence and session behavior meet expectations
- Monitoring confirms stability beyond initial transition

**Decision Gate:**  
If validation fails, initiate rollback or remediation before proceeding.

Failover is treated as a test, not a formality.

---

## Phase 3 — Former Active Node Upgrade

### Objectives
Upgrade the remaining node once the upgraded system is stable under load.

### Key Actions
- Upgrade the former active node
- Monitor for symmetry in behavior and configuration
- Re-establish HA pairing and sync

### Validation Requirements
- Both nodes operate on 21.x
- ConfigSync stabilizes without errors
- Failover readiness restored

**Decision Gate:**  
Proceed only when HA stability is confirmed.

---

## Phase 4 — Post-Upgrade Stabilization

### Objectives
Identify delayed or load-dependent issues introduced by the upgrade.

### Key Actions
- Observe system under sustained traffic
- Monitor logs for new or changed warnings
- Validate known high-risk traffic patterns
- Confirm operational tooling compatibility

### Validation Requirements
- No recurring critical errors
- Traffic behavior consistent with baseline
- Operational confidence restored

**Decision Gate:**  
Close upgrade only after stability is confirmed over a meaningful window.

---

## Rollback Considerations (Inline)

Rollback readiness is preserved throughout the upgrade flow.

Key points:
- Rollback paths must be understood before each phase
- Configuration drift may affect rollback safety
- Rollback should be executed decisively when required

Detailed rollback strategy is defined in:
- `rollback/README.md`

---

## Flow Control Summary

At each phase, the upgrade flow enforces one of three actions:

- **Proceed** — validation successful
- **Pause** — investigate and remediate
- **Rollback** — restore prior state

The absence of errors does not imply success. Behavior determines progression.

---

## What This Flow Avoids

This upgrade flow intentionally avoids:
- Tool-specific execution steps
- Vendor-specific automation assumptions
- One-size-fits-all timing guarantees

Its purpose is to enforce safe, repeatable decision-making across diverse enterprise environments.

---

## Next Steps

Once the controlled upgrade flow completes successfully, the environment transitions to long-term monitoring and operational validation.

For rollback design and execution details, refer to:
- `rollback/README.md`
