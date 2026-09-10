# F5 BIG-IP 17.x → 21.x Upgrade Automation Framework
## Comprehensive Technical Architecture, iControl REST vs. TMSH Reference, & Engineering Runbook

---

## 1. Executive Summary & Design Philosophy

Upgrading an enterprise F5 BIG-IP Active/Standby cluster across major TMOS versions (from **17.x to 21.x**) involves significant operational risk. Traditional manual upgrade methods rely heavily on human procedural discipline, which frequently fails due to:
* Configuration drift between cluster peers.
* Latent partition syntax errors in `/config/partitions/*/bigip.conf`.
* Unvalidated failover transitions under live traffic.
* Lack of deep pre/post runtime traffic baselining.

This automation framework enforces **five core architectural principles**:

1. **Standby-First Blast Radius Containment:** All destructive operations (software installation, volume creation, and reboot) are strictly hard-locked to the `STANDBY` node. If an `ACTIVE` node is targeted, the script automatically refuses destructive actions.
2. **Multi-Partition Configuration Verification (`CFG-001`):** Validates the entire configuration tree across all tenant partitions (`tmsh load sys config verify partitions all`) before any image install or reboot is attempted.
3. **Decoupled Deep State Snapshots:** Captures 13 distinct subsystems (Virtual Servers, Pools, Nodes, Routes, Interfaces, Sync status, etc.) in structured JSON and multi-sheet Excel format.
4. **Automated Regression Classification (Diff Engine):** Compares pre-upgrade and post-upgrade runtime states to automatically flag critical regressions (e.g., Virtual Server moving from `available` to `offline` or pool member count drop).
5. **Non-Blocking, Masked Credential Security:** Eliminates plain-text passwords in environment variables and shell history by leveraging secure masked interactive prompting via Python `getpass`.

---

## 2. End-to-End Workflow & Pipeline Architecture

```
+-----------------------------------------------------------------------------------------+
|                    F5 BIG-IP 17.x -> 21.x AUTOMATED UPGRADE PIPELINE                   |
+-----------------------------------------------------------------------------------------+
                                             |
                                             v
 [ STAGE 0: SECURE INITIALIZATION ]
   * Read BIGIP_HOST, BIGIP_USER from environment
   * Secure masked getpass prompt for Password (no plain text in bash history)
                                             |
                                             v
 [ STAGE 1: HARD DECISION PRECHECKS (make prechecks) ]
   * PLAT-001: Version reachability via iControl REST (/mgmt/tm/sys/version)
   * CFG-001:  Syntax check across all partitions ('tmsh load sys config verify partitions all')
               --> Fatal Errors = Hard FAIL (Halt)
               --> Warnings = Display details & Prompt Operator (y/N)
   * HA-001 / HA-002: Failover & ConfigSync state confirmation
   * RB-001 / PLAT-002: Disk volume availability & License reachability
                                             |
                                             v
 [ STAGE 2: BASELINE SNAPSHOT & BACKUPS (make state-pre) ]
   * Collect 13 REST Tables -> outputs/snapshots/pre_state_<host>.json / .xlsx
   * Execute UCS Backup (timeout=600s) -> /var/local/ucs/<hostname>-<HHMM-MMDDYY>.ucs
   * Execute SCF Backup (timeout=300s) -> /var/local/scf/<hostname>-<HHMM-MMDDYY>.scf
                                             |
                                             v
 [ STAGE 3: CONTROLLED STANDBY UPGRADE (make flow) ]
   * Enforce Role = STANDBY (Active nodes are automatically skipped)
   * Target Image Check (/mgmt/tm/sys/software/image or /shared/images fallback)
   * Install ISO to Target Volume ('cd /shared/images && tmsh install sys software image ... volume HD1.2')
   * Poll Volume Completion (/mgmt/tm/sys/software/volume/HD1.2 every 20s up to 3600s)
   * Reboot Standby into New Volume ('tmsh reboot volume HD1.2')
   * Poll Post-Boot Health (/mgmt/tm/sys/version up to 900s, verify standby role)
                                             |
                                             v
 [ STAGE 4: POST-UPGRADE SNAPSHOT (make state-post) ]
   * Collect 13 REST Tables on upgraded 21.x node -> outputs/snapshots/post_state_<host>.json
                                             |
                                             v
 [ STAGE 5: AUTOMATED DIFFERENCE ENGINE (make state-diff) ]
   * Compare Pre vs Post Snapshots
   * Classify: 🔴 CRITICAL (VS available->offline, active pool member drop)
               🟡 WARNING (status reason changes, disabled objects)
               🟢 PASS (all runtime objects healthy)
   * Generate Markdown & JSON Diff Reports (outputs/diff_report_<host>.md)
                                             |
                                             v
 [ STAGE 6: HA RECOVERY & FAILOVER VALIDATION (make ha) ]
   * Validate Trust Domain, ConfigSync In-Sync, and Traffic Group 1 ownership
+-----------------------------------------------------------------------------------------+
```

