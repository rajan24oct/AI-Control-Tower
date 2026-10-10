import json
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.roles import Role
from apps.assistant.answers import GUIDED, AssistantService, decision_rights_answer, framework_terms, glossary_notes
from apps.assistant.knowledge import KnowledgeBase, extract, knowledge, render, tokenize
from apps.organizations.models import Organization, OrganizationMembership

User = get_user_model()

SAMPLE = """# Sample guide

Intro text that mentions gates and tiers and everything else in passing.

## Contents

1. [Tiers](#tiers)

## Tiers

Each system is scored and given a tier from 1 to 4. The tier decides which controls apply.

| Tier | Meaning |
|---|---|
| 4 | Highest risk |

## Deployment gate

Gate G3 authorizes a deployment. See [Tiers](#tiers) and <https://example.org/>.

```
## not a heading
```
"""


class KnowledgeTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        Path(self.tmp.name, "guide.md").write_text(SAMPLE, encoding="utf-8")
        self.kb = KnowledgeBase(documents=[("sample", "Sample", "guide.md")], base_dir=self.tmp.name)
        self.kb.ensure_loaded()

    def tearDown(self):
        self.tmp.cleanup()

    def test_sections_split_at_headings_and_skip_contents(self):
        titles = [section.title for section in self.kb.sections]
        self.assertEqual(titles, ["Sample guide", "Tiers", "Deployment gate"])
        self.assertIn("## not a heading", self.kb.by_id["sample-deployment-gate"].body)

    def test_gate_phrasings_normalise_to_codes(self):
        self.assertIn("g3", tokenize("What does gate 3 need?"))
        self.assertIn("s7", tokenize("stage 7"))
        self.assertEqual(tokenize("authorizations"), tokenize("authorization"))

    def test_search_ranks_the_section_about_the_question(self):
        results = self.kb.search("how is a tier given to a system")
        self.assertEqual(results[0][1].title, "Tiers")
        self.assertEqual(self.kb.search("deployment g3")[0][1].title, "Deployment gate")

    def test_extract_keeps_the_matching_blocks(self):
        passage = extract(self.kb.by_id["sample-tiers"], "tier meaning")
        self.assertIn("| 4 | Highest risk |", passage)

    def test_the_index_reloads_when_a_document_changes(self):
        import os
        import time

        path = Path(self.tmp.name, "guide.md")
        path.write_text(SAMPLE + "\n## Exceptions\n\nAn exception sets one policy aside for a while.\n", encoding="utf-8")
        os.utime(path, (time.time() + 5, time.time() + 5))
        self.kb.ensure_loaded()
        self.assertIn("Exceptions", [s.title for s in self.kb.sections])


class RenderTests(TestCase):
    def test_markup_is_escaped_before_anything_is_interpreted(self):
        html = render("Watch out <script>alert(1)</script> and **bold**")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("<strong>bold</strong>", html)

    def test_tables_lists_and_external_links(self):
        html = render("| A | B |\n|---|---|\n| 1 | `two` |\n\n- one\n- two\n\nSee <https://example.org/>.")
        self.assertIn("<td><code>two</code></td>", html)
        self.assertIn("<ul><li>one</li><li>two</li></ul>", html)
        self.assertIn('href="https://example.org/" target="_blank" rel="noopener"', html)

    def test_a_link_with_a_script_scheme_is_dropped(self):
        html = render("[click](javascript:alert(1))")
        self.assertNotIn("javascript:", html)
        self.assertIn("click", html)


