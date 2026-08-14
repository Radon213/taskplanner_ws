#!/usr/bin/env python3
from __future__ import annotations

import time
import unittest
from threading import Thread
from typing import TYPE_CHECKING, Any

import rclpy
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from rosbridge_library.internal.subscribers import manager
from rosbridge_library.internal.topics import (
    TopicNotEstablishedException,
    TypeConflictException,
)
from rosbridge_library.util.ros import is_topic_subscribed
from std_msgs.msg import String

if TYPE_CHECKING:
    from rosbridge_library.internal.outgoing_message import OutgoingMessage


class TestSubscriberManager(unittest.TestCase):
    def setUp(self) -> None:
        rclpy.init()
        self.executor = SingleThreadedExecutor()
        self.node = Node("test_subscriber_manager")
        self.executor.add_node(self.node)

        self.exec_thread = Thread(target=self.executor.spin)
        self.exec_thread.start()

    def tearDown(self) -> None:
        self.executor.remove_node(self.node)
        self.node.destroy_node()
        self.executor.shutdown()
        rclpy.shutdown()

    def assert_topic_subscribed(self, topic: str, timeout: float = 1.0) -> None:
        start_time = time.monotonic()
        while not is_topic_subscribed(self.node, topic):
            time.sleep(0.05)
            if time.monotonic() - start_time > timeout:
                self.fail(f"Timed out waiting for topic '{topic}' to be subscribed.")

    def assert_topic_not_subscribed(self, topic: str, timeout: float = 1.0) -> None:
        start_time = time.monotonic()
        while is_topic_subscribed(self.node, topic):
            time.sleep(0.05)
            if time.monotonic() - start_time > timeout:
                self.fail(f"Timed out waiting for topic '{topic}' to be unsubscribed.")

    def test_subscribe(self) -> None:
        """Register a publisher on a clean topic with a good msg type."""
        topic = "/test_subscribe"
        msg_type = "std_msgs/String"
        client = "client_test_subscribe"

        self.assertFalse(topic in manager._subscribers)
        self.assert_topic_not_subscribed(topic)
        manager.subscribe(client, topic, lambda _: None, self.node, msg_type)
        self.assertTrue(topic in manager._subscribers)
        self.assert_topic_subscribed(topic)

        manager.unsubscribe(client, topic)
        self.assertFalse(topic in manager._subscribers)
        self.assert_topic_not_subscribed(topic)

    def test_register_subscriber_multiclient(self) -> None:
        topic = "/test_register_subscriber_multiclient"
        msg_type = "std_msgs/String"
        client1 = "client_test_register_subscriber_multiclient_1"
        client2 = "client_test_register_subscriber_multiclient_2"

        self.assertFalse(topic in manager._subscribers)
        self.assert_topic_not_subscribed(topic)
        manager.subscribe(client1, topic, lambda _: None, self.node, msg_type)
        self.assertTrue(topic in manager._subscribers)
        self.assert_topic_subscribed(topic)

        manager.subscribe(client2, topic, lambda _: None, self.node, msg_type)
        self.assertTrue(topic in manager._subscribers)
        self.assert_topic_subscribed(topic)

        manager.unsubscribe(client1, topic)
        self.assertTrue(topic in manager._subscribers)
        self.assert_topic_subscribed(topic)

        manager.unsubscribe(client2, topic)
        self.assertFalse(topic in manager._subscribers)
        self.assert_topic_not_subscribed(topic)

    def test_late_client_receives_all_transient_local_publisher_snapshots(self) -> None:
        """Do not drop sibling /tf_static-style retained publisher messages."""
        topic = "/test_late_client_retained_snapshots"
        msg_type = "std_msgs/String"
        first_client = "client_test_late_retained_first"
        late_client = "client_test_late_retained_late"
        publisher_qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        anchor_publisher = self.node.create_publisher(String, topic, publisher_qos)
        multicam_publisher = self.node.create_publisher(String, topic, publisher_qos)
        first_received: list[str] = []
        late_received: list[str] = []

        def first_callback(message: OutgoingMessage[String]) -> None:
            first_received.append(message.message.data)

        def late_callback(message: OutgoingMessage[String]) -> None:
            late_received.append(message.message.data)

        try:
            anchor_publisher.publish(String(data="world-anchor"))
            multicam_publisher.publish(String(data="multicam"))
            time.sleep(0.1)

            retained_qos = QoSProfile(depth=8, durability=DurabilityPolicy.TRANSIENT_LOCAL)
            manager.subscribe(first_client, topic, first_callback, self.node, msg_type, qos=retained_qos)
            deadline = time.monotonic() + 2.0
            while set(first_received) != {"world-anchor", "multicam"} and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(set(first_received), {"world-anchor", "multicam"})

            manager.subscribe(late_client, topic, late_callback, self.node, msg_type, qos=retained_qos)
            deadline = time.monotonic() + 2.0
            while set(late_received) != {"world-anchor", "multicam"} and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(set(late_received), {"world-anchor", "multicam"})
        finally:
            manager.unsubscribe(late_client, topic)
            manager.unsubscribe(first_client, topic)
            self.node.destroy_publisher(anchor_publisher)
            self.node.destroy_publisher(multicam_publisher)

    def test_register_json_and_raw_subscribers_on_same_topic(self) -> None:
        """Keep decoded and CDR subscriptions isolated for mixed clients."""
        topic = "/test_register_json_and_raw_subscribers_on_same_topic"
        msg_type = "std_msgs/String"
        json_client = "client_test_mixed_json"
        raw_client = "client_test_mixed_raw"
        received: dict[str, Any] = {"json": None, "raw": None}

        publisher_qos = QoSProfile(
            depth=10,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        pub = self.node.create_publisher(String, topic, publisher_qos)

        def json_cb(msg: OutgoingMessage[String]) -> None:
            received["json"] = msg.get_json_values()["data"]

        def raw_cb(msg: OutgoingMessage) -> None:
            received["raw"] = msg.message

        manager.subscribe(json_client, topic, json_cb, self.node, msg_type, raw=False)
        manager.subscribe(raw_client, topic, raw_cb, self.node, msg_type, raw=True)
        self.assertEqual(set(manager._subscribers[topic]), {False, True})

        time.sleep(0.1)
        pub.publish(String(data="mixed transport"))
        time.sleep(0.1)

        self.assertEqual(received["json"], "mixed transport")
        self.assertIsInstance(received["raw"], bytes)

        manager.unsubscribe(json_client, topic)
        self.assertIn(topic, manager._subscribers)
        manager.unsubscribe(raw_client, topic)
        self.assertNotIn(topic, manager._subscribers)
        self.node.destroy_publisher(pub)

    def test_register_publisher_conflicting_types(self) -> None:
        topic = "/test_register_publisher_conflicting_types"
        msg_type = "std_msgs/String"
        msg_type_bad = "std_msgs/Int32"
        client = "client_test_register_publisher_conflicting_types"

        self.assertFalse(topic in manager._subscribers)
        self.assert_topic_not_subscribed(topic)
        manager.subscribe(client, topic, lambda _: None, self.node, msg_type)
        self.assertTrue(topic in manager._subscribers)
        self.assert_topic_subscribed(topic)

        self.assertRaises(
            TypeConflictException,
            manager.subscribe,
            "client2",
            topic,
            None,
            self.node,
            msg_type_bad,
        )

    def test_register_multiple_publishers(self) -> None:
        topic1 = "/test_register_multiple_publishers1"
        topic2 = "/test_register_multiple_publishers2"
        msg_type = "std_msgs/String"
        client = "client_test_register_multiple_publishers"

        self.assertFalse(topic1 in manager._subscribers)
        self.assertFalse(topic2 in manager._subscribers)
        self.assert_topic_not_subscribed(topic1)
        self.assert_topic_not_subscribed(topic2)

        manager.subscribe(client, topic1, lambda _: None, self.node, msg_type)
        self.assertTrue(topic1 in manager._subscribers)
        self.assert_topic_subscribed(topic1)
        self.assertFalse(topic2 in manager._subscribers)
        self.assert_topic_not_subscribed(topic2)

        manager.subscribe(client, topic2, lambda _: None, self.node, msg_type)
        self.assertTrue(topic1 in manager._subscribers)
        self.assert_topic_subscribed(topic1)
        self.assertTrue(topic2 in manager._subscribers)
        self.assert_topic_subscribed(topic2)

        manager.unsubscribe(client, topic1)
        self.assertFalse(topic1 in manager._subscribers)
        self.assert_topic_not_subscribed(topic1)
        self.assertTrue(topic2 in manager._subscribers)
        self.assert_topic_subscribed(topic2)

        manager.unsubscribe(client, topic2)
        self.assertFalse(topic1 in manager._subscribers)
        self.assert_topic_not_subscribed(topic1)
        self.assertFalse(topic2 in manager._subscribers)
        self.assert_topic_not_subscribed(topic2)

    def test_register_no_msgtype(self) -> None:
        topic = "/test_register_no_msgtype"
        client = "client_test_register_no_msgtype"

        self.assertFalse(topic in manager._subscribers)
        self.assert_topic_not_subscribed(topic)
        self.assertRaises(
            TopicNotEstablishedException, manager.subscribe, client, topic, None, self.node
        )

    def test_register_infer_topictype(self) -> None:
        topic = "/test_register_infer_topictype"
        client = "client_test_register_infer_topictype"

        self.assert_topic_not_subscribed(topic)

        subscriber_qos = QoSProfile(
            depth=10,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.node.create_subscription(String, topic, lambda *_args: None, subscriber_qos)

        self.assert_topic_subscribed(topic)
        self.assertFalse(topic in manager._subscribers)

        manager.subscribe(client, topic, lambda _: None, self.node)
        self.assertTrue(topic in manager._subscribers)
        self.assert_topic_subscribed(topic)

        manager.unsubscribe(client, topic)
        self.assertFalse(topic in manager._subscribers)
        self.assert_topic_subscribed(topic)

    def test_register_multiple_notopictype(self) -> None:
        topic = "/test_register_multiple_notopictype"
        msg_type = "std_msgs/String"
        client1 = "client_test_register_multiple_notopictype_1"
        client2 = "client_test_register_multiple_notopictype_2"

        self.assertFalse(topic in manager._subscribers)
        self.assert_topic_not_subscribed(topic)

        manager.subscribe(client1, topic, lambda _: None, self.node, msg_type)
        self.assertTrue(topic in manager._subscribers)
        self.assert_topic_subscribed(topic)

        manager.subscribe(client2, topic, lambda _: None, self.node)
        self.assertTrue(topic in manager._subscribers)
        self.assert_topic_subscribed(topic)

        manager.unsubscribe(client1, topic)
        self.assertTrue(topic in manager._subscribers)
        self.assert_topic_subscribed(topic)

        manager.unsubscribe(client2, topic)
        self.assertFalse(topic in manager._subscribers)
        self.assert_topic_not_subscribed(topic)

    def test_subscribe_not_registered(self) -> None:
        topic = "/test_subscribe_not_registered"
        client = "client_test_subscribe_not_registered"

        self.assertFalse(topic in manager._subscribers)
        self.assert_topic_not_subscribed(topic)
        self.assertRaises(
            TopicNotEstablishedException, manager.subscribe, client, topic, None, self.node
        )

    def test_publisher_manager_publish(self) -> None:
        topic = "/test_publisher_manager_publish"
        msg_type = "std_msgs/String"
        client = "client_test_publisher_manager_publish"

        msg = String()
        msg.data = "dsajfadsufasdjf"

        publisher_qos = QoSProfile(
            depth=10,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        pub = self.node.create_publisher(String, topic, publisher_qos)
        received: dict[str, Any] = {"msg": None}

        def cb(msg: OutgoingMessage[String]) -> None:
            received["msg"] = msg.get_json_values()

        manager.subscribe(client, topic, cb, self.node, msg_type)
        time.sleep(0.1)
        pub.publish(msg)
        time.sleep(0.1)
        self.assertEqual(msg.data, received["msg"]["data"])


if __name__ == "__main__":
    unittest.main()