---

## 3. Deep-Dive: iControl REST vs. TMSH (Where & Why)

### 3.1 Where and Why We Use iControl REST

**iControl REST** is the preferred interface for reading structured state, polling asynchronous status, and querying performance metrics because it returns native JSON schemas that Python can parse without brittle regex string splitting.

| iControl REST Endpoint | HTTP Method & Module | Technical Rationale & Why REST is Used |
| :--- | :--- | :--- |
| `/mgmt/tm/sys/version` | `GET` / `checks.py`, `flow.py` | Fast platform reachability and version verification. Returns JSON object containing exact TMOS version, build, and edition. |
| `/mgmt/tm/cm/failover-status` | `GET` / `ha.py`, `checks.py` | Captures local failover role (`ACTIVE`/`STANDBY`). Used by safety locks to refuse destructive execution on active nodes. |
| `/mgmt/tm/cm/sync-status` | `GET` / `checks.py`, `ha_recovery.py` | Extracts ConfigSync color, status, and synchronization details between HA cluster members. |
| `/mgmt/tm/sys/software/volume` | `GET` / `checks.py`, `execution.py` | Polls boot volume installation progress (`status='installing'`, `'testing archives'`, `'complete'`) safely with 0% risk of command hang. |
| `/mgmt/tm/sys/software/image` | `GET` / `execution.py` | Discovers all ISO images imported into the BIG-IP software repository. |
| `/mgmt/tm/cm/device` | `GET` / `discovery.py`, `mgmt.py` | Enumerates cluster trust domain devices, hostnames, and resolves management IP addresses. |
| `/mgmt/tm/ltm/virtual/stats` | `GET` / `state_collector.py` | Retrieves structured `availabilityState`, `enabledState`, and `statusReason` for all Virtual Servers across partitions. |
| `/mgmt/tm/ltm/pool/stats` | `GET` / `state_collector.py` | Extracts `activeMemberCnt`, `availableMemberCnt`, and pool health without screen-scraping text. |
| `/mgmt/tm/ltm/node/stats` | `GET` / `state_collector.py` | Extracts node IP availability, enabled state, and monitor status codes. |
| `/mgmt/tm/net/interface/stats` | `GET` / `state_collector.py` | Reads hardware interface status (`up`/`down`), `bitsIn`, `bitsOut`, and packet drop/error counters. |
| `/mgmt/tm/sys/performance/system` | `GET` / `state_collector.py` | Captures CPU usage, TMM memory, and throughput statistics for pre/post baseline. |

---

### 3.2 Where and Why We Use TMSH (via `/mgmt/tm/util/bash`)

**TMSH commands** executed through the secure `/mgmt/tm/util/bash` endpoint are reserved for operational actions where iControl REST either lacks an equivalent declarative endpoint, where recursion across all tenant administrative partitions is mandatory, or where direct TMOS system execution is required.

| TMSH Command Executed | Script Module | Technical Rationale & Why TMSH is Used |
| :--- | :--- | :--- |
| `tmsh load sys config verify partitions all` | `checks.py` (`CFG-001`) | Compiles the entire configuration tree across **ALL** tenant partitions in memory. Catches syntax/schema errors before upgrade without changing running state. |
| `tmsh save sys ucs <hostname>-<timestamp>` | `state_collector.py`, `backup.py` | Generates the authoritative User Configuration Set (UCS) backup file including SSL keys, certificates, licenses, and base configs. (Handled with 600s timeout). |
| `tmsh save sys config file <name> no-passphrase` | `state_collector.py`, `backup.py` | Generates flat Single Configuration File (SCF) in plain text for human-readable audit and line-by-line configuration comparison. (Handled with 300s timeout). |
| `cd /shared/images && tmsh install sys software image <iso> volume <vol> create-volume` | `execution.py` | Triggers the TMOS disk partitioning and software installation engine to write the new OS image into target boot location HD1.2. The ISO is passed by filename after changing to `/shared/images`. |
| `tmsh reboot volume <vol>` | `execution.py` | Commands TMOS bootloader (GRUB) to set the active boot volume and reboot the standby appliance into version 21.x. |
| `tmsh -q -c "cd /; list ltm virtual recursive"` | `state_collector.py` | Recursively lists all virtual servers across all administrative partitions (`/Common`, `/cloud_dev`, `/tenant_xyz`) as audit-grade evidence. |
| `tmsh -q show cm traffic-group` | `ha_recovery.py` | Verifies active/standby ownership of `traffic-group-1` to ensure exactly one active and one standby instance post-upgrade. |

---

## 4. Stakeholder & Interview Defense Q&A

Use these points to answer questions from management, senior architects, or interviewers:

