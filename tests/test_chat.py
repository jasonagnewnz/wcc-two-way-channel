"""Tests for the message board.

The rules worth testing here are the visibility ones. Everything else on this
surface is presentation, but "who can see this message" is the part that, if
it broke, would leak a private welfare report about a named person onto a
public board — or let a member of the public appear inside an inter-agency
channel during an emergency.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.chat import (  # noqa: E402
    AGENCY_CHANNELS, MESSAGE_TYPE, OFFICIALS, PUBLIC, ChatService, channel_kind,
)
from core.store import SignalStore  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SignalStore(Path(self._tmp.name) / "signals.jsonl")
        self.chat = ChatService(self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def resident(self, channel="wellington", body="Hello", author_id="anon-1",
                 visibility=PUBLIC):
        return self.chat.post(channel_id=channel, body=body, author_name="Mere",
                              author_id=author_id, author_role="resident",
                              visibility=visibility)

    def official(self, channel="wellington", body="Update from Council"):
        return self.chat.post(channel_id=channel, body=body, author_name="Duty Officer",
                              author_id="wcc-1", author_role="official",
                              agency="Wellington City Council")


class TestAgencyChannelsAreClosed(Base):
    """Agency channels are for agencies. The public are not in them."""

    def test_public_cannot_post_into_an_agency_channel(self):
        with self.assertRaises(PermissionError):
            self.chat.post(channel_id="fenz", body="let me in", author_name="X",
                           author_id="anon-1", author_role="resident")

    def test_public_read_of_agency_channels_follows_the_demo_flag(self):
        import core.chat as chat
        self.chat.post(channel_id="fenz", body="Two appliances committed.",
                       author_name="Comms", author_id="fenz-1",
                       author_role="official", agency="Fire and Emergency NZ")

        original = chat.AGENCY_CHANNELS_PUBLIC_READ
        try:
            chat.AGENCY_CHANNELS_PUBLIC_READ = False
            with self.assertRaises(PermissionError):
                self.chat.messages("fenz", viewer="public")

            chat.AGENCY_CHANNELS_PUBLIC_READ = True
            self.assertEqual(len(self.chat.messages("fenz", viewer="public")), 1)
        finally:
            chat.AGENCY_CHANNELS_PUBLIC_READ = original

    def test_public_read_never_relaxes_posting(self):
        # The flag opens reading only. Posting is the control that would let
        # somebody put words in an agency's mouth, and it stays shut.
        import core.chat as chat
        original = chat.AGENCY_CHANNELS_PUBLIC_READ
        try:
            chat.AGENCY_CHANNELS_PUBLIC_READ = True
            with self.assertRaises(PermissionError):
                self.chat.post(channel_id="fenz", body="I am the fire service",
                               author_name="X", author_id="anon-1",
                               author_role="resident")
        finally:
            chat.AGENCY_CHANNELS_PUBLIC_READ = original

    def test_public_read_never_exposes_an_internal_agency_message(self):
        # An agency deliberating internally must stay internal even when the
        # channel becomes readable — otherwise opening the hub for a demo
        # publishes exactly the messages that should never be public.
        import core.chat as chat
        self.chat.post(channel_id="fenz", body="Public operational line.",
                       author_name="Comms", author_id="fenz-1",
                       author_role="official", agency="Fire and Emergency NZ")
        self.chat.post(channel_id="fenz", body="Internal deliberation, not for release.",
                       author_name="Comms", author_id="fenz-1",
                       author_role="official", agency="Fire and Emergency NZ",
                       visibility=OFFICIALS)

        original = chat.AGENCY_CHANNELS_PUBLIC_READ
        try:
            chat.AGENCY_CHANNELS_PUBLIC_READ = True
            seen = self.chat.messages("fenz", viewer="public", author_id="anon-other")
            bodies = [m["body"] for m in seen]
            self.assertIn("Public operational line.", bodies)
            self.assertNotIn("Internal deliberation, not for release.", bodies)
        finally:
            chat.AGENCY_CHANNELS_PUBLIC_READ = original

    def test_officials_can_read_and_post(self):
        self.chat.post(channel_id="fenz", body="Two appliances committed.",
                       author_name="Comms", author_id="fenz-1",
                       author_role="official", agency="Fire and Emergency NZ")
        self.assertEqual(len(self.chat.messages("fenz", viewer="official")), 1)

    def test_agency_channel_listing_follows_the_demo_flag(self):
        import core.chat as chat
        original = chat.AGENCY_CHANNELS_PUBLIC_READ
        try:
            chat.AGENCY_CHANNELS_PUBLIC_READ = False
            self.assertEqual(self.chat.channels(viewer="public")["agency"], [])

            chat.AGENCY_CHANNELS_PUBLIC_READ = True
            listed = self.chat.channels(viewer="public")
            self.assertEqual(len(listed["agency"]), len(AGENCY_CHANNELS))
            # The interface needs to know to hide the composer rather than
            # offer one that will 403.
            self.assertTrue(listed["agency_read_only"])
            self.assertFalse(self.chat.channels(viewer="official")["agency_read_only"])
        finally:
            chat.AGENCY_CHANNELS_PUBLIC_READ = original

        self.assertEqual(len(self.chat.channels(viewer="official")["agency"]),
                         len(AGENCY_CHANNELS))

    def test_channel_kind_classification(self):
        self.assertEqual(channel_kind("fenz"), "agency")
        self.assertEqual(channel_kind("island-bay"), "public")
        self.assertEqual(channel_kind("WLG-K7M2Q"), "thread")


class TestPrivateMessages(Base):
    """'Only officials' has to mean only officials."""

    def test_private_message_is_hidden_from_other_residents(self):
        self.resident(body="An older resident on our street needs help.",
                      author_id="anon-author", visibility=OFFICIALS)
        seen = self.chat.messages("wellington", viewer="public", author_id="anon-other")
        self.assertEqual(seen, [])

    def test_private_message_is_visible_to_officials(self):
        self.resident(visibility=OFFICIALS, author_id="anon-author")
        seen = self.chat.messages("wellington", viewer="official")
        self.assertEqual(len(seen), 1)

    def test_author_still_sees_their_own_private_message(self):
        self.resident(visibility=OFFICIALS, author_id="anon-author")
        seen = self.chat.messages("wellington", viewer="public", author_id="anon-author")
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0]["mine"])

    def test_public_messages_do_not_leak_another_persons_author_id(self):
        self.resident(author_id="anon-author")
        seen = self.chat.messages("wellington", viewer="public", author_id="anon-other")
        self.assertIsNone(seen[0]["author_id"])


class TestFlagging(Base):
    """Moderation has to be visible. Nothing is deleted."""

    def test_flagged_message_leaves_the_public_feed_but_leaves_a_marker(self):
        message = self.resident(body="something off-topic")
        self.chat.flag(message["id"], reason="Off-topic.")

        seen = self.chat.messages("wellington", viewer="public", author_id="anon-other")
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0]["withheld"])
        self.assertNotIn("off-topic", seen[0]["body"].lower().replace("under review", ""))

    def test_officials_still_see_the_content_and_the_reason(self):
        message = self.resident(body="something off-topic")
        self.chat.flag(message["id"], reason="Off-topic for an emergency board.")
        seen = self.chat.messages("wellington", viewer="official")
        self.assertEqual(seen[0]["body"], "something off-topic")
        self.assertTrue(seen[0]["flagged"])
        self.assertEqual(seen[0]["flag_reason"], "Off-topic for an emergency board.")

    def test_flagging_never_removes_the_original_signal(self):
        message = self.resident(body="something off-topic")
        self.chat.flag(message["id"], reason="Off-topic.")
        stored = self.store.get(message["id"])
        self.assertIsNotNone(stored)
        self.assertEqual(stored["description"], "something off-topic")
        self.assertEqual(stored["signal_type"], MESSAGE_TYPE)

    def test_a_flag_can_be_cleared(self):
        message = self.resident(body="actually fine")
        self.chat.flag(message["id"], reason="Mistake.")
        self.chat.flag(message["id"], unflag=True)
        seen = self.chat.messages("wellington", viewer="public", author_id="anon-other")
        self.assertFalse(seen[0]["withheld"])
        self.assertEqual(seen[0]["body"], "actually fine")

    def test_flagging_an_unknown_message_is_refused(self):
        with self.assertRaises(KeyError):
            self.chat.flag("WLG-ZZZZZ", reason="nope")


class TestBanner(Base):
    def test_no_banner_by_default(self):
        self.assertIsNone(self.chat.banner())

    def test_publishing_and_clearing(self):
        self.chat.set_banner(text="Orange rain warning until 8pm.", level="warning")
        banner = self.chat.banner()
        self.assertEqual(banner["level"], "warning")
        self.assertIn("Orange", banner["text"])

        self.chat.set_banner(text="", active=False)
        self.assertIsNone(self.chat.banner())

    def test_clearing_is_recorded_rather_than_deleted(self):
        self.chat.set_banner(text="First message.", level="info")
        self.chat.set_banner(text="", active=False)
        # Both the publish and the clear survive in the log, so afterwards you
        # can say exactly what was displayed and for how long.
        from core.chat import BANNER_TYPE
        entries = self.store.fetch(limit=0, signal_type=BANNER_TYPE)
        self.assertEqual(len(entries), 2)
        self.assertIn("First message.", entries[0]["description"])

    def test_bad_level_is_refused(self):
        with self.assertRaises(ValueError):
            self.chat.set_banner(text="x", level="apocalyptic")

    def test_an_active_banner_needs_text(self):
        with self.assertRaises(ValueError):
            self.chat.set_banner(text="   ", active=True)


class TestPosting(Base):
    def test_empty_message_is_refused(self):
        with self.assertRaises(ValueError):
            self.resident(body="   ")

    def test_bad_visibility_is_refused(self):
        with self.assertRaises(ValueError):
            self.resident(visibility="semi-public")

    def test_long_message_is_truncated_not_rejected(self):
        message = self.resident(body="x" * 5000)
        self.assertEqual(len(message["description"]), 2000)

    def test_official_messages_are_marked_official(self):
        self.official()
        seen = self.chat.messages("wellington", viewer="public")
        self.assertEqual(seen[0]["author_role"], "official")
        self.assertEqual(seen[0]["agency"], "Wellington City Council")
        self.assertEqual(seen[0]["source_type"] if "source_type" in seen[0] else "official",
                         "official")

    def test_messages_are_scoped_to_their_channel(self):
        self.resident(channel="karori", body="Karori message")
        self.resident(channel="miramar", body="Miramar message")
        karori = self.chat.messages("karori", viewer="public")
        self.assertEqual(len(karori), 1)
        self.assertEqual(karori[0]["body"], "Karori message")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestPublicSignalRedaction(Base):
    """/api/signals bypasses every service filter, so the boundary is here.

    This class exists because the leak happened twice. First the endpoint had
    no filter at all and served the private welfare message. Then the filter
    was written per-type, and help requests arrived carrying
    `visibility: officials` and went straight through it. The rules are now
    general, and these tests assert the general property rather than the
    specific types that were leaking at the time.
    """

    def test_anything_marked_officials_only_is_withheld_whatever_its_type(self):
        from core.chat import redact_for_public
        invented = [
            {"id": "1", "signal_type": "chat-message", "raw": {"visibility": "officials"}},
            {"id": "2", "signal_type": "help-request", "raw": {"visibility": "officials"}},
            {"id": "3", "signal_type": "community-resource", "raw": {"visibility": "officials"}},
            # A type that does not exist yet. The point of a general rule is
            # that it covers this one too.
            {"id": "4", "signal_type": "something-invented-later",
             "raw": {"visibility": "officials"}},
            {"id": "5", "signal_type": "community-report", "raw": {"visibility": "public"}},
        ]
        kept = {s["id"] for s in redact_for_public(invented)}
        self.assertEqual(kept, {"5"})

    def test_agency_traffic_is_withheld_from_the_raw_feed_regardless_of_the_flag(self):
        # The demo flag opens the INTERFACE, where the messages arrive with
        # their channel, their agency and a read-only notice around them. The
        # raw feed is for machine consumption by other teams and carries none
        # of that context, so it stays conservative.
        import core.chat as chat
        from core.chat import redact_for_public
        rows = [{"id": "a", "signal_type": "chat-message",
                 "raw": {"channel_kind": "agency", "visibility": "public"}}]
        original = chat.AGENCY_CHANNELS_PUBLIC_READ
        try:
            for flag in (True, False):
                chat.AGENCY_CHANNELS_PUBLIC_READ = flag
                self.assertEqual(redact_for_public(rows), [])
        finally:
            chat.AGENCY_CHANNELS_PUBLIC_READ = original

    def test_unapproved_community_content_is_withheld(self):
        from core.chat import redact_for_public
        rows = [
            {"id": "p", "signal_type": "evidence-photo", "raw": {"state": "pending"}},
            {"id": "r", "signal_type": "evidence-photo", "raw": {"state": "rejected"}},
            {"id": "a", "signal_type": "evidence-photo", "raw": {"state": "approved"}},
        ]
        self.assertEqual({s["id"] for s in redact_for_public(rows)}, {"a"})

    def test_moderation_and_card_events_are_withheld(self):
        from core.chat import redact_for_public
        rows = [{"id": "1", "signal_type": "chat-flag", "raw": {}},
                {"id": "2", "signal_type": "moderation-decision", "raw": {}},
                {"id": "3", "signal_type": "card-event", "raw": {}}]
        self.assertEqual(redact_for_public(rows), [])

    def test_a_contact_detail_is_never_published_even_on_a_public_item(self):
        from core.chat import redact_for_public
        rows = [{"id": "1", "signal_type": "community-resource",
                 "raw": {"visibility": "public", "kind": "water",
                         "contact": "021 555 0100"}}]
        kept = redact_for_public(rows)
        self.assertEqual(len(kept), 1)
        self.assertNotIn("contact", kept[0]["raw"])
        self.assertEqual(kept[0]["raw"]["kind"], "water")   # the rest survives

    def test_redaction_does_not_mutate_the_stored_signal(self):
        from core.chat import redact_for_public
        original = {"id": "1", "signal_type": "community-resource",
                    "raw": {"visibility": "public", "contact": "021 555 0100"}}
        redact_for_public([original])
        # The log is the audit trail; redaction is a view, never an edit.
        self.assertIn("contact", original["raw"])
