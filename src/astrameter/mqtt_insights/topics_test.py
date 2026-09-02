"""The command-topic grammar, from both ends."""

from __future__ import annotations

from .topics import (
    ConsumerCommandTopic,
    DeviceCommandTopic,
    MalformedCommandTopic,
    consumer_command_topic,
    device_command_topic,
    parse_command_topic,
)


def test_consumer_command_topic_round_trips():
    topic = consumer_command_topic("am", "dev1", "aabbcc", "manual_target")
    assert topic == "am/ct002/dev1/consumer/aabbcc/manual_target/set"
    assert parse_command_topic("am", topic) == ConsumerCommandTopic(
        device_id="dev1", consumer_id="aabbcc", field="manual_target"
    )


def test_device_command_topic_round_trips():
    topic = device_command_topic("am", "dev1")
    assert topic == "am/ct002/dev1/set"
    assert parse_command_topic("am", topic) == DeviceCommandTopic(device_id="dev1")


def test_consumer_frame_without_a_field_is_malformed():
    parsed = parse_command_topic("am", "am/ct002/dev1/consumer/aabbcc/set")
    assert isinstance(parsed, MalformedCommandTopic)


def test_foreign_topics_are_not_commands():
    for topic in (
        "am/ct002/dev1/status",
        "am/shelly/dev1/status",
        "other/ct002/dev1/set",
        "hame_energy/HME-4/App/aabbcc/ctrl",
    ):
        assert parse_command_topic("am", topic) is None, topic


def test_ids_spanning_several_topic_levels_are_not_commands():
    # The subscription filters bind each id to a single "+" level, so these
    # never arrive from a broker -- but the grammar has to agree with them.
    for topic in (
        "am/ct002/dev1/other/set",
        "am/ct002/dev1/other/consumer/aabbcc/manual_target/set",
        "am/ct002/dev1/consumer/aabbcc/extra/manual_target/set",
    ):
        assert parse_command_topic("am", topic) is None, topic
