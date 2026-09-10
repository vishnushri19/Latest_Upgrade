# Contributing

Thanks for your interest in improving this project.

## Principles

This repository prioritizes:
- Safe-by-default design
- Validation-driven decision gates
- Rollback-first thinking
- Lab-first development (no risky production automation)

## How to Contribute

1. Open an issue describing:
   - the problem
   - expected behavior
   - proposed change

2. Submit a pull request that:
   - keeps changes focused
   - includes clear rationale
   - avoids adding unsafe "one-click upgrade" behavior

## Coding Guidelines

- Keep functions small and testable
- Do not hardcode credentials
- Do not log secrets
- Prefer explicit, readable logic over clever shortcuts

## Scope Control

Early versions intentionally avoid executing destructive actions (install/reboot/failover).
Improvements should strengthen:
- prechecks
- validation
- reporting
- architecture documentation