@override_settings(ASSISTANT_PROVIDER="documentation")
class AnswerTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Assist Org")

    def test_decision_rights_come_from_the_enforced_rules(self):
        answer = decision_rights_answer("Who must sign Gate G3 for a Tier 4 agent?")
        self.assertEqual(answer.mode, "rules")
        self.assertIn("AI Governance Body, then Enterprise Governance Sponsor", answer.html)
        self.assertNotIn("Tier 1", answer.html)

    def test_decision_rights_need_a_question_about_signing(self):
        self.assertIsNone(decision_rights_answer("What is Gate G3?"))

    def test_an_unimplemented_code_is_not_guessed_at(self):
        notes, _, _ = glossary_notes("what is S4?")
        self.assertIn("does not implement", notes[0])

    def test_a_known_code_gets_its_definition_and_page(self):
        notes, links, sections = glossary_notes("Explain M17")
        self.assertIn("Exceptions", notes[0])
        self.assertEqual(links[0]["url"], reverse("lifecycle:exceptions"))

    def test_the_estate_status_is_read_live(self):
        answer = AssistantService.answer("Where do my governance gates stand?", organization=self.organization)
        self.assertEqual(answer.mode, "live")
        self.assertIn("Assist Org", answer.html)
        self.assertIn("0 in force", answer.html)

    def test_documentation_answers_cite_their_sources(self):
        answer = AssistantService.answer("How is a risk tier calculated?", organization=self.organization)
        self.assertEqual(answer.mode, "documentation")
        self.assertEqual(answer.sources[0]["title"], "Risk dimensions D1 to D5 and the four tiers")
        self.assertIn("Control deficit", answer.html)

    def test_off_topic_questions_say_so(self):
        answer = AssistantService.answer("What is the weather in Paris?", organization=self.organization)
        self.assertIn("couldn't find that", answer.html)
        self.assertTrue(answer.suggestions)

    @override_settings(ASSISTANT_AGENT="no-such-agent")
    def test_a_missing_assistant_agent_falls_back_to_the_documentation(self):
        answer = AssistantService.answer("What does the Control Plane compare?", organization=self.organization)
        self.assertEqual(answer.mode, "documentation")


@override_settings(ASSISTANT_PROVIDER="documentation")
class FrameworkTests(TestCase):
    def test_every_irgf_code_is_defined_from_the_framework_reference(self):
        terms = framework_terms()
        expected = {f"S{n}" for n in range(11)} | {f"G{n}" for n in range(1, 6)} | {f"D{n}" for n in range(1, 6)}
        expected |= {f"M{n}" for n in range(1, 21)} | {f"L0{n}" for n in range(1, 9)}
        self.assertEqual(set(terms), expected)
        self.assertEqual(terms["G2"][0], "Architecture and data readiness")

    def test_a_code_gets_the_framework_definition_and_the_platform_note(self):
        notes, links, _ = glossary_notes("What is S2?")
        self.assertIn("S2 · Risk classification** (IRGF)", notes[0])
        self.assertIn("In the Control Tower", notes[0])
        notes, _, _ = glossary_notes("What is G5?")
        self.assertIn("does not implement this element yet", notes[0])

    def test_a_range_is_not_read_as_two_codes(self):
        notes, _, _ = glossary_notes("What are the dimensions D1 to D5?")
        self.assertEqual(notes, [])

    def test_every_guided_question_gets_an_answer(self):
        organization = Organization.objects.create(name="Guided Org")
        for _, questions in GUIDED:
            for question in questions:
                with self.subTest(question):
                    answer = AssistantService.answer(question, organization=organization)
                    self.assertNotIn("couldn't find", answer.html)

    def test_the_safety_floor_is_tier_3(self):
        answer = AssistantService.answer("What is the safety-override floor?")
        self.assertIn("Tier 3 at least", answer.html)


@override_settings(ASSISTANT_PROVIDER="documentation")
class AssistantViewTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Assist View Org")
        self.user = User.objects.create_user(email="asker@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.user, role=Role.VIEWER, is_default=True
        )

    def ask(self, question):
        return self.client.post(reverse("assistant:ask"), json.dumps({"question": question}),
                                content_type="application/json")

    def test_asking_needs_a_session(self):
        self.assertEqual(self.ask("What is G3?").status_code, 403)

    def test_a_question_gets_a_json_answer(self):
        self.client.force_login(self.user)
        data = self.ask("What happens to a major change at Gate G4?").json()
        self.assertIn("html", data)
        self.assertTrue(data["sources"])
        self.assertEqual(data["links"][0]["url"], reverse("lifecycle:changes"))

    def test_an_overlong_question_is_refused(self):
        self.client.force_login(self.user)
        self.assertEqual(self.ask("x" * 700).status_code, 400)

    def test_a_source_section_renders(self):
        self.client.force_login(self.user)
        section = next(s for s in knowledge().sections if s.title == "Exceptions (M17)")
        response = self.client.get(section.url)
        self.assertContains(response, "compensating control")
        self.assertEqual(self.client.get(reverse("assistant:section", args=["no-such-section"])).status_code, 404)

    def test_the_widget_is_on_every_page_for_a_signed_in_user(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("dashboard:home")), "data-assistant")
        self.client.logout()
        self.assertNotContains(self.client.get(reverse("accounts:login")), "data-assistant")

    def test_the_widget_is_the_irgf_agent_with_guided_questions(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("dashboard:home"))
        self.assertContains(response, "IRGF Agent")
        self.assertContains(response, "data-assistant-guide-template")
        for topic, questions in GUIDED:
            self.assertContains(response, questions[0])
