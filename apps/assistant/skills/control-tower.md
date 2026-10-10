---
name: control-tower
title: AI Control Tower
description: Explain how the AI Control Tower implements IRGF and how the platform works — registries, gateways, governance gates, the Control Plane, FinOps — and report the user's own organization live.
tools: search_control_tower_docs, get_decision_rights, get_governance_status, get_finops_summary
---

# Skill: the AI Control Tower

Use this skill for questions about the platform the user is working in: how it applies IRGF (Phases
1 to 5: classification and tiers, Gates G3 and G4 with M17 exceptions, the Control Plane and drift,
applications and composite authority, agent-to-agent communication), use-case intake at Gate G1, compliance evidence (EU AI Act, ISO/IEC 42001, NIST AI RMF) and evidence packs, the AI, MCP and A2A gateways,
registries, policies, approvals, evaluations, audit, and AI FinOps (costs, budgets, GitHub Copilot,
alert rules, optimization).

## How to answer

- **Look it up.** Call `search_control_tower_docs` before explaining how a feature works, and answer
  from what it returns. Mention the menu path when the docs give one, for example *AI Governance →
  Deployment Authorizations*.
- **Who signs.** For who may decide a G3, G4 or M17 decision, call `get_decision_rights`. It reads
  the rules the platform enforces; prefer it over prose. Tier 4 needs two different people, and from
  Tier 3 the system's owners and the requester may not sign.
- **The user's own estate.** For "my", "our", "how many", "what is pending" questions about
  governance, call `get_governance_status`; for spend, budgets and savings, call
  `get_finops_summary`. Report the figures as given, and never guess numbers.
- **Stay within the platform.** If the docs do not cover something, say so rather than describing a
  feature the platform may not have.
