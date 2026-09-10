"""Tests for prompt and completion text on the monitor.

This is the most sensitive thing the platform displays, so the tests lean on
the two controls -- the permission and classification redaction -- and on the
paths where text could escape them: the WebSocket broadcast, which reaches
every watcher of an organization at once, and the poll, which does not.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Perm, Role, role_has_permission
from apps.core.constants import DataClassification
from apps.executions.models import Execution, ExecutionEvent, ExecutionStatusChoices
from apps.executions.prompts import PromptService, max_chars
from apps.executions.tests import ExecutionFixtureMixin
from apps.organizations.models import OrganizationMembership

User = get_user_model()

PROMPT = "Summarise the outstanding underwriting exceptions on SUB-40182."
COMPLETION = "Three exceptions are outstanding. Recommendation: refer to the property desk."


class PromptFixtureMixin(ExecutionFixtureMixin):
    def build_run(self, *, prompt=PROMPT, completion=COMPLETION, status=None):
        self.execution = Execution.objects.create(
            organization=self.organization,
            external_id="run-0001",
            agent=self.agent,
            application=self.application,
            model=self.model,
            provider=self.provider,
            status=status or ExecutionStatusChoices.RUNNING,
            started_at=timezone.now() - timedelta(seconds=30),
        )
        if prompt is not None:
            ExecutionEvent.objects.create(
                organization=self.organization,
                execution=self.execution,
                event_type="PROMPT_CREATED",
                sequence=0,
                timestamp=timezone.now(),
                metadata={"prompt": prompt},
            )
        if completion is not None:
            ExecutionEvent.objects.create(
                organization=self.organization,
                execution=self.execution,
                event_type="MODEL_REQUEST_COMPLETED",
                sequence=1,
                timestamp=timezone.now(),
                metadata={"completion": completion},
            )
        return self.execution

    def member(self, email, role):
        user = User.objects.create_user(email=email, password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=user, role=role, is_default=True
        )
        return user


class PromptPermissionTests(PromptFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.build_run()

    def test_a_developer_sees_the_exchange(self):
        exchange = PromptService.for_execution(
            self.execution,
            viewer=self.member("dev@example.com", Role.AI_DEVELOPER),
            organization=self.organization,
        )
        self.assertEqual(exchange.prompt, PROMPT)
        self.assertEqual(exchange.completion, COMPLETION)
        self.assertFalse(exchange.redacted)

    def test_a_viewer_sees_nothing(self):
        # Viewer is the role for a broad internal audience: operations without
        # content. If this ever starts returning text, that promise is broken.
        exchange = PromptService.for_execution(
            self.execution,
            viewer=self.member("viewer@example.com", Role.VIEWER),
            organization=self.organization,
        )
        self.assertEqual(exchange.prompt, "")
        self.assertEqual(exchange.completion, "")
        self.assertFalse(exchange.has_content)

    def test_viewer_is_the_only_role_without_the_permission(self):
        without = {
            role for role in Role.values if not role_has_permission(role, Perm.PROMPT_VIEW)
        }
        self.assertEqual(without, {Role.VIEWER})

    def test_an_anonymous_caller_sees_nothing(self):
        from django.contrib.auth.models import AnonymousUser

        self.assertFalse(
            PromptService.may_view(AnonymousUser(), self.organization)
        )

    def test_a_member_of_another_organization_sees_nothing(self):
        from apps.organizations.models import Organization

        other = Organization.objects.create(name="Other Org")
        outsider = User.objects.create_user(email="out@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=other, user=outsider, role=Role.ORG_ADMIN, is_default=True
        )
        self.assertFalse(PromptService.may_view(outsider, self.organization))


class PromptRedactionTests(PromptFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()

    def _exchange_for(self, classification):
        self.agent.data_classification = classification
        self.agent.save(update_fields=["data_classification"])
        self.build_run()
        self.execution.refresh_from_db()
        return PromptService.for_execution(
            self.execution,
            viewer=self.member("gov@example.com", Role.GOVERNANCE_MANAGER),
            organization=self.organization,
        )

    def test_confidential_agents_are_redacted_even_for_permitted_viewers(self):
        exchange = self._exchange_for(DataClassification.CONFIDENTIAL)
        self.assertTrue(exchange.redacted)
        self.assertEqual(exchange.prompt, "")
        self.assertEqual(exchange.completion, "")
        self.assertIn("confidential", exchange.notice)

    def test_restricted_agents_are_redacted(self):
        self.assertTrue(self._exchange_for(DataClassification.RESTRICTED).redacted)

    def test_internal_agents_are_shown(self):
        exchange = self._exchange_for(DataClassification.INTERNAL)
        self.assertFalse(exchange.redacted)
        self.assertEqual(exchange.prompt, PROMPT)


class RedactedPIIReportingTests(PromptFixtureMixin, TestCase):
    """A redacted run still reports what it was carrying, not what it said."""

    def setUp(self):
        self.build_estate()
        self.agent.data_classification = DataClassification.RESTRICTED
        self.agent.save(update_fields=["data_classification"])
        self.build_run(prompt="Customer j.okafor@example.com called on 07700 900461.")

    def test_counts_survive_redaction(self):
        exchange = PromptService.for_execution(
            self.execution,
            viewer=self.member("gov@example.com", Role.GOVERNANCE_MANAGER),
            organization=self.organization,
        )
        self.assertTrue(exchange.redacted)
        self.assertTrue(exchange.has_pii)
        self.assertEqual({entry["key"] for entry in exchange.pii}, {"email", "phone"})

    def test_the_text_itself_never_escapes(self):
        exchange = PromptService.for_execution(
            self.execution,
            viewer=self.member("gov@example.com", Role.GOVERNANCE_MANAGER),
            organization=self.organization,
        )
        # The count is the signal; the identifier is not part of it.
        self.assertNotIn("j.okafor@example.com", str(exchange))
        self.assertNotIn("j.okafor@example.com", exchange.pii_summary)

    def test_a_viewer_gets_neither_text_nor_counts(self):
        exchange = PromptService.for_execution(
            self.execution,
            viewer=self.member("viewer@example.com", Role.VIEWER),
            organization=self.organization,
        )
        self.assertFalse(exchange.has_pii)


class PIIHighlightingTests(PromptFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.build_run(prompt="Customer j.okafor@example.com called about POL-88213.")
        self.url = reverse("executions:live_table")

    def test_the_monitor_marks_the_identifier(self):
        self.client.force_login(self.member("dev@example.com", Role.AI_DEVELOPER))
        body = self.client.get(self.url).content.decode()
        self.assertIn('<mark class="ct-pii" data-pii="email"', body)
        self.assertIn("ct-pii-badge", body)

    def test_the_caveat_is_shown_with_the_finding(self):
        # Never let a highlight be read as a guarantee of completeness.
        self.client.force_login(self.member("dev@example.com", Role.AI_DEVELOPER))
        body = self.client.get(self.url).content.decode()
        self.assertIn("not all personal data", body)


class PromptExtractionTests(PromptFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.viewer = self.member("dev@example.com", Role.AI_DEVELOPER)

    def _exchange(self):
        return PromptService.for_execution(
            self.execution, viewer=self.viewer, organization=self.organization
        )

    def test_a_run_with_no_reported_text_is_empty_not_broken(self):
        self.build_run(prompt=None, completion=None)
        exchange = self._exchange()
        self.assertFalse(exchange.has_content)
        self.assertFalse(exchange.redacted)

    def test_long_text_is_truncated_on_a_word_boundary(self):
        self.build_run(prompt="word " * 2000)
        exchange = self._exchange()
        self.assertTrue(exchange.truncated)
        self.assertLessEqual(len(exchange.prompt), max_chars())
        self.assertFalse(exchange.prompt.endswith("wor"))

    def test_a_messages_array_is_rendered_as_a_transcript(self):
        self.build_run(prompt=None, completion=None)
        ExecutionEvent.objects.create(
            organization=self.organization,
            execution=self.execution,
            event_type="PROMPT_CREATED",
            sequence=0,
            timestamp=timezone.now(),
            metadata={
                "messages": [
                    {"role": "system", "content": "You are an underwriting assistant."},
                    {"role": "user", "content": "Can SUB-40182 be bound?"},
                ]
            },
        )
        prompt = self._exchange().prompt
        self.assertIn("system: You are an underwriting assistant.", prompt)
        self.assertIn("user: Can SUB-40182 be bound?", prompt)

    def test_aliases_are_accepted(self):
        self.build_run(prompt=None, completion=None)
        ExecutionEvent.objects.create(
            organization=self.organization,
            execution=self.execution,
            event_type="MODEL_REQUEST_COMPLETED",
            sequence=0,
            timestamp=timezone.now(),
            metadata={"output": "answered"},
        )
        self.assertEqual(self._exchange().completion, "answered")

    def test_the_last_completion_wins(self):
        self.build_run(completion="first")
        ExecutionEvent.objects.create(
            organization=self.organization,
            execution=self.execution,
            event_type="MODEL_REQUEST_COMPLETED",
            sequence=9,
            timestamp=timezone.now(),
            metadata={"completion": "final answer"},
        )
        self.assertEqual(self._exchange().completion, "final answer")

    def _bulk_runs(self, count, offset=0):
        runs = []
        for index in range(offset, offset + count):
            execution = Execution.objects.create(
                organization=self.organization,
                external_id=f"bulk-{index}",
                agent=self.agent,
                status=ExecutionStatusChoices.RUNNING,
                started_at=timezone.now(),
            )
            ExecutionEvent.objects.create(
                organization=self.organization,
                execution=execution,
                event_type="PROMPT_CREATED",
                sequence=0,
                timestamp=timezone.now(),
                metadata={"prompt": PROMPT},
            )
            runs.append(execution)
        return runs

    def test_attaching_does_not_cost_a_query_per_run(self):
        """The monitor re-renders fifty rows on every push.

        Asserting a fixed number would just pin today's permission lookup, so
        assert the property that matters: the cost does not grow with the rows.
        """
        few = self._bulk_runs(5)
        many = self._bulk_runs(20, offset=100)

        with self.assertNumQueries(2):
            PromptService.attach(few, viewer=self.viewer, organization=self.organization)
        with self.assertNumQueries(2):
            attached = PromptService.attach(
                many, viewer=self.viewer, organization=self.organization
            )

        self.assertTrue(all(run.exchange.prompt == PROMPT for run in attached))


class LiveMonitorPromptViewTests(PromptFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.build_run()
        self.url = reverse("executions:live_table")

    def test_the_polled_table_shows_the_exchange_to_a_developer(self):
        self.client.force_login(self.member("dev@example.com", Role.AI_DEVELOPER))
        body = self.client.get(self.url).content.decode()
        self.assertIn(PROMPT, body)
        self.assertIn(COMPLETION, body)

    def test_the_polled_table_withholds_it_from_a_viewer(self):
        self.client.force_login(self.member("viewer@example.com", Role.VIEWER))
        body = self.client.get(self.url).content.decode()
        self.assertNotIn(PROMPT, body)
        self.assertNotIn(COMPLETION, body)
        # And offers no control suggesting there is something to open.
        self.assertNotIn("data-toggle-execution", body)


class BroadcastLeakTests(PromptFixtureMixin, TestCase):
    """A group message reaches every watcher, whatever their role."""

    def setUp(self):
        self.build_estate()
        self.build_run()

    def test_the_broadcast_carries_no_rendered_html(self):
        from unittest.mock import patch

        from apps.executions import broadcast

        sent = {}

        class Layer:
            async def group_send(self, group, message):
                sent["group"] = group
                sent["message"] = message

        with patch.object(broadcast, "get_channel_layer", return_value=Layer()):
            self.assertTrue(broadcast.execution_changed(self.execution))

        self.assertEqual(sent["message"]["type"], "execution.upsert")
        self.assertNotIn("html", sent["message"])
        # Belt and braces: the text must not reach the layer by any key.
        self.assertNotIn(PROMPT, str(sent["message"]))
