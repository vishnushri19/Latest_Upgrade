#!/usr/bin/env python3
"""
Generates a comprehensive Word Document (.docx) explaining the F5 17.x -> 21.x
Upgrade Automation Framework, iControl REST vs TMSH usage, and stakeholder Q&A.
"""

from pathlib import Path
import docx
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_ALIGN_VERTICAL
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn


def set_cell_background(cell, fill_hex):
    """Sets cell background color in docx table."""
    tcPr = cell._tc.get_or_add_tcPr()
    shd = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{fill_hex}"/>')
    tcPr.append(shd)


def set_cell_margins(cell, top=100, bottom=100, left=150, right=150):
    """Sets cell padding."""
    tcPr = cell._tc.get_or_add_tcPr()
    tcMar = parse_xml(
        f'<w:tcMar {nsdecls("w")}>'
        f'<w:top w:w="{top}" w:type="dxa"/>'
        f'<w:bottom w:w="{bottom}" w:type="dxa"/>'
        f'<w:left w:w="{left}" w:type="dxa"/>'
        f'<w:right w:w="{right}" w:type="dxa"/>'
        f'</w:tcMar>'
    )
    tcPr.append(tcMar)


def create_document():
    doc = docx.Document()

    # Page setup - 1 inch margins
    sections = doc.sections
    for section in sections:
        section.top_margin = Inches(1.0)
        section.bottom_margin = Inches(1.0)
        section.left_margin = Inches(1.0)
        section.right_margin = Inches(1.0)

    # Styles
    styles = doc.styles
    normal_style = styles['Normal']
    normal_style.font.name = 'Calibri'
    normal_style.font.size = Pt(11)
    normal_style.font.color.rgb = RGBColor(0x22, 0x22, 0x22)

    # --- Title Page / Header ---
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run_title = title.add_run("F5 BIG-IP 17.x → 21.x UPGRADE AUTOMATION FRAMEWORK")
    run_title.bold = True
    run_title.font.size = Pt(22)
    run_title.font.color.rgb = RGBColor(0x00, 0x33, 0x66)  # F5 Deep Navy

    subtitle = doc.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run_sub = subtitle.add_run("Comprehensive Technical Architecture, iControl REST vs TMSH Reference, & Engineering Runbook")
    run_sub.font.size = Pt(13)
    run_sub.font.italic = True
    run_sub.font.color.rgb = RGBColor(0x55, 0x55, 0x55)

    doc.add_paragraph()  # Spacer

    # Callout Box: Document Overview
    tbl_callout = doc.add_table(rows=1, cols=1)
    tbl_callout.alignment = WD_TABLE_ALIGNMENT.CENTER
    cell_callout = tbl_callout.rows[0].cells[0]
    set_cell_background(cell_callout, "F0F4F8")
    set_cell_margins(cell_callout, top=140, bottom=140, left=200, right=200)
    p_callout = cell_callout.paragraphs[0]
    r_c1 = p_callout.add_run("Target Audience & Purpose:\n")
    r_c1.bold = True
    r_c1.font.size = Pt(11)
    r_c1.font.color.rgb = RGBColor(0x00, 0x33, 0x66)
    r_c2 = p_callout.add_run(
        "This guide is designed for network engineers, F5 administrators, and architects. "
        "It provides a line-by-line explanation of why this framework was built in Python, "
        "the exact architectural decision gates, where iControl REST vs. TMSH are used, "
        "and how to defend every design choice in stakeholder reviews."
    )
    r_c2.font.size = Pt(10.5)

    doc.add_paragraph()

    # ==========================================
    # SECTION 1: EXECUTIVE SUMMARY & PHILOSOPHY
    # ==========================================
    h1 = doc.add_heading("1. Executive Summary & Design Philosophy", level=1)
    h1.paragraph_format.space_before = Pt(16)

    doc.add_paragraph(
        "Upgrading an enterprise F5 BIG-IP Active/Standby cluster across major TMOS versions "
        "(from 17.x to 21.x) involves significant operational risk. Traditional manual upgrade methods "
        "rely heavily on human procedural discipline, which frequently fails due to configuration drift, "
        "latent partition syntax errors, unvalidated failover states, and lack of pre/post traffic baseline comparisons."
    )

    doc.add_paragraph("This automation framework enforces five non-negotiable architectural principles:")

    bullets = [
        ("Standby-First Blast Radius Containment: ", "All destructive actions (software install, boot volume creation, reboot) are strictly hard-locked to the STANDBY node. If an ACTIVE node is targeted, the script automatically refuses to execute destructive steps."),
        ("Multi-Partition Configuration Verification (CFG-001): ", "Validates the entire TMOS configuration tree across all administrative tenant partitions before any upgrade action begins."),
        ("Decoupled Deep State Snapshots: ", "Captures 13 distinct subsystems (Virtual Servers, Pools, Nodes, Routes, Interfaces, Sync, etc.) in structured JSON and multi-sheet Excel format before and after upgrade."),
        ("Automated Regression Classification (Diff Engine): ", "Compares pre-upgrade and post-upgrade runtime states to automatically flag critical regressions (e.g., Virtual Server moving from 'available' to 'offline' or pool member loss)."),
        ("Non-Blocking, Masked Credential Security: ", "Eliminates plain-text passwords in environment variables and shell history by leveraging secure masked interactive prompting.")
    ]

    for b_title, b_desc in bullets:
        p = doc.add_paragraph(style='List Bullet')
        r1 = p.add_run(b_title)
        r1.bold = True
        r1.font.color.rgb = RGBColor(0x00, 0x33, 0x66)
        p.add_run(b_desc)

    doc.add_paragraph()

    # ==========================================
    # SECTION 2: ARCHITECTURE & WORKFLOW DIAGRAM
    # ==========================================
    h2 = doc.add_heading("2. End-to-End Workflow & Pipeline Architecture", level=1)
    h2.paragraph_format.space_before = Pt(16)

    doc.add_paragraph(
        "The upgrade framework follows a linear, validation-gated execution pipeline. "
        "Progression from one stage to the next is strictly conditional based on exit gates."
    )

    # ASCII Architecture Diagram in Box
    tbl_diag = doc.add_table(rows=1, cols=1)
    tbl_diag.alignment = WD_TABLE_ALIGNMENT.CENTER
    cell_diag = tbl_diag.rows[0].cells[0]
    set_cell_background(cell_diag, "F8F9FA")
    set_cell_margins(cell_diag, top=120, bottom=120, left=150, right=150)
    p_diag = cell_diag.paragraphs[0]
    
    diagram_text = (
        "+-----------------------------------------------------------------------------------------+\n"
        "|                    F5 BIG-IP 17.x -> 21.x AUTOMATED UPGRADE PIPELINE                   |\n"
        "+-----------------------------------------------------------------------------------------+\n"
        "                                             |\n"
        "                                             v\n"
        " [ STAGE 0: SECURE INITIALIZATION ]\n"
        "   * Read BIGIP_HOST, BIGIP_USER from environment\n"
        "   * Secure masked getpass prompt for Password (no plain text in bash history)\n"
        "                                             |\n"
        "                                             v\n"
        " [ STAGE 1: HARD DECISION PRECHECKS (make prechecks) ]\n"
        "   * PLAT-001: Version reachability via iControl REST (/mgmt/tm/sys/version)\n"
        "   * CFG-001:  Syntax check across all partitions ('tmsh load sys config verify partitions all')\n"
        "               --> Fatal Errors = Hard FAIL (Halt)\n"
        "               --> Warnings = Display details & Prompt Operator (y/N)\n"
        "   * HA-001 / HA-002: Failover & ConfigSync state confirmation\n"
        "   * RB-001 / PLAT-002: Disk volume availability & License reachability\n"
        "                                             |\n"
        "                                             v\n"
        " [ STAGE 2: BASELINE SNAPSHOT & BACKUPS (make state-pre) ]\n"
        "   * Collect 13 REST Tables -> outputs/snapshots/pre_state_<host>.json / .xlsx\n"
        "   * Execute UCS Backup (timeout=600s) -> /var/local/ucs/<hostname>-<HHMM-MMDDYY>.ucs\n"
        "   * Execute SCF Backup (timeout=300s) -> /var/local/scf/<hostname>-<HHMM-MMDDYY>.scf\n"
        "                                             |\n"
        "                                             v\n"
        " [ STAGE 3: CONTROLLED STANDBY UPGRADE (make flow) ]\n"
        "   * Enforce Role = STANDBY (Active nodes are automatically skipped)\n"
        "   * Target Image Check (/mgmt/tm/sys/software/image or /shared/images fallback)\n"
        "   * Install ISO to Target Volume ('tmsh install sys software image ... volume HD1.2')\n"
        "   * Poll Volume Completion (/mgmt/tm/sys/software/volume/HD1.2 every 20s up to 3600s)\n"
        "   * Reboot Standby into New Volume ('tmsh reboot volume HD1.2')\n"
        "   * Poll Post-Boot Health (/mgmt/tm/sys/version up to 900s, verify standby role)\n"
        "                                             |\n"
        "                                             v\n"
        " [ STAGE 4: POST-UPGRADE SNAPSHOT (make state-post) ]\n"
        "   * Collect 13 REST Tables on upgraded 21.x node -> outputs/snapshots/post_state_<host>.json\n"
        "                                             |\n"
        "                                             v\n"
        " [ STAGE 5: AUTOMATED DIFFERENCE ENGINE (make state-diff) ]\n"
        "   * Compare Pre vs Post Snapshots\n"
        "   * Classify: 🔴 CRITICAL (VS available->offline, active pool member drop)\n"
        "               🟡 WARNING (status reason changes, disabled objects)\n"
        "               🟢 PASS (all runtime objects healthy)\n"
        "   * Generate Markdown & JSON Diff Reports (outputs/diff_report_<host>.md)\n"
        "                                             |\n"
        "                                             v\n"
        " [ STAGE 6: HA RECOVERY & FAILOVER VALIDATION (make ha) ]\n"
        "   * Validate Trust Domain, ConfigSync In-Sync, and Traffic Group 1 ownership\n"
        "+-----------------------------------------------------------------------------------------+"
    )
    run_diag = p_diag.add_run(diagram_text)
    run_diag.font.name = 'Consolas'
    run_diag.font.size = Pt(8.5)

    doc.add_paragraph()

    # ==========================================
    # SECTION 3: iControl REST vs TMSH COMMANDS
    # ==========================================
    h3 = doc.add_heading("3. Deep-Dive: iControl REST vs. TMSH (Where & Why)", level=1)
    h3.paragraph_format.space_before = Pt(16)

    doc.add_paragraph(
        "A common interview and architectural question is: "
        "\"Why does the framework use iControl REST for some tasks and TMSH for others?\" "
        "The answer lies in understanding the strengths and limitations of each interface in F5 TMOS."
    )

    h3_sub1 = doc.add_heading("3.1 Where and Why We Use iControl REST", level=2)
    doc.add_paragraph(
        "iControl REST is the preferred interface for reading structured state, polling asynchronous status, "
        "and querying performance statistics because it returns native JSON schemas that Python can parse without brittle regex string splitting."
    )

    # Table of iControl REST Endpoints
    tbl_rest = doc.add_table(rows=1, cols=3)
    tbl_rest.alignment = WD_TABLE_ALIGNMENT.CENTER
    tbl_rest.autofit = False

    headers_rest = ["iControl REST Endpoint", "Method / Module", "Technical Rationale & Why REST is Used"]
    hdr_cells = tbl_rest.rows[0].cells
    for i, h_text in enumerate(headers_rest):
        hdr_cells[i].text = h_text
        set_cell_background(hdr_cells[i], "003366")
        set_cell_margins(hdr_cells[i], top=100, bottom=100, left=120, right=120)
        p = hdr_cells[i].paragraphs[0]
        p.runs[0].font.bold = True
        p.runs[0].font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        p.runs[0].font.size = Pt(9.5)

    rest_data = [
        ("/mgmt/tm/sys/version", "GET / checks.py, flow.py", "Fast platform reachability and version verification. Returns JSON object containing exact TMOS version, build, and edition."),
        ("/mgmt/tm/cm/failover-status", "GET / ha.py, checks.py", "Captures local failover role (ACTIVE/STANDBY). Used by safety locks to refuse execution on active nodes."),
        ("/mgmt/tm/cm/sync-status", "GET / checks.py, ha_recovery.py", "Extracts ConfigSync color, status, and synchronization details between HA cluster members."),
        ("/mgmt/tm/sys/software/volume", "GET / checks.py, execution.py", "Polls boot volume installation progress (status='installing', 'testing archives', 'complete') with 0% risk of command hang."),
        ("/mgmt/tm/sys/software/image", "GET / execution.py", "Discovers all ISO images imported into the BIG-IP software repository."),
        ("/mgmt/tm/cm/device", "GET / discovery.py, mgmt.py", "Enumerates cluster trust domain devices, hostnames, and resolves management IP addresses."),
        ("/mgmt/tm/ltm/virtual/stats", "GET / state_collector.py", "Retrieves structured availabilityState, enabledState, and statusReason for all Virtual Servers."),
        ("/mgmt/tm/ltm/pool/stats", "GET / state_collector.py", "Extracts activeMemberCnt, availableMemberCnt, and pool health without screen-scraping text."),
        ("/mgmt/tm/ltm/node/stats", "GET / state_collector.py", "Extracts node IP availability, enabled state, and monitor status codes."),
        ("/mgmt/tm/net/interface/stats", "GET / state_collector.py", "Reads hardware interface status (up/down), bitsIn, bitsOut, and packet drop/error counters."),
        ("/mgmt/tm/sys/performance/system", "GET / state_collector.py", "Captures CPU usage, TMM memory, and throughput statistics for pre/post baseline.")
    ]

    for ep, mod, rat in rest_data:
        row = tbl_rest.add_row()
        c0, c1, c2 = row.cells
        c0.text = ep
        c1.text = mod
        c2.text = rat
        for c in (c0, c1, c2):
            set_cell_margins(c, top=80, bottom=80, left=100, right=100)
            c.paragraphs[0].runs[0].font.size = Pt(8.5)
        c0.paragraphs[0].runs[0].font.name = 'Consolas'
        set_cell_background(c0, "F8F9FA")

    doc.add_paragraph()

    h3_sub2 = doc.add_heading("3.2 Where and Why We Use TMSH (via /mgmt/tm/util/bash)", level=2)
    doc.add_paragraph(
        "TMSH commands executed through the secure `/mgmt/tm/util/bash` endpoint are reserved for "
        "operational and administrative actions where iControl REST either lacks an equivalent declarative endpoint, "
        "where recursion across all tenant administrative partitions is mandatory, or where direct TMOS system execution is required."
    )

    # Table of TMSH Commands
    tbl_tmsh = doc.add_table(rows=1, cols=3)
    tbl_tmsh.alignment = WD_TABLE_ALIGNMENT.CENTER
    tbl_tmsh.autofit = False

    headers_tmsh = ["TMSH Command Executed", "Script Module", "Technical Rationale & Why TMSH is Used"]
    hdr_cells_t = tbl_tmsh.rows[0].cells
    for i, h_text in enumerate(headers_tmsh):
        hdr_cells_t[i].text = h_text
        set_cell_background(hdr_cells_t[i], "003366")
        set_cell_margins(hdr_cells_t[i], top=100, bottom=100, left=120, right=120)
        p = hdr_cells_t[i].paragraphs[0]
        p.runs[0].font.bold = True
        p.runs[0].font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        p.runs[0].font.size = Pt(9.5)

    tmsh_data = [
        ("tmsh load sys config verify partitions all", "checks.py (CFG-001)", "Tests and compiles the entire configuration tree across ALL tenant partitions in memory. Catches syntax/schema errors before upgrade without changing running state."),
        ("tmsh save sys ucs <hostname>-<timestamp>", "state_collector.py, backup.py", "Generates the authoritative User Configuration Set (UCS) backup file including SSL keys, certificates, licenses, and base configs. Handled with 600s timeout."),
        ("tmsh save sys config file <name> no-passphrase", "state_collector.py, backup.py", "Generates flat Single Configuration File (SCF) in plain text for human-readable audit and line-by-line configuration comparison."),
        ("tmsh install sys software image <iso> volume <vol> create-volume", "execution.py", "Triggers the TMOS disk partitioning and software installation engine to write the new OS image into target boot location HD1.2."),
        ("tmsh reboot volume <vol>", "execution.py", "Commands TMOS bootloader (GRUB) to set the active boot volume and reboot the standby appliance into version 21.x."),
        ("tmsh -q -c \"cd /; list ltm virtual recursive\"", "state_collector.py", "Recursively lists all virtual servers across all administrative partitions (Common, cloud_dev, tenant_xyz) as audit-grade evidence."),
        ("tmsh -q show cm traffic-group", "ha_recovery.py", "Verifies active/standby ownership of traffic-group-1 to ensure exactly one active and one standby instance post-upgrade.")
    ]

    for cmd, mod, rat in tmsh_data:
        row = tbl_tmsh.add_row()
        c0, c1, c2 = row.cells
        c0.text = cmd
        c1.text = mod
        c2.text = rat
        for c in (c0, c1, c2):
            set_cell_margins(c, top=80, bottom=80, left=100, right=100)
            c.paragraphs[0].runs[0].font.size = Pt(8.5)
        c0.paragraphs[0].runs[0].font.name = 'Consolas'
        set_cell_background(c0, "F8F9FA")

    doc.add_paragraph()

    # ==========================================
    # SECTION 4: INTERVIEW & DEFENSE CHEAT SHEET
    # ==========================================
    h4 = doc.add_heading("4. Stakeholder & Interview Defense Q&A", level=1)
    h4.paragraph_format.space_before = Pt(16)

    doc.add_paragraph(
        "Use this section to confidently answer questions from management, senior architects, "
        "or interviewers regarding the rationale behind each technical decision."
    )

    qa_items = [
        ("Q1: Why did you write custom Python scripts instead of using Ansible or Terraform?",
         "Answer: Ansible and Terraform are excellent for desired-state configuration management (CRUD operations on VIPs/pools), but they are inadequate for multi-stage, state-aware OS upgrades. An upgrade is not a static configuration change—it is a temporal state transition requiring sub-second decision gates, recursive partition verification, continuous HTTP disconnect handling during reboots, and multi-subsystem diff analysis. Python gives us native control over session timeouts, custom exception handling (e.g., expected socket dropouts during reboot), and lightweight zero-dependency portability."),

        ("Q2: Why is the upgrade strictly Standby-First?",
         "Answer: In an Active/Standby HA architecture, the standby node carries 0% of production traffic. Upgrading the standby first confines the blast radius of software installation, volume creation, and reboot to an idle node. If the upgrade fails on the standby, production traffic on the active node is completely untouched, allowing zero-downtime rollback simply by leaving traffic where it is."),

        ("Q3: Why was CFG-001 (load sys config verify partitions all) added as a precheck?",
         "Answer: One of the leading causes of post-reboot upgrade outages is latent configuration syntax errors. Administrators frequently edit iRules, SSL profiles, or partition configs that remain uncompiled in memory until a reboot forces a full config reload. By running 'tmsh load sys config verify partitions all', we force TMOS to parse and compile every tenant partition in memory before touching any software image. If fatal errors exist, we halt before the upgrade, eliminating surprise boot-loop failures."),

        ("Q4: Why did we need custom timeout overrides for UCS/SCF backups?",
         "Answer: By default, HTTP REST clients use short timeouts (e.g., 20 seconds). On production BIG-IPs with dozens of partitions, hundreds of SSL certificates, and heavy configs, generating a UCS archive or compiling all partitions takes 45 to 180+ seconds. Without custom timeout overrides (e.g., 600s for UCS, 300s for SCF), the REST client throws a ReadTimeout exception while the BIG-IP is still working. We added explicit timeout parameters to BigIPClient.run_bash() to support large enterprise workloads."),

        ("Q5: How does the Diff Engine work and what does it prevent?",
         "Answer: The Diff Engine performs deep state comparison across 13 subsystems. It takes the pre-upgrade snapshot and post-upgrade snapshot, maps each object by name and partition, and classifies changes into CRITICAL (e.g., Virtual Server was 'available' pre-upgrade and became 'offline' post-upgrade, or active pool member count dropped) vs WARNING vs PASS. This eliminates 'silent failures' where an upgrade appears successful on the console, but specific application VIPs or pool members failed to initialize.")
    ]

    for q_text, a_text in qa_items:
        p_q = doc.add_paragraph()
        r_q = p_q.add_run(q_text)
        r_q.bold = True
        r_q.font.size = Pt(11)
        r_q.font.color.rgb = RGBColor(0x00, 0x33, 0x66)

        p_a = doc.add_paragraph()
        r_a = p_a.add_run(a_text)
        r_a.font.size = Pt(10.5)
        p_a.paragraph_format.space_after = Pt(8)

    doc.add_paragraph()

    # ==========================================
    # SECTION 5: MODULE-BY-MODULE ARCHITECTURE
    # ==========================================
    h5 = doc.add_heading("5. Codebase Module-by-Module Walkthrough", level=1)
    h5.paragraph_format.space_before = Pt(16)

    modules = [
        ("src/f5upgrade/config.py", "Environment Configuration & Secure Password Prompting",
         "Parses BIGIP_HOST, BIGIP_USER, TARGET_VOLUME, etc. Uses getpass.getpass() when BIGIP_PASS is omitted to ensure passwords are never stored in plain text or terminal history."),

        ("src/f5upgrade/bigip_client.py", "Unified iControl REST & Bash Client",
         "Wraps Python requests with HTTP Basic Authentication, disables InsecureRequestWarning for self-signed certificates, and provides get(), post(), and run_bash() with custom timeout overrides."),

        ("src/f5upgrade/checks.py", "Pre-Upgrade Decision Gates & Config Verification",
         "Implements PLAT-001 (system reachability), CFG-001 (tmsh load sys config verify partitions all with interactive warning prompt), HA-001 (failover state), HA-002 (ConfigSync), and RB-001 (software volume visibility)."),

        ("src/f5upgrade/state_collector.py", "13-Table State Snapshot & Backup Generator",
         "Extracts structured JSON and multi-sheet Excel tables across Virtual Servers, Pools, Nodes, Routes, Interfaces, DNS, NTP, Syslog, and Performance. Triggers timestamped UCS and SCF backups."),

        ("src/f5upgrade/diff_engine.py", "Pre vs. Post Difference & Regression Analyzer",
         "Computes exact deltas across pre/post JSON snapshots. Identifies missing objects, state flips (available -> offline), and pool member count drops. Generates outputs/diff_report_<host>.md."),

        ("src/f5upgrade/flow.py", "Standby-First Upgrade Orchestrator",
         "Coordinates the end-to-end upgrade flow. Validates that the targeted node is STANDBY, skips destructive steps on ACTIVE nodes, checks image presence, triggers installation, polls volume readiness, reboots, and validates post-boot health."),

        ("src/f5upgrade/execution.py", "Low-Level TMOS Software & Boot Operators",
         "Executes 'tmsh install sys software image', polls /mgmt/tm/sys/software/volume, executes 'tmsh reboot volume', and traps expected socket disconnects during appliance reboot."),

        ("src/f5upgrade/ha_recovery.py", "Post-Upgrade HA & Synchronization Validator",
         "Validates trust-domain health, verifies cm sync-status is 'In Sync', and confirms traffic-group-1 has exactly one active and one standby member.")
    ]

    for mod_path, mod_title, mod_desc in modules:
        p_m = doc.add_paragraph()
        r_mp = p_m.add_run(f"• {mod_path} — {mod_title}\n")
        r_mp.bold = True
        r_mp.font.color.rgb = RGBColor(0x00, 0x33, 0x66)
        r_md = p_m.add_run(mod_desc)
        r_md.font.size = Pt(10)
        p_m.paragraph_format.space_after = Pt(6)

    # Save document
    out_dir = Path("/Users/v.gatla/Downloads/Upgrade/docs")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "F5_17_to_21_Upgrade_Architecture_and_Script_Guide.docx"
    doc.save(str(out_file))
    print(f"Successfully generated Word document: {out_file}")


if __name__ == "__main__":
    create_document()
