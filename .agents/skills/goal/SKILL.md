---
name: goal
description: Autonomous goal execution — drives implementation thoroughly until production-ready, fully verified, and complete.
triggers: goal, /goal, production-ready, finish-goal
title: Goal
version: 1.0.0
category: general
author: SSL Wireless
---# Autonomous Goal Execution (Production-Ready)

When this skill is active, execute autonomously and do not stop until the objective is completely achieved, verified, and hardened for production.

## Core Directives

1. **Autonomous Thoroughness**:
   - Do not stop at surface-level implementation or mock code.
   - Implement the complete end-to-end functionality as requested.

2. **Production-Readiness Checklist**:
   - **Type Safety & Validation**: Rigorous argument checks and schema validations.
   - **Error Handling**: Graceful error catching and descriptive logging/diagnostics without leaking secrets.
   - **Tests & Verification**: Write or run verification tests to prove that code changes work.
   - **Edge Cases**: Handle boundary conditions, timeouts, network interruptions, and malformed inputs.
   - **Clean Architecture**: Follow established repository conventions, file structures, and linting standards.

3. **Chained Execution Context**:
   - If paired with an earlier discovery or planning skill (e.g., `/grill-me` or `/plan`), strictly respect the decisions and specifications established in that phase before executing the production build.
