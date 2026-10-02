"""The client identifier every MQTT connection presents.

Kept apart from the MQTT backend so MQTT Insights can use it without loading
aiomqtt before it connects.
"""

import uuid


def mqtt_client_id() -> str:
    """A fresh, non-empty MQTT client identifier.

    Left unset, aiomqtt connects with an empty one, which brokers may reject
    (Mosquitto with ``allow_zero_length_clientid false``); aiomqtt then just
    times out. Random, so several clients and instances never kick each other
    off the broker, and 23 alphanumerics, the most MQTT 3.1.1 guarantees.
    """
    return "astrameter" + uuid.uuid4().hex[:13]
