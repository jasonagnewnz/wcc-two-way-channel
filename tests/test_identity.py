"""Tests for auth cards, delegation, spam limits and earned trust.

This is the part of the prototype where a bug is a security bug rather than a
cosmetic one, so the tests are about the boundaries: what a card cannot do,
what a revoked card cannot do, and what automation is structurally prevented
from granting however the heuristic is tuned.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.chat import ChatService  # noqa: E402
from core.identity import (  # noqa: E402
    AUTO_PROMOTE_MAX_ROLE, ROLES, CardStore, can, can_issue, code_looks_valid,
    hash_code, new_code, normalise, permissions_for, role_rank,
)
from core.moderation import (  # noqa: E402
    ContentChallenge, RateLimited, RateLimiter, auto_promote, challenge,
    score_author,
)
from core.reports import ReportService  # noqa: E402
from core.store import SignalStore  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = SignalStore(root / "signals.jsonl")
        self.cards = CardStore(root / "cards.jsonl")
        self.chat = ChatService(self.store)
        self.reports = ReportService(self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestCodeFormat(Base):
    """A code is read off paper by a stressed person in bad light."""

    def test_codes_are_well_formed_and_validate(self):
        for _ in range(200):
            code = new_code()
            self.assertTrue(code.startswith("WCC-"))
            self.assertTrue(code_looks_valid(code), code)

    def test_ambiguous_characters_never_appear(self):
        # No O/0, no I/1/L — the pairs people mis-transcribe.
        for _ in range(200):
            body = normalise(new_code())
            for bad in "O0I1L":
                self.assertNotIn(bad, body)

    def test_a_single_typo_is_caught_by_the_checksum(self):
        code = new_code()
        body = normalise(code)
        caught = 0
        for i, ch in enumerate(body[:-1]):
            other = "B" if ch != "B" else "C"
            if not code_looks_valid(body[:i] + other + body[i + 1:]):
                caught += 1
        self.assertEqual(caught, len(body) - 1)

    def test_formatting_is_forgiving(self):
        code = new_code()
        for variant in (code.lower(), code.replace("-", ""), f"  {code}  ",
                        code.replace("-", " ")):
            self.assertTrue(code_looks_valid(variant), variant)

    def test_codes_are_unique(self):
        self.assertEqual(len({new_code() for _ in range(3000)}), 3000)


class TestCardLifecycle(Base):
    def test_issue_then_redeem_gives_the_cards_role(self):
        code, card = self.cards.issue(role="official", holder="Ops Officer")
        token, _ = self.cards.redeem(code)
        session = self.cards.resolve(token)
        self.assertEqual(session["role"], "official")
        self.assertEqual(session["holder"], "Ops Officer")
        self.assertIn("post.agency", session["permissions"])

    def test_the_plaintext_code_is_never_stored(self):
        code, card = self.cards.issue(role="official", holder="Ops")
        raw = Path(self.cards.path).read_text(encoding="utf-8")
        self.assertNotIn(normalise(code), raw)
        self.assertIn(hash_code(code), raw)

    def test_listing_cards_never_exposes_the_hash(self):
        self.cards.issue(role="official", holder="Ops")
        for card in self.cards.cards():
            self.assertNotIn("code_hash", card)

    def test_revoking_kills_the_card_and_its_live_session(self):
        code, card = self.cards.issue(role="official", holder="Ops")
        token, _ = self.cards.redeem(code)
        self.assertIsNotNone(self.cards.resolve(token))

        self.cards.revoke(card["card_id"])
        self.assertIsNone(self.cards.resolve(token))
        with self.assertRaises(ValueError):
            self.cards.redeem(code)

    def test_an_unknown_code_is_refused(self):
        with self.assertRaises(ValueError):
            self.cards.redeem(new_code())

    def test_a_malformed_code_is_refused(self):
        with self.assertRaises(ValueError):
            self.cards.redeem("WCC-NOPE")

    def test_repeated_wrong_codes_are_throttled(self):
        for _ in range(10):
            with self.assertRaises(ValueError):
                self.cards.redeem(new_code(), client="1.2.3.4")
        # Eleventh attempt is refused as throttling, not as a bad code — so a
        # brute-force attempt becomes visible instead of silent.
        with self.assertRaises(PermissionError):
            self.cards.redeem(new_code(), client="1.2.3.4")

    def test_throttling_is_per_client(self):
        for _ in range(10):
            with self.assertRaises(ValueError):
                self.cards.redeem(new_code(), client="1.2.3.4")
        code, _ = self.cards.issue(role="verified", holder="Someone else")
        token, _ = self.cards.redeem(code, client="5.6.7.8")
        self.assertIsNotNone(self.cards.resolve(token))

    def test_cards_survive_a_restart(self):
        code, card = self.cards.issue(role="hub-lead", holder="Aro Hub")
        reopened = CardStore(self.cards.path)
        token, _ = reopened.redeem(code)
        self.assertEqual(reopened.resolve(token)["role"], "hub-lead")

    def test_revocation_survives_a_restart(self):
        code, card = self.cards.issue(role="official", holder="Ops")
        self.cards.revoke(card["card_id"])
        reopened = CardStore(self.cards.path)
        with self.assertRaises(ValueError):
            reopened.redeem(code)

    def test_an_unknown_role_cannot_be_issued(self):
        with self.assertRaises(ValueError):
            self.cards.issue(role="mayor", holder="Nope")


class TestDelegation(Base):
    """Cards mint cards, but only downward."""

    def test_each_role_can_issue_only_up_to_its_ceiling(self):
        self.assertTrue(can_issue("coordinator", "official"))
        self.assertFalse(can_issue("coordinator", "coordinator"))
        self.assertTrue(can_issue("official", "hub-lead"))
        self.assertFalse(can_issue("official", "official"))
        self.assertTrue(can_issue("hub-lead", "moderator"))
        self.assertFalse(can_issue("hub-lead", "hub-lead"))

    def test_roles_without_the_permission_cannot_issue_at_all(self):
        for role in ("resident", "verified", "moderator"):
            self.assertFalse(can(role, "card.issue"))
            for target in ROLES:
                self.assertFalse(can_issue(role, target))

    def test_no_role_can_issue_a_peer_or_higher(self):
        # The containment property: a leaked card cannot manufacture its own
        # replacement, so a chain of delegation strictly loses privilege.
        for role in ROLES:
            for target in ROLES:
                if can_issue(role, target):
                    self.assertLess(role_rank(target), role_rank(role))


class TestAutomationCeiling(Base):
    """What a bot is structurally prevented from granting."""

    def test_automation_cannot_grant_a_role_that_issues_cards(self):
        self.assertNotIn("card.issue", permissions_for(AUTO_PROMOTE_MAX_ROLE))

    def test_automation_cannot_grant_official_powers(self):
        granted = permissions_for(AUTO_PROMOTE_MAX_ROLE)
        for forbidden in ("post.agency", "banner.publish", "card.issue"):
            self.assertNotIn(forbidden, granted)

    def test_auto_promote_refuses_to_run_if_the_ceiling_is_unsafe(self):
        import core.moderation as moderation
        original = moderation.AUTO_PROMOTE_MAX_ROLE
        try:
            moderation.AUTO_PROMOTE_MAX_ROLE = "coordinator"
            with self.assertRaises(AssertionError):
                auto_promote(self.store, self.cards)
        finally:
            moderation.AUTO_PROMOTE_MAX_ROLE = original


class TestTrustScoring(Base):
    def _post(self, author, body, channel="wellington"):
        return self.chat.post(channel_id=channel, body=body, author_name=author,
                              author_id=author, author_role="resident")

    def test_an_unknown_author_scores_nothing(self):
        result = score_author(self.store, "nobody")
        self.assertEqual(result["score"], 0)
        self.assertFalse(result["eligible"])

    def test_a_flagged_message_rules_the_author_out_entirely(self):
        message = self._post("anon-x", "A perfectly reasonable long message about flooding here.")
        for i in range(6):
            self._post("anon-x", f"Another substantive message about conditions, number {i}.")
        before = score_author(self.store, "anon-x")["score"]
        self.assertGreater(before, 0)

        self.chat.flag(message["id"], reason="Off-topic.")
        after = score_author(self.store, "anon-x")
        self.assertEqual(after["score"], 0)
        self.assertFalse(after["eligible"])
        self.assertIn("flagged", after["blocked_by"])

    def test_volume_alone_cannot_reach_the_threshold(self):
        # The property that matters: the one input a spammer controls is
        # capped below the bar, so posting a lot is never enough on its own.
        for i in range(200):
            self._post("anon-spam", f"Message number {i} with enough words to pass the challenge.")
        result = score_author(self.store, "anon-spam")
        self.assertFalse(result["eligible"])

    def test_a_report_acted_on_by_an_official_is_the_strongest_input(self):
        report = self.reports.submit_report(
            title="Water over the road", description="Getting deeper by the minute.",
            issue_type="flooding", lat=-41.24, lng=174.81, author_id="anon-good")
        self._post("anon-good", "Water is over the kerb outside the yard now, getting worse.")

        before = score_author(self.store, "anon-good")["score"]
        self.reports.set_status(report["id"], "reviewing", note="Checking.")
        after = score_author(self.store, "anon-good")["score"]
        self.assertGreater(after, before)

    def test_scores_come_with_their_reasons(self):
        self._post("anon-y", "A message with quite a lot of substance in it, over eighty characters long.")
        result = score_author(self.store, "anon-y")
        self.assertTrue(result["reasons"])
        for reason in result["reasons"]:
            self.assertIn("what", reason)
            self.assertIn("points", reason)


class TestAutoPromotion(Base):
    def _make_eligible(self, author="anon-star"):
        for channel in ("wellington", "karori", "newtown"):
            self._post_long(author, channel)
        for _ in range(2):
            report = self.reports.submit_report(
                title="Something worth acting on", description="Detail here.",
                issue_type="flooding", lat=-41.24, lng=174.81, author_id=author)
            self.reports.set_status(report["id"], "reviewing", note="Checking.")
        return author

    def _post_long(self, author, channel):
        self.chat.post(channel_id=channel,
                       body="A properly substantive message about local conditions, "
                            "well over eighty characters so it counts.",
                       author_name=author, author_id=author, author_role="resident")

    def test_an_eligible_author_is_promoted_to_the_capped_role(self):
        author = self._make_eligible()
        promoted = auto_promote(self.store, self.cards)
        self.assertEqual(len(promoted), 1)
        self.assertEqual(promoted[0]["author_id"], author)

        card = [c for c in self.cards.cards() if c["subject"] == author][0]
        self.assertEqual(card["role"], AUTO_PROMOTE_MAX_ROLE)
        self.assertEqual(card["issued_by"], "trust-bot")

    def test_promotion_is_idempotent(self):
        self._make_eligible()
        granted: dict[str, str] = {}
        self.assertEqual(len(auto_promote(self.store, self.cards, granted=granted)), 1)
        self.assertEqual(len(auto_promote(self.store, self.cards, granted=granted)), 0)

    def test_promotion_is_idempotent_with_no_shared_state(self):
        # An empty dict is falsy; `granted or {}` used to swap it for a
        # throwaway, so every run re-promoted the same people.
        self._make_eligible()
        self.assertEqual(len(auto_promote(self.store, self.cards)), 1)
        self.assertEqual(len(auto_promote(self.store, self.cards)), 0)

    def test_promotion_is_recorded_in_the_audit_log_with_its_reason(self):
        self._make_eligible()
        auto_promote(self.store, self.cards)
        events = self.store.fetch(limit=0, signal_type="card-event")
        promotions = [e for e in events if (e.get("raw") or {}).get("action") == "auto-promoted"]
        self.assertEqual(len(promotions), 1)
        raw = promotions[0]["raw"]
        self.assertEqual(raw["role"], AUTO_PROMOTE_MAX_ROLE)
        self.assertIn("score", raw)
        self.assertTrue(raw["reasons"])

    def test_nobody_ineligible_is_promoted(self):
        self.chat.post(channel_id="wellington", body="One short-ish message only here.",
                       author_name="quiet", author_id="anon-quiet", author_role="resident")
        self.assertEqual(auto_promote(self.store, self.cards), [])


class TestContentChallenge(Base):
    def test_a_one_word_cry_for_help_is_challenged_not_rejected(self):
        with self.assertRaises(ContentChallenge) as ctx:
            challenge("help")
        self.assertIn("what is happening", str(ctx.exception).lower())

    def test_filler_only_messages_are_challenged(self):
        with self.assertRaises(ContentChallenge):
            challenge("please someone help urgent now anyone")

    def test_shouting_is_challenged(self):
        with self.assertRaises(ContentChallenge) as ctx:
            challenge("WATER EVERYWHERE ON THE ROAD RIGHT NOW SOMEBODY COME")
        self.assertIn("capitals", str(ctx.exception))

    def test_a_useful_message_passes(self):
        challenge("Water over the road on Hutt Road near the Ngauranga onramp, ankle deep.")

    def test_link_spam_from_a_resident_is_challenged(self):
        with self.assertRaises(ContentChallenge):
            challenge("look http://a.com http://b.com http://c.com at these", role="resident")

    def test_the_same_rules_apply_to_officials(self):
        # A one-word message is just as useless from an official.
        with self.assertRaises(ContentChallenge):
            challenge("help", role="official")


class TestRateLimits(Base):
    def test_a_resident_is_limited_per_minute(self):
        limiter = RateLimiter()
        for _ in range(5):
            limiter.check("anon-1", "resident")
            limiter.record("anon-1", "unique message " + str(_))
        with self.assertRaises(RateLimited) as ctx:
            limiter.check("anon-1", "resident")
        self.assertGreater(ctx.exception.retry_after, 0)

    def test_officials_get_more_headroom(self):
        limiter = RateLimiter()
        for i in range(30):
            limiter.check("card:1", "official")
            limiter.record("card:1", f"message {i}")
        limiter.check("card:1", "official")

    def test_limits_are_per_author_not_shared(self):
        # A hub is one building sharing one connection; limiting them as one
        # would silence a hub during the event it exists for.
        limiter = RateLimiter()
        for i in range(5):
            limiter.check("anon-a", "resident")
            limiter.record("anon-a", f"m{i}")
        limiter.check("anon-b", "resident")

    def test_duplicates_are_refused(self):
        limiter = RateLimiter()
        limiter.record("anon-1", "The exact same message text.")
        with self.assertRaises(ContentChallenge):
            limiter.check_duplicate("anon-1", "the exact   same MESSAGE text.  ")


if __name__ == "__main__":
    unittest.main(verbosity=2)
