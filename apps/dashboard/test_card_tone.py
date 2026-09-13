"""Control Tower card colour: the worse of risk and availability."""

from django.test import SimpleTestCase

from apps.core.constants import HealthStatus, RiskLevel
from apps.dashboard.landscape import Node


def node(risk, health):
    return Node(id="n", name="Node", url="/", risk_level=risk, health=health)


class CardToneTests(SimpleTestCase):
    def test_a_healthy_card_takes_its_risk_colour(self):
        for risk in (RiskLevel.LOW, RiskLevel.MODERATE, RiskLevel.HIGH, RiskLevel.CRITICAL):
            self.assertEqual(node(risk, HealthStatus.HEALTHY).tone, risk)

    def test_an_unavailable_asset_reads_critical_whatever_its_risk(self):
        self.assertEqual(node(RiskLevel.LOW, HealthStatus.UNHEALTHY).tone, RiskLevel.CRITICAL)

    def test_a_degraded_asset_reads_at_least_moderate(self):
        self.assertEqual(node(RiskLevel.LOW, HealthStatus.DEGRADED).tone, RiskLevel.MODERATE)
        # Degraded does not soften a worse risk rating.
        self.assertEqual(node(RiskLevel.HIGH, HealthStatus.DEGRADED).tone, RiskLevel.HIGH)

    def test_unknown_health_leaves_the_risk_colour_alone(self):
        self.assertEqual(node(RiskLevel.MODERATE, HealthStatus.UNKNOWN).tone, RiskLevel.MODERATE)

    def test_an_unrecognised_risk_value_reads_as_low(self):
        self.assertEqual(node("", HealthStatus.HEALTHY).tone, RiskLevel.LOW)
