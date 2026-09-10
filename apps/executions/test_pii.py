"""Tests for personal-data detection in prompt text.

Weighted towards the two ways this feature could do harm rather than towards
detection coverage: injecting markup into the monitor, and crying wolf so often
on ordinary business text that operators learn to ignore the highlight.
"""

from __future__ import annotations

from django.test import TestCase

from apps.executions.pii import highlight, scan, summarise


def kinds(text):
    return [finding.kind for finding in scan(text)]


class DetectionTests(TestCase):
    def test_email(self):
        self.assertEqual(kinds("write to j.okafor@example.com today"), ["email"])

    def test_a_valid_card_number(self):
        self.assertEqual(kinds("card 4111 1111 1111 1111 was charged"), ["card"])

    def test_a_digit_run_that_fails_the_checksum_is_not_a_card(self):
        # Long reference numbers are everywhere in this domain; without the
        # checksum every one of them would light up as a card.
        self.assertNotIn("card", kinds("reference 1234 5678 9012 3456"))

    def test_national_insurance_number(self):
        self.assertEqual(kinds("NI JM 12 34 56 C on file"), ["ni"])

    def test_prefixes_that_are_never_issued_are_rejected(self):
        # QQ is the prefix HMRC uses in its own examples precisely because no
        # real number starts with it.
        self.assertEqual(kinds("NI QQ 12 34 56 C on file"), [])

    def test_social_security_number(self):
        self.assertEqual(kinds("SSN 123-45-6789"), ["ssn"])

    def test_postcode(self):
        self.assertIn("postcode", kinds("posted to Bristol BS1 4QX yesterday"))

    def test_telephone_number(self):
        self.assertIn("phone", kinds("called on 07700 900461 this morning"))

    def test_ip_address(self):
        self.assertEqual(kinds("request from 192.168.4.22"), ["ip"])

    def test_an_impossible_octet_is_not_an_ip(self):
        self.assertEqual(kinds("version 999.1.1.1"), [])

    def test_several_kinds_are_reported_in_reading_order(self):
        text = "email a@b.co, call 07700 900461, card 4111 1111 1111 1111"
        self.assertEqual(kinds(text), ["email", "phone", "card"])


class FalsePositiveTests(TestCase):
    """Ordinary business text must stay quiet, or the signal is worthless."""

    CLEAN = [
        "Summarise the outstanding underwriting exceptions on submission SUB-40182.",
        "The increase is driven by a 9% adjustment following the 2025 claim.",
        "Reconciled 4,182 of 4,186 entries with a variance of £0.01.",
        "Total exposure is £14.2m across property, liability and motor fleet.",
        "Clause 14.3 permits sub-processing subject to prior written notice.",
        "Recommend reversing the duplicate settlement of £2,140.00.",
        "Policy POL-88213 renews on 1 October.",
    ]

    def test_business_prose_produces_no_findings(self):
        for text in self.CLEAN:
            with self.subTest(text=text):
                self.assertEqual(scan(text), [], f"false positive in: {text}")


class HighlightTests(TestCase):
    def test_matches_are_wrapped_and_labelled(self):
        html = highlight("write to a@b.com")
        self.assertIn('<mark class="ct-pii" data-pii="email"', html)
        self.assertIn("Email address", html)
        self.assertIn("a@b.com</mark>", html)

    def test_text_with_no_findings_is_still_escaped(self):
        self.assertEqual(highlight("1 < 2 & 3 > 2"), "1 &lt; 2 &amp; 3 &gt; 2")

    def test_markup_in_engine_text_cannot_execute(self):
        # The text comes from an external engine over an authenticated API;
        # the monitor renders it into a page an administrator is watching.
        html = highlight("<script>alert('x')</script> mail a@b.com")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn('<mark class="ct-pii"', html)

    def test_markup_inside_a_match_is_escaped_too(self):
        html = highlight('a"onmouseover="alert(1)@b.com')
        self.assertNotIn('onmouseover="alert(1)"', html)
        self.assertIn("&quot;", html)

    def test_empty_text_is_safe_and_empty(self):
        self.assertEqual(highlight(""), "")


class OverlapTests(TestCase):
    def test_an_email_containing_digits_is_not_also_reported_as_a_card(self):
        findings = scan("4111111111111111@example.com")
        self.assertEqual([finding.kind for finding in findings], ["email"])


class SummaryTests(TestCase):
    def test_counts_are_grouped_by_kind(self):
        summary = summarise(scan("a@b.com and c@d.com and 192.168.0.1"))
        self.assertEqual(
            summary,
            [
                {"key": "email", "label": "Email address", "count": 2},
                {"key": "ip", "label": "IP address", "count": 1},
            ],
        )

    def test_nothing_found_is_an_empty_summary(self):
        self.assertEqual(summarise(scan("nothing personal here")), [])
