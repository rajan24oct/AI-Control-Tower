"""Tests for the WebSocket live monitor (section 25)."""

from decimal import Decimal
from unittest.mock import patch

from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import TestCase, TransactionTestCase, override_settings

from aig_tower.asgi import application
from apps.accounts.roles import Role
from apps.executions.routing import websocket_urlpatterns
from apps.agents.models import AIAgent
from apps.executions.broadcast import execution_changed
from apps.executions.consumers import group_name
from apps.executions.models import Execution, ExecutionStatusChoices
from apps.executions.services import ExecutionService
from apps.models_registry.models import AIModel
from apps.organizations.models import Organization, OrganizationMembership
from apps.providers.models import AIProvider

User = get_user_model()

IN_MEMORY = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}


def build_estate(name="Realtime Org"):
    organization = Organization.objects.create(name=name)
    provider = AIProvider.objects.create(
        organization=organization, name="Mock", adapter_key="mock"
    )
    model = AIModel.objects.create(
        organization=organization, provider=provider,
        model_name="Mock Large", model_id="mock-large",
        input_cost_per_1k=Decimal("0.003"), output_cost_per_1k=Decimal("0.015"),
    )
    agent = AIAgent.objects.create(
        organization=organization, name="Live Agent", agent_id="live-agent",
        provider=provider, primary_model=model,
    )
    return organization, agent


def add_member(organization, email, role=Role.VIEWER):
    user = User.objects.create_user(email=email, password="pw-12345")
    OrganizationMembership.objects.create(
        organization=organization, user=user, role=role, is_default=True
    )
    return user


#: The consumer is exercised through the router alone. Going through
#: `AuthMiddlewareStack` would replace `scope["user"]` with a session lookup,
#: overwriting the identity the test is trying to assert against. The full
#: stack is covered separately in `AsgiStackTests`.
consumer_application = URLRouter(websocket_urlpatterns)


async def open_socket(user, organization_id):
    communicator = WebsocketCommunicator(
        consumer_application, f"/ws/executions/{organization_id}/"
    )
    communicator.scope["user"] = user
    connected, _ = await communicator.connect()
    return communicator, connected


@override_settings(CHANNEL_LAYERS=IN_MEMORY)
class ConsumerAccessTests(TransactionTestCase):
    """Session auth says who you are; the consumer decides what you may watch."""

    def setUp(self):
        self.organization, self.agent = build_estate()
        self.other_org = Organization.objects.create(name="Rival Realtime")
        self.member = add_member(self.organization, "member@example.com")
        self.outsider = add_member(self.other_org, "outsider@example.com")

    async def test_a_member_can_connect(self):
        communicator, connected = await open_socket(self.member, self.organization.pk)
        self.assertTrue(connected)
        await communicator.disconnect()

    async def test_an_anonymous_socket_is_refused(self):
        communicator, connected = await open_socket(AnonymousUser(), self.organization.pk)
        self.assertFalse(connected)
        await communicator.disconnect()

    async def test_a_user_cannot_watch_another_tenant(self):
        """The heart of it: no socket may join a group it does not belong to."""
        communicator, connected = await open_socket(self.outsider, self.organization.pk)
        self.assertFalse(connected)
        await communicator.disconnect()

    async def test_an_unknown_organization_is_refused(self):
        import uuid

        communicator, connected = await open_socket(self.member, uuid.uuid4())
        self.assertFalse(connected)
        await communicator.disconnect()


@override_settings(CHANNEL_LAYERS=IN_MEMORY)
class AsgiStackTests(TransactionTestCase):
    """Covers the middleware the consumer tests deliberately bypass."""

    def setUp(self):
        self.organization, _ = build_estate("Stack Org")

    async def test_a_socket_with_no_session_is_refused(self):
        communicator = WebsocketCommunicator(
            application, f"/ws/executions/{self.organization.pk}/"
        )
        connected, _ = await communicator.connect()

        self.assertFalse(connected)
        await communicator.disconnect()

    async def test_a_foreign_origin_is_refused(self):
        """Origin checking guards against cross-site WebSocket hijacking."""
        communicator = WebsocketCommunicator(
            application,
            f"/ws/executions/{self.organization.pk}/",
            headers=[(b"origin", b"https://evil.example")],
        )
        connected, _ = await communicator.connect()

        self.assertFalse(connected)
        await communicator.disconnect()