#### Q1: Why did you write custom Python scripts instead of using Ansible or Terraform?
> **Answer:** Ansible and Terraform are designed for desired-state configuration management (CRUD operations on VIPs/pools), but they are inadequate for multi-stage, state-aware OS upgrades. An upgrade is not a static configuration change—it is a temporal state transition requiring sub-second decision gates, recursive partition verification, continuous HTTP disconnect handling during reboots, and multi-subsystem diff analysis. Python gives us native control over session timeouts, custom exception handling (e.g., expected socket dropouts during reboot), and lightweight zero-dependency portability.

#### Q2: Why is the upgrade strictly Standby-First?
> **Answer:** In an Active/Standby HA architecture, the standby node carries 0% of production traffic. Upgrading the standby first confines the blast radius of software installation, volume creation, and reboot to an idle node. If the upgrade fails on the standby, production traffic on the active node is completely untouched, allowing zero-downtime rollback simply by leaving traffic where it is.

#### Q3: Why was CFG-001 (`load sys config verify partitions all`) added as a precheck?
> **Answer:** One of the leading causes of post-reboot upgrade outages is latent configuration syntax errors. Administrators frequently edit iRules, SSL profiles, or partition configs that remain uncompiled in memory until a reboot forces a full config reload. By running `tmsh load sys config verify partitions all`, we force TMOS to parse and compile every tenant partition in memory before touching any software image. If fatal errors exist, we halt before the upgrade, eliminating surprise boot-loop failures.

#### Q4: Why did we need custom timeout overrides for UCS/SCF backups?
> **Answer:** By default, HTTP REST clients use short timeouts (e.g., 20 seconds). On production BIG-IPs with dozens of partitions, hundreds of SSL certificates, and heavy configs, generating a UCS archive or compiling all partitions takes 45 to 180+ seconds. Without custom timeout overrides (e.g., 600s for UCS, 300s for SCF), the REST client throws a ReadTimeout exception while the BIG-IP is still working. We added explicit timeout parameters to `BigIPClient.run_bash()` to support large enterprise workloads.

#### Q5: How does the Diff Engine work and what does it prevent?
> **Answer:** The Diff Engine performs deep state comparison across 13 subsystems. It takes the pre-upgrade snapshot and post-upgrade snapshot, maps each object by name and partition, and classifies changes into CRITICAL (e.g., Virtual Server was 'available' pre-upgrade and became 'offline' post-upgrade, or active pool member count dropped) vs WARNING vs PASS. This eliminates "silent failures" where an upgrade appears successful on the console, but specific application VIPs or pool members failed to initialize.

---

## 5. Module-by-Module Codebase Walkthrough

* **`src/f5upgrade/config.py` (Environment & Credentials):**
  Parses `BIGIP_HOST`, `BIGIP_USER`, `TARGET_VOLUME`, etc. Uses `getpass.getpass()` when `BIGIP_PASS` is omitted to ensure passwords are never stored in plain text or terminal history.
* **`src/f5upgrade/bigip_client.py` (Unified REST & Bash Client):**
  Wraps Python `requests` with HTTP Basic Authentication, disables `InsecureRequestWarning` for self-signed certificates, and provides `get()`, `post()`, and `run_bash()` with custom timeout overrides.
* **`src/f5upgrade/checks.py` (Pre-Upgrade Decision Gates):**
  Implements `PLAT-001` (system reachability), `CFG-001` (`tmsh load sys config verify partitions all` with interactive warning prompt), `HA-001` (failover state), `HA-002` (ConfigSync), and `RB-001` (software volume visibility).
* **`src/f5upgrade/state_collector.py` (13-Table Snapshot & Backup Generator):**
  Extracts structured JSON and multi-sheet Excel tables across Virtual Servers, Pools, Nodes, Routes, Interfaces, DNS, NTP, Syslog, and Performance. Triggers timestamped UCS and SCF backups with extended timeouts.
* **`src/f5upgrade/diff_engine.py` (Pre vs. Post Regression Analyzer):**
  Computes exact deltas across pre/post JSON snapshots. Identifies missing objects, state flips (`available` $\to$ `offline`), and pool member count drops. Generates `outputs/diff_report_<host>.md`.
* **`src/f5upgrade/flow.py` (Standby-First Upgrade Orchestrator):**
  Coordinates the end-to-end upgrade flow. Validates that the targeted node is `STANDBY`, skips destructive steps on `ACTIVE` nodes, checks image presence, triggers installation, polls volume readiness, reboots, and validates post-boot health.
* **`src/f5upgrade/execution.py` (Low-Level TMOS Software & Boot Operators):**
  Executes `tmsh install sys software image`, polls `/mgmt/tm/sys/software/volume`, executes `tmsh reboot volume`, and traps expected socket disconnects during appliance reboot.
* **`src/f5upgrade/ha_recovery.py` (Post-Upgrade HA Validator):**
  Validates trust-domain health, verifies `cm sync-status` is `In Sync`, and confirms `traffic-group-1` has exactly one active and one standby member.
