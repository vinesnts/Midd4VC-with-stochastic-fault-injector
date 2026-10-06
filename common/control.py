"""MQTT control-plane primitives shared by system and experiment services."""

import json
import os
import threading
import time
from datetime import datetime

import paho.mqtt.client as mqtt


BROKER = os.getenv("MQTT_BROKER", "localhost")
PORT = int(os.getenv("MQTT_PORT", 1883))
CONTROL_TOPIC = "vc/control/{target_id}/command"
EVENT_TOPIC = "vc/control/{target_id}/event"


def _now():
    return datetime.now().isoformat(timespec="microseconds")


def _publish(topic, payload, client_id) -> bool:
    """Publish one QoS-1 message after the short-lived client is connected."""
    client = mqtt.Client(client_id=client_id)
    try:
        client.connect(BROKER, PORT, 60)
        client.loop_start()
        deadline = time.monotonic() + 5.0
        while not client.is_connected() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not client.is_connected():
            return False
        info = client.publish(topic, json.dumps(payload), qos=1)
        info.wait_for_publish(timeout=5.0)
        return info.rc == mqtt.MQTT_ERR_SUCCESS
    except (OSError, RuntimeError, ValueError):
        return False
    finally:
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass


def publish_control(target_id, action, reason, source) -> bool:
    """Publish a lifecycle command for a system target."""
    payload = {
        "target_id": target_id,
        "action": action,
        "reason": reason,
        "source": source,
        "timestamp": _now(),
    }
    return _publish(CONTROL_TOPIC.format(target_id=target_id), payload, f"control-{source}-{target_id}")


class ControlListener:
    """Persistent control connection that survives data-plane outages."""

    def __init__(self, target_id, handler, source="system"):
        self.target_id = target_id
        self.handler = handler
        self.source = source
        self.client = mqtt.Client(client_id=f"control-listener-{source}-{target_id}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect
        self._stopping = threading.Event()

    def start(self):
        self.client.connect(BROKER, PORT, 60)
        self.client.loop_start()

    def stop(self):
        self._stopping.set()
        self.client.loop_stop()
        self.client.disconnect()

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            client.subscribe(CONTROL_TOPIC.format(target_id=self.target_id), qos=1)

    def _on_disconnect(self, client, userdata, rc):
        if not self._stopping.is_set() and rc != 0:
            try:
                client.reconnect()
            except Exception:
                pass

    def _on_message(self, client, userdata, message):
        try:
            command = json.loads(message.payload.decode())
            if command.get("target_id") == self.target_id:
                self.handler(command)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            return


def publish_event(target_id, event, reason, source, **metadata) -> bool:
    payload = {
        "target_id": target_id,
        "event": event,
        "reason": reason,
        "source": source,
        "timestamp": _now(),
    }
    payload.update(metadata)
    return _publish(EVENT_TOPIC.format(target_id=target_id), payload, f"event-{source}-{target_id}")
