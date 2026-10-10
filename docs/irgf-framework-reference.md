# IRGF framework reference

A short reference to the **Intent-to-Runtime Governance Framework (IRGF) v1.0**, written for the IRGF
Agent and for anyone who wants the framework's vocabulary in one place. It summarizes the framework;
the framework itself, with the full handbook, templates, guidelines and checklists, is at
<https://irgframework.org/>. For how AI Control Tower implements it, see *AI Control Tower and the
IRGF*.

Cite the framework as: Dr. Rajan K Upadhyay (2026). *IRGF: The Intent-to-Runtime Governance
Framework, Version 1.0*. <https://irgframework.org>

## What IRGF governs

IRGF governs an AI system from the intent behind it to its behaviour at runtime. It has eleven
lifecycle stages (S0–S10) grouped under five decision gates (G1–G5); a risk tier (1–4) derived from
five scored dimensions (D1–D5); twenty modules (M1–M20) in seven groups; an eight-link governance
chain for agents and other systems that act; and a Control Plane that keeps comparing what is
deployed with what was approved.

All five gates apply at every tier. The tier changes who decides, how much evidence is needed and how
much can be automated, not the procedure. The handbook is candid that the framework has not yet been
run in a real organization: its thresholds and cadences are reasoned defaults, not calibrated values.

