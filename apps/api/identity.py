"""Which agent a gateway caller may act as (IRGF agent chain link 02).

A gateway request names the agent it acts for. That name used to be taken on
trust: any key allowed to call a gateway could act as any agent in its
organization. IRGF asks that each agent act under a registered identity of its
own, so a service account can be bound to the agents it runs:

  * a bound identity may act only as its own agents, and is taken to act as its
    agent when it is bound to exactly one and names none;
  * an unbound identity keeps working as it always did, for agents up to Tier 2;
  * Tier 3-4 agents answer only to identities bound to them.

People signed in to the Control Tower are not service accounts and are never
bound; their role governs what they may do.
"""

from __future__ import annotations

#: From this IRGF tier, an agent acts only under an identity bound to it.
BOUND_IDENTITY_TIER = 3


class IdentityRefused(Exception):
    code = "identity_not_bound"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def acting_agent(service_account, agent, *, requested: bool):
    """The agent this request acts as, or `IdentityRefused`.

    `agent` is the resolved agent the request named, or None; `requested` says
    whether the request named an agent at all.
    """
    if service_account is None:
        return agent

    bound = set(service_account.agents.values_list("pk", flat=True))

    if agent is None:
        if not bound:
            return None
        if requested:
            raise IdentityRefused(
                f"Service account '{service_account.name}' is not bound to that agent."
            )
        if len(bound) == 1:
            from apps.agents.models import AIAgent

            return AIAgent.objects.filter(pk=next(iter(bound))).first()
        raise IdentityRefused(
            f"Service account '{service_account.name}' acts for {len(bound)} agents; "
            "name the one this request is for."
        )

    if bound:
        if agent.pk not in bound:
            raise IdentityRefused(
                f"Service account '{service_account.name}' is not bound to agent "
                f"'{agent.name}'. Use that agent's own identity, or bind this one to it "
                "(IRGF agent chain link 02)."
            )
        return agent

    tier = getattr(agent, "risk_tier", None) or 0
    if tier >= BOUND_IDENTITY_TIER:
        raise IdentityRefused(
            f"Agent '{agent.name}' is Tier {tier}. IRGF requires a Tier 3-4 agent to act under "
            f"an identity registered to it: bind a service account to '{agent.agent_id}' and "
            "call with that account's key."
        )
    return agent
