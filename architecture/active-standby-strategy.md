# Active-Standby Upgrade Architecture (BIG-IP 17.x → 21.x)

## Purpose

This document explains the architectural strategy for upgrading F5 BIG-IP systems from 17.x to 21.x in an Active/Standby high-availability (HA) deployment with minimal disruption.

The intent is not to provide a list of commands, but to define the decision model and validation gates required to reduce risk during major-version upgrades.

---

## Why Active-Standby Upgrades Fail in Production

In enterprise environments, “upgrade the standby, fail over, upgrade the former active” is often treated as a complete strategy. In reality, this approach frequently fails due to behavioral gaps between assumptions and production conditions:

- State synchronization is not always complete at the moment it is needed.
- Traffic drain is frequently partial or misleading.
- Failover is not guaranteed to produce identical runtime behavior across versions.
- Timing windows (policy reload, SSL session tables, persistence records) can cause intermittent failures after failover.
- Rollback is rarely rehearsed and often becomes riskier than continuing forward.

Therefore, the upgrade must be treated as a controlled system transition with explicit validation gates.

---

## Core Architectural Goals

1. **Preserve service availability**
   - Aim for zero downtime where feasible.
   - Accept brief, controlled impact only when validated and understood.

2. **Reduce unknowns during state transitions**
   - Limit simultaneous changes.
   - Verify behavior at each stage rather than relying on “upgrade complete” indicators.

3. **Maintain rollback readiness**
   - Rollback must remain possible at each phase, not only at the end.

---

## Operating Assumptions (Scope)

This architecture assumes:

- Two-node Active/Standby HA pair
- ConfigSync is enabled and operational
- Failover is tested and reliable under normal conditions
- Health monitors exist and are meaningful for the application
- Primary scope: LTM + SSL/TLS behavior (ASM/AWAF treated as considerations unless explicitly included)

If these assumptions are not true, upgrade risk increases and additional controls are required.

---

## Upgrade Strategy Overview

### Phase 0 — Baseline and Freeze
Establish a stable baseline and reduce variability:

- Confirm HA health and stable sync status
- Confirm monitoring and alerting signals are trusted
- Freeze configuration changes except for upgrade-required edits
- Capture baseline artifacts:
  - current version/build
  - active/standby roles
  - sync state
  - key traffic KPIs (error rate, latency, handshake failures)

**Exit Gate:** Baseline metrics stable and HA state clean.

---

### Phase 1 — Standby Upgrade (Change One Side Only)
Upgrade the Standby node first to reduce blast radius.

Key architectural point:
- The objective is not only “standby boots successfully”
- The objective is: **standby can assume active role without unexpected behavior**

**Exit Gate (behavioral):**
- Standby boots cleanly
- Objects load without critical errors
- SSL/TLS handshake tests succeed against representative endpoints
- LTM objects and pools validate expected state
- No unexpected config rewrite issues

---

### Phase 2 — Controlled Failover (Validated Transition)
Failover is treated as a risk event. Do not fail over blindly.

Before failover:
- Ensure sync consistency is understood
- Confirm application teams are aware of the controlled transition window
- Run targeted validation tests against standby while still standby where possible

Failover execution:
- Initiate controlled failover
- Observe behavior under real traffic
- Validate key functional checks immediately (not minutes later)

**Exit Gate:**
- Traffic resumes with acceptable error rate
- SSL/TLS behavior matches expectations
- Persistence/session behavior is within acceptable bounds
- Monitoring confirms stability for a sustained window (not a brief spike)

If failover validation fails, rollback decision must be available immediately.

---

### Phase 3 — Upgrade Former Active Node
Once the upgraded node is running as Active and stable, upgrade the remaining node.

**Exit Gate:**
- Second node upgraded successfully
- ConfigSync stability verified
- Failover readiness re-established (tested or at least validated)

---

### Phase 4 — Post-Upgrade Stabilization
Major upgrades frequently surface delayed issues (policy reload timing, SSL renegotiation edge cases, monitor behavior changes).

Stabilization includes:
- sustained traffic observation window
- validation against known “risky” traffic patterns
- review of logs for new warning/error patterns introduced by the upgrade

**Exit Gate:**
- Environment stable under normal and peak-like traffic patterns
- No critical regressions identified
- Rollback window expired by choice (not by accident)

---

## Decision Gates (The Non-Negotiable Part)

This architecture includes explicit decision gates:

- **Proceed** only when behavior matches expectations
- **Pause** when validation signals are inconsistent
- **Rollback** when service risk increases faster than uncertainty decreases

This is the difference between an upgrade procedure and an upgrade architecture.

---

## What This Document Avoids (Intentionally)

This strategy does not list specific commands because:
- enterprises differ by topology and policy complexity
- tooling choices (manual vs automation) are secondary to architectural controls
- the goal is to preserve safe decision-making regardless of implementation

Implementation details are provided separately in the repository directories (prechecks/, upgrade-flow/, validation/, rollback/).

---

## Next Documents

- `prechecks/README.md` — readiness and pre-upgrade validation model  
- `upgrade-flow/README.md` — sequencing logic and control points  
- `validation/README.md` — behavioral verification checks  
- `rollback/README.md` — rollback readiness and execution principles