Source: [Handbook overview](https://irgframework.org/docs/index.html) ·
[Running the AI build lifecycle](https://irgframework.org/docs/ch04-running-the-ai-build-lifecycle.html)

## Lifecycle stages S0 to S10

| Stage | Name | What it settles | Feeds |
|---|---|---|---|
| S0 | Opportunity | Whether the problem should be solved with AI at all | G1 |
| S1 | Use-case qualification | Value, feasibility, and whether a data path exists | G1 |
| S2 | Risk classification | The system's risk tier; revisited when change or runtime evidence moves a dimension | G1, G2 |
| S3 | Architecture decision | The architecture, recorded in an ADR and checked against a reference pattern | G2 |
| S4 | Data readiness | Grounding data that is governed, traced and classified | G2 |
| S5 | Build, configure or acquire | Constructing, integrating or procuring the system | — |
| S6 | AI assurance | Evidence that the system performs as intended, as safely as its tier requires | G3 |
| S7 | Deployment authorization | Who authorizes deployment, on what evidence | G3 |
| S8 | Runtime operation and continuous monitoring | That it keeps behaving as intended, stays compliant and stays valuable | — |
| S9 | Change | Whether a change sends the system back to an earlier stage | G4 |
| S10 | Retirement | Decommissioning with the evidence preserved | G5 |

The lifecycle is numbered but not linear. S8 runs alongside everything from deployment to retirement.
S2 is re-entered whenever a change or runtime evidence plausibly moves a risk dimension. S8 can loop
back to S2, S6 or S9, and S9 can route to S2, S3, S6 or S7 depending on how a change is classed.
Skipping S2 for vendor-embedded AI is named as an anti-pattern.

Source: [Running the AI build lifecycle](https://irgframework.org/docs/ch04-running-the-ai-build-lifecycle.html)

## The five gates

| Gate | Name | After | What it decides |
|---|---|---|---|
| G1 | Qualification and provisional risk | S0–S1 | Whether the qualified use case goes ahead, carrying provisional D1–D5 scores |
| G2 | Architecture and data readiness | S3–S4 | Whether the architecture and data are ready; the tier is confirmed or changed here |
| G3 | Deployment authorization | S6–S7 | Whether the system may be deployed, as this configuration, for this owner |
| G4 | Material change | S9 | How a change is classed, what assurance it needs, and whether it is approved |
| G5 | Retirement | S10 | Whether retirement is complete and the evidence is kept |

Every gate ends in one of five outcomes: **approve**; **approve with conditions** (each owned, dated
and tracked); **remediate** (do not proceed; say what would satisfy the gate); **escalate** (this body
cannot decide; name the route); or **reject** (with the reason recorded).

Source: [Gate checklist](https://irgframework.org/docs/checklist-gates.html)

### Gate G1: qualification and provisional risk

Asks for a named need with current and target measures, the non-AI alternative considered, the one
decision the output will support, the people affected, the intended launch autonomy, provisional
D1–D5 scores marked as provisional, a data path with a sensitivity signal, and no premature
architecture or vendor commitment. Rejections are recorded with reasons. At Tier 1 it can be
self-service and approved by rule; Tiers 3–4 need the AI Governance Lead's review.

Source: [Opportunity and qualification, S0, S1 and G1](https://irgframework.org/docs/ch05-opportunity-and-qualification-s0-s1-and-gate-g1.html)

### Gate G2: architecture and data readiness

Asks for a complete architecture decision record (model, hosting, grounding, boundary), the
alternatives rejected, conformance to a reference pattern, stated failure behaviour, full data
lineage and sensitivity, an owner, refresh cadence and maximum staleness for every grounding source,
the risk tier confirmed or changed, an agreed effective-autonomy threshold, due diligence for procured
systems, an authority boundary for agents, and a concentration check at Tiers 3–4. Architects
self-certify at Tiers 1–2 against unmodified patterns; Tiers 3–4 go to the expanded Architecture
Review Board.

Source: [Data readiness and Gate G2](https://irgframework.org/docs/ch09-data-readiness-and-gate-g2-s4.html)

### Gate G3: deployment authorization

Asks for a technical evaluation whose pass criteria were set before it ran, an AI Assurance Summary
at the depth the tier requires (including what was not tested), error rates reported per direction,
a complete control matrix, a human oversight point with a time budget from Tier 2, the authorized
configuration written as data, a machine-readable permission boundary for agents, kill-switch
conditions, agreed monitoring signals and recipients, owned and dated conditions, and stated
re-authorization triggers. A named person, not a function, is accountable. A System Owner approves at
Tier 1; Tier 4 needs the AI Governance Body and the Enterprise Governance Sponsor.

Source: [Deployment authorization, S7 and G3](https://irgframework.org/docs/ch12-deployment-authorization-s7-and-gate-g3.html)

### Gate G4: material change

Asks for the change's class against the change-type table with reasoning, the dimensions it touches
(identified before choosing what to re-test), a delta assurance scope saying what is re-run and what
is not, an explicit yes or no on autonomy change, an updated risk score where a dimension moved, the
post-change tier, supplier-initiated changes recorded even when nothing local changed, a rollback plan,
and the authorized configuration updated on completion. Approval goes to the authority for the
post-change tier; at Tiers 3–4 a second person confirms any claim that a change is cosmetic or minor.

Source: [Change, S9 and G4](https://irgframework.org/docs/ch14-change-s9-and-gate-g4.html)

### Gate G5: retirement

Asks for retention or deletion confirmed against the schedule (including derived stores), access
revoked including machine identities, downstream consumers identified from telemetry, evidence
archived with one retrieval tested, the ability to explain past decisions preserved for the liability
window, a knowledge-preservation note, and residual obligations owned.

Source: [Retirement, S10 and G5](https://irgframework.org/docs/ch15-retirement-s10-and-gate-g5.html)

## Risk dimensions D1 to D5 and the four tiers

Each dimension is scored 1–4 against published anchors:

| Dimension | Name |
|---|---|
| D1 | Decision consequence severity |
| D2 | Autonomy |
| D3 | Reversibility deficit |
| D4 | Exposure and scale |
| D5 | Data sensitivity and model uncertainty |

**Impact** is the average of D1, D4 and D5, rounded up; **control deficit** is the average of D2 and
D3, rounded up. The two axes are read together, so one catastrophic factor cannot be averaged away:

| Impact ↓ · Control deficit → | 1 | 2 | 3 | 4 |
|---|---|---|---|---|
| 4 | Tier 3 | Tier 3 | Tier 4 | Tier 4 |
| 3 | Tier 2 | Tier 2 | Tier 3 | Tier 4 |
| 2 | Tier 1 | Tier 2 | Tier 2 | Tier 3 |
| 1 | Tier 1 | Tier 1 | Tier 2 | Tier 2 |

A **safety-override floor** puts a system at **Tier 3 at least**, whatever the matrix says, when
D1 = 4 (catastrophic if wrong), or when D2 = 4 and D3 = 4 together (fully autonomous and effectively
irreversible).

The tier sets governance intensity, decision authority, evidence depth and whether automation may be
used:

| Tier | Posture | What it means in practice |
|---|---|---|
| 1 | Self-service within patterns | The architect self-certifies G2 against an unmodified pattern; assurance is sampled |
| 2 | Self-service with visibility | As Tier 1, with the ARB and AI Governance Lead notified; fuller assurance; an Agent Card if the system has any tool access |
| 3 | Reviewed | The ARB decides G2 and the AI Governance Body decides G3 and G4; full assurance; annual re-scoring; preventive controls for standing-authority agents |
| 4 | Reviewed and escalated | As Tier 3, plus Enterprise Governance Sponsor sign-off, pre-approved kill-switch conditions, and no automatic exception renewal |

**Effective autonomy** is the autonomy a system really carries, which can exceed its recorded D2 when
human review is only nominal.

Source: [The five dimensions](https://irgframework.org/docs/ch06-risk-classification-the-five-dimensions.html) ·
[Tiering, calibration and gaming](https://irgframework.org/docs/ch07-risk-classification-tiering-calibration-and-gaming.html) ·
[Glossary](https://irgframework.org/docs/ch37-glossary.html)

## The twenty modules M1 to M20

| Module | Name | Group | Purpose |
|---|---|---|---|
| M1 | Core Model | Foundations | One connected record per system across eight entities: Intent, Capability, AI System, Decision, Risk, Control, Outcome, Assurance |
| M2 | Architecture Domains | Foundations | Makes AI an architecture domain alongside business, data, application and technology |
| M3 | Three Views | Foundations | Structural, lifecycle and control views over one metamodel, each fact authored once |
| M4 | Risk Dimensions | Classification | Scores a system on D1–D5 with justifications |
| M5 | Tiering Method | Classification | Combines the dimensions into Tier 1–4, with the safety-override floor |
| M6 | Independent Verification | Classification | Separates scorer from delivery: a countersigned tier, and sampling of Tier 1–2 self-certifications |
| M7 | AI Build Lifecycle | Lifecycles | The eleven stages and five gates, with intensity proportional to tier |
| M8 | Transformation Lifecycle | Lifecycles | Connects transformation initiatives (T0–T7) to architecture without owning their delivery |
| M9 | Architecture Lifecycle | Lifecycles | Turns individual architecture decisions into reusable, ratified patterns |
| M10 | Agent Governance Chain | Autonomy | Constrains what an acting system may do and makes its real authority comparable with its approved authority |
| M11 | Autonomy Change Control | Autonomy | Any expansion of an agent's authority is re-authorized and detectable |
| M12 | Control Plane | Continuous assurance | Keeps establishing whether what is deployed is still what was approved |
| M13 | Drift Taxonomy | Continuous assurance | Classes divergence so it reaches an owner who can act |
| M14 | Policy- and Evidence-as-Code | Continuous assurance | Executable preventive rules, and gate evidence captured during operation |
| M15 | Two Standing Bodies | Operating model | Review capacity for Tier 3–4 work without a second bureaucracy |
| M16 | Decision Rights and RACI | Operating model | Who decides what at each tier |
| M17 | Escalation and Exceptions | Operating model | Recorded, time-boxed routes for cases that do not fit |
| M18 | Primary Records | Evidence | Each governed fact held once, with one owner and one location |
| M19 | Derived Views | Evidence | Status compiled from primary records, never maintained separately |
| M20 | Reference Pattern Library | Evidence | Pre-approved architectures that shorten review for familiar problems |

The modules can be adopted in any order. Points worth knowing: automated corrective action is allowed
at Tiers 1–2 only (M12, M14), and the only exception at Tiers 3–4 is a pre-approved kill-switch
condition; an exception (M17) lasts at most 90 days at any tier, and a Tier 4 exception cannot be
renewed; autonomy grants and increases go to the AI Governance Body at every tier above 1 (M11).

Source: [Module reference: foundations, classification, lifecycles](https://irgframework.org/docs/ch33-module-reference-foundations-classification-lifecycles.html) ·
[Module reference: autonomy, assurance, operating model, evidence](https://irgframework.org/docs/ch34-module-reference-autonomy-assurance-operating-model-evidence.html)

## The agent governance chain

For systems that act, governance follows eight links, each a control point:

| Link | Name | What it requires |
|---|---|---|
| 01 | Agent | The starting point: the agent, its owner and its Agent Card |
| 02 | Identity | A registered machine identity per agent, with least privilege, so actions can be attributed |
| 03 | Permission | The authority boundary in machine-readable form |
| 04 | Tool | An explicit allow-list of tools and APIs, so access cannot widen silently |
| 05 | Policy | Machine-enforced constraints; mandatory for standing-authority agents at Tiers 3–4 |
| 06 | Action | Execution, with a pre-approved kill-switch condition |
| 07 | Verification | A check of each action after it happens, within a maximum audit latency |
| 08 | Audit | An immutable log that feeds the Control Plane |

The **Agent Card** is the primary record of an agent's enforceable authority: its machine identity,
an allow-list (not a deny-list, which fails open) of tools, action limits on value, counterparty, rate
and scope, the reversibility of each action, escalation thresholds, kill-switch conditions (mandatory
and objectively evaluable at Tiers 3–4), the delegation chain in multi-agent systems, and a named human
owner. In a multi-agent system, D2 and D3 are scored against the composite authority the agents can
reach together, not the entry-point agent alone.

Source: [Governing agents](https://irgframework.org/docs/ch16-governing-agents.html)

## Governance bodies and roles

IRGF has two standing bodies, and both serve Tiers 3–4 only:

- The **expanded Architecture Review Board** decides G2 at Tiers 3–4 and ratifies or retires reference
  patterns. AI architecture expertise is a membership requirement.
- The **AI Governance Body** decides G3 and G4 at Tiers 3–4, accepts residual risk, grants or
  increases autonomy, approves kill-switch conditions, and gives concurrence on Tier 3–4 AI funding. It
  should include people who did not build the system under review.

At Tiers 1–2 governance is **federated**: architects self-certify G2 against unmodified patterns,
owners approve G3, and the Control Plane checks those self-certifications continuously. Named roles
include the Enterprise Governance Sponsor, the AI Governance Lead, the Risk and Compliance Lead, the AI
System Owner, the Data Owner and the Security Architect.

Source: [The governance bodies](https://irgframework.org/docs/ch20-the-governance-bodies.html) ·
[Roles, decision rights and RACI](https://irgframework.org/docs/ch21-roles-decision-rights-and-raci.html)

## Where to read more

- The handbook, chapter by chapter: <https://irgframework.org/docs/index.html>
- Practical guide: <https://irgframework.org/docs/practical-guide.html>
- Industry overlays (banking, insurance, healthcare and others): <https://irgframework.org/docs/industry-overlays.html>
- Templates: <https://irgframework.org/docs/templates.html>
- Guidelines: <https://irgframework.org/docs/guidelines.html>
- Checklists: <https://irgframework.org/docs/checklists.html>
- Known limitations: <https://irgframework.org/docs/appendix-a-known-limitations-register.html>