@override_settings(CHANNEL_LAYERS=IN_MEMORY)
class ConsumerStreamTests(TransactionTestCase):
    def setUp(self):
        self.organization, self.agent = build_estate("Stream Org")
        self.member = add_member(self.organization, "watcher@example.com")

    @database_sync_to_async
    def make_execution(self, external_id, status=ExecutionStatusChoices.RUNNING):
        return ExecutionService.record(
            organization=self.organization,
            external_id=external_id,
            agent="live-agent",
            status=status,
        )

    @database_sync_to_async
    def finish(self, execution):
        return ExecutionService.complete(execution)

    async def test_connecting_sends_a_snapshot(self):
        await self.make_execution("run-live-1")
        communicator, connected = await open_socket(self.member, self.organization.pk)
        self.assertTrue(connected)

        message = await communicator.receive_json_from(timeout=5)
        self.assertEqual(message["type"], "snapshot")
        self.assertEqual(len(message["rows"]), 1)
        self.assertIn("run-live-1", message["rows"][0])
        await communicator.disconnect()

    async def test_the_snapshot_excludes_finished_runs(self):
        await self.make_execution("done-1", ExecutionStatusChoices.COMPLETED)
        communicator, _ = await open_socket(self.member, self.organization.pk)

        message = await communicator.receive_json_from(timeout=5)
        self.assertEqual(message["rows"], [])
        await communicator.disconnect()

    async def test_a_new_live_run_is_pushed(self):
        communicator, _ = await open_socket(self.member, self.organization.pk)
        await communicator.receive_json_from(timeout=5)  # snapshot

        execution = await self.make_execution("run-live-2")
        message = await communicator.receive_json_from(timeout=5)

        self.assertEqual(message["type"], "upsert")
        self.assertEqual(message["id"], str(execution.pk))
        self.assertIn("run-live-2", message["html"])
        await communicator.disconnect()

    async def test_finishing_a_run_removes_it_from_the_board(self):
        execution = await self.make_execution("run-live-3")
        communicator, _ = await open_socket(self.member, self.organization.pk)
        await communicator.receive_json_from(timeout=5)  # snapshot

        await self.finish(execution)
        message = await communicator.receive_json_from(timeout=5)

        self.assertEqual(message["type"], "remove")
        self.assertEqual(message["id"], str(execution.pk))
        await communicator.disconnect()

    async def test_a_refresh_request_returns_a_snapshot(self):
        communicator, _ = await open_socket(self.member, self.organization.pk)
        await communicator.receive_json_from(timeout=5)

        await communicator.send_json_to({"action": "refresh"})
        message = await communicator.receive_json_from(timeout=5)

        self.assertEqual(message["type"], "snapshot")
        await communicator.disconnect()

    async def test_a_watcher_never_sees_another_tenants_traffic(self):
        other = await database_sync_to_async(Organization.objects.create)(
            name="Quiet Org"
        )
        communicator, _ = await open_socket(self.member, self.organization.pk)
        await communicator.receive_json_from(timeout=5)

        await database_sync_to_async(ExecutionService.record)(
            organization=other,
            external_id="foreign-run",
            status=ExecutionStatusChoices.RUNNING,
        )
        self.assertTrue(await communicator.receive_nothing(timeout=1))
        await communicator.disconnect()


