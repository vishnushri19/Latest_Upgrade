# Pre-Upgrade Validation Model (BIG-IP 17.x → 21.x)

## Purpose

This document defines the mandatory pre-upgrade validation model used to determine whether a BIG-IP environment is safe to enter the 17.x → 21.x upgrade workflow.

In enterprise environments, most upgrade failures are caused not by software defects, but by proceeding with upgrades while the system is already in a degraded or ambiguous state. The goal of these prechecks is to eliminate unknowns before any irreversible change occurs.

No upgrade phase should begin unless all required prechecks pass.

---

## Design Philosophy

Prechecks are treated as **hard decision gates**, not informational checks.

This model is based on the following principles:

- An unstable system cannot be safely upgraded.
- Ambiguous signals must be resolved before proceeding.
- Passing prechecks reduces blast radius and rollback risk.
- Automation must enforce these checks consistently.

If any required precheck fails, the upgrade must be paused until the condition is corrected or explicitly accepted as risk.

---

## Precheck Categories

The prechecks are organized into four logical categories:

1. Platform and Version State
2. High Availability Health
3. Configuration and Policy Integrity
4. Traffic and Behavioral Baseline

Each category addresses a different failure class observed during enterprise upgrades.

---

## 1. Platform and Version State

### Objectives
Confirm that the system is in a known, supported, and stable baseline state.

### Required Checks
- Current BIG-IP version and build confirmed (17.x baseline)
- Disk space and boot location availability verified
- No active software install or reboot operations in progress
- License status valid and not near expiration
- No pending EULA or post-upgrade prompts

**Rationale:**  
Upgrade failures frequently occur due to incomplete prior installs or environmental drift that goes unnoticed until reboot.

---

## 2. High Availability Health

### Objectives
Ensure HA behavior is predictable before introducing change.

### Required Checks
- HA status is stable (one active, one standby)
- ConfigSync state is clean and consistent
- No forced offline or disabled HA components
- Failover mechanism tested or recently validated
- Standby node is capable of assuming active role

**Rationale:**  
If HA behavior is uncertain before the upgrade, failover during the upgrade becomes an uncontrolled event.

---

## 3. Configuration and Policy Integrity

### Objectives
Verify that the configuration can be cleanly loaded and evaluated post-upgrade.

### Required Checks
- No configuration load errors or warnings
- Dependent objects (profiles, policies, iRules) resolve correctly
- SSL/TLS profiles reviewed for deprecated or changed defaults
- No unresolved references or orphaned objects
- ASM/AWAF (if present) health verified at a high level

**Rationale:**  
Major version upgrades frequently expose latent configuration issues that were tolerated in earlier releases.

---

## 4. Traffic and Behavioral Baseline

### Objectives
Establish a behavioral baseline against which post-upgrade behavior can be compared.

### Required Checks
- Current traffic volume and error rate documented
- SSL/TLS handshake success rate validated
- Persistence behavior observed under normal traffic
- Key application health endpoints verified
- Monitoring and alerting signals confirmed reliable

**Rationale:**  
Without a baseline, post-upgrade issues cannot be accurately attributed to the upgrade.

---

## Precheck Outcomes

Each precheck must result in one of the following outcomes:

- **Pass** — condition is satisfied, proceed
- **Fail** — condition is not satisfied, block upgrade
- **Risk Accepted** — condition is known and accepted with explicit approval

Risk acceptance must be documented and time-bound.

---

## Precheck Enforcement

Prechecks may be executed manually or through automation, but enforcement must be consistent.

Key enforcement rules:
- All required checks must pass before upgrade begins
- Failed checks cannot be bypassed silently
- Risk acceptance must be explicit, documented, and reversible

Automation should fail fast and prevent partial execution.

---

## What This Model Does Not Do

This precheck model intentionally avoids:
- Tool-specific commands
- Environment-specific thresholds
- Vendor marketing assumptions

Its purpose is to preserve safe decision-making across diverse enterprise environments.

---

## Next Steps

Once all prechecks pass, the environment may proceed to the controlled upgrade flow defined in:

- `upgrade-flow/README.md`

Prechecks should be re-evaluated if the environment changes materially before upgrade execution.