@override_settings(CHANNEL_LAYERS=IN_MEMORY)
class ConsumerPromptVisibilityTests(TransactionTestCase):
    """One broadcast, two watchers, two different rows.

    The push is where prompt text is most likely to escape its permission: a
    group message fans out to everyone watching an organization at once. The
    consumer renders per socket precisely so that fan-out cannot flatten two
    different entitlements into one payload.
    """

    PROMPT = "Reconcile the September ledger against the settlement file."

    def setUp(self):
        self.organization, self.agent = build_estate("Visibility Org")
        self.developer = add_member(self.organization, "dev@example.com", Role.AI_DEVELOPER)
        self.viewer = add_member(self.organization, "viewer@example.com", Role.VIEWER)

    @database_sync_to_async
    def make_run_with_prompt(self, external_id):
        from apps.executions.models import ExecutionEvent

        execution = ExecutionService.record(
            organization=self.organization,
            external_id=external_id,
            agent="live-agent",
            status=ExecutionStatusChoices.RUNNING,
        )
        ExecutionEvent.objects.create(
            organization=self.organization,
            execution=execution,
            event_type="PROMPT_CREATED",
            sequence=0,
            timestamp=execution.started_at,
            metadata={"prompt": self.PROMPT},
        )
        return execution

    async def test_the_snapshot_is_rendered_for_the_socket_that_asked(self):
        await self.make_run_with_prompt("vis-1")

        privileged, _ = await open_socket(self.developer, self.organization.pk)
        message = await privileged.receive_json_from(timeout=5)
        self.assertIn(self.PROMPT, message["rows"][0])
        await privileged.disconnect()

        restricted, _ = await open_socket(self.viewer, self.organization.pk)
        message = await restricted.receive_json_from(timeout=5)
        self.assertNotIn(self.PROMPT, message["rows"][0])
        await restricted.disconnect()

    async def test_one_push_reaches_two_watchers_with_different_content(self):
        privileged, _ = await open_socket(self.developer, self.organization.pk)
        restricted, _ = await open_socket(self.viewer, self.organization.pk)
        await privileged.receive_json_from(timeout=5)  # snapshot
        await restricted.receive_json_from(timeout=5)  # snapshot

        execution = await self.make_run_with_prompt("vis-2")
        await database_sync_to_async(execution_changed)(execution)

        to_developer = await privileged.receive_json_from(timeout=5)
        to_viewer = await restricted.receive_json_from(timeout=5)

        self.assertEqual(to_developer["id"], str(execution.pk))
        self.assertEqual(to_viewer["id"], str(execution.pk))
        self.assertIn("vis-2", to_developer["html"])
        self.assertIn("vis-2", to_viewer["html"])

        self.assertIn(self.PROMPT, to_developer["html"])
        self.assertNotIn(self.PROMPT, to_viewer["html"])

        await privileged.disconnect()
        await restricted.disconnect()


@override_settings(CHANNEL_LAYERS=IN_MEMORY)
class BroadcastTests(TestCase):
    """The broadcaster's job is to be cheap and to never break its caller."""

    def setUp(self):
        self.organization, self.agent = build_estate("Broadcast Org")

    def test_a_live_run_broadcasts(self):
        execution = Execution.objects.create(
            organization=self.organization, external_id="b1",
            status=ExecutionStatusChoices.RUNNING,
        )
        self.assertTrue(execution_changed(execution))

    def test_a_finished_run_broadcasts_a_removal(self):
        execution = Execution.objects.create(
            organization=self.organization, external_id="b2",
            status=ExecutionStatusChoices.COMPLETED,
        )
        self.assertTrue(execution_changed(execution))

    def test_recording_a_finished_run_does_not_broadcast(self):
        """Most telemetry describes runs that were already over."""
        with self.assertNumQueries(0):
            ExecutionService.broadcast(
                Execution(
                    organization=self.organization,
                    external_id="b3",
                    status=ExecutionStatusChoices.COMPLETED,
                ),
                previous_status=None,
            )

    def test_a_run_leaving_the_live_board_still_broadcasts(self):
        execution = Execution.objects.create(
            organization=self.organization, external_id="b4",
            status=ExecutionStatusChoices.COMPLETED,
        )
        layer = get_channel_layer()
        self.assertIsNotNone(layer)
        ExecutionService.broadcast(execution, previous_status=ExecutionStatusChoices.RUNNING)

    def test_a_broken_channel_layer_does_not_break_the_caller(self):
        """An unreachable layer must not fail the telemetry write.

        Patched rather than pointed at a dead Redis: `get_channel_layer()`
        caches its backend process-wide, so a real bad-host layer would leak
        into every test that ran afterwards.
        """
        execution = Execution.objects.create(
            organization=self.organization, external_id="b5",
            status=ExecutionStatusChoices.RUNNING,
        )

        class BrokenLayer:
            async def group_send(self, *args, **kwargs):
                raise RuntimeError("channel layer unavailable")

        with patch(
            "apps.executions.broadcast.get_channel_layer", return_value=BrokenLayer()
        ):
            self.assertFalse(execution_changed(execution))

    def test_no_channel_layer_configured_is_handled(self):
        execution = Execution.objects.create(
            organization=self.organization, external_id="b6",
            status=ExecutionStatusChoices.RUNNING,
        )
        with patch("apps.executions.broadcast.get_channel_layer", return_value=None):
            self.assertFalse(execution_changed(execution))

    def test_group_names_are_per_organization(self):
        self.assertEqual(
            group_name(self.organization.pk), f"executions.{self.organization.pk}"
        )
        self.assertNotEqual(group_name("a"), group_name("b"))
