"""CLIP v2 resource model for official Hue clients (Hue Sync app).

Pure functions, no Home Assistant dependency: everything is derived from the
bridge id, the ordered light list and the streaming state, so resource ids
are stable across restarts without any storage.

Shapes follow what the diyHue emulator serves (reported to work with the
Samsung Hue Sync app). One entertainment configuration mirrors the v1
entertainment group: channel ``i`` is the ``i``-th selected light, which is
how the engine resolves HueStream v2 channel ids.
"""

from __future__ import annotations

import uuid
from typing import Any

from .const import BRIDGE_API_VERSION, BRIDGE_MODEL_ID, BRIDGE_SW_VERSION

_NAMESPACE = uuid.UUID("5f3c6a2e-8c1d-4b8e-9a43-0d6f2b1e7c55")
_APPLICATION_NAMESPACE = uuid.UUID("0c7e2d84-3b9a-4f61-a5d2-6e8f1b4c9a37")

BRIDGE_KEY = "bridge"
ENTERTAINMENT_GROUP_NAME = "TV Entertainment"
GAMUT_C = {
    "red": {"x": 0.6915, "y": 0.3083},
    "green": {"x": 0.17, "y": 0.7},
    "blue": {"x": 0.1532, "y": 0.0475},
}
_NAME_MAX = 32  # CLIP v2 metadata.name limit

Resource = dict[str, Any]


def application_id(username: str) -> str:
    """The client's public application id (``hue-application-id``, ``active_streamer``).

    A real bridge returns a UUID here, distinct from the application key. The
    username *is* the key (the credential), so it must never be shown to other
    clients; this id is derived from it, one way, and is stable.
    """
    return str(uuid.uuid5(_APPLICATION_NAMESPACE, username))


def rid(bridge_id: str, key: str, rtype: str) -> str:
    """Deterministic resource id for ``key`` (bridge or light entity) and ``rtype``."""
    return str(uuid.uuid5(_NAMESPACE, f"{bridge_id.lower()}/{key}/{rtype}"))


def ref(bridge_id: str, key: str, rtype: str) -> dict[str, str]:
    """Resource identifier ``{rid, rtype}``."""
    return {"rid": rid(bridge_id, key, rtype), "rtype": rtype}


def light_mac_address(entity_id: str) -> str:
    """Stable 8-byte Zigbee address for a light (the v1 ``uniqueid`` without its endpoint)."""
    h = uuid.uuid5(uuid.NAMESPACE_DNS, f"uniqueid-{entity_id}").hex
    return ":".join(h[j : j + 2] for j in range(0, 16, 2))


def software_version() -> str:
    """Bridge software version as CLIP v2 reports it, e.g. ``1.67.1967054020``."""
    major_minor = ".".join(BRIDGE_API_VERSION.split(".")[:2])
    return f"{major_minor}.{BRIDGE_SW_VERSION}"


def synthetic_positions(count: int) -> list[list[float]]:
    """Lights spread left to right in front of the screen (shared with v1 locations)."""
    return [[round(-1.0 + 2.0 * i / max(count - 1, 1), 4), 1.0, 0.0] for i in range(count)]


def entertainment_configuration_id(bridge_id: str) -> str:
    """Id of the single entertainment configuration."""
    return rid(bridge_id, BRIDGE_KEY, "entertainment_configuration")


def build_resources(
    bridge_id: str,
    lights: list[str],
    *,
    active: bool = False,
    owner: str | None = None,
) -> list[Resource]:
    """Every CLIP v2 resource the bridge exposes."""
    b = bridge_id.lower()
    resources: list[Resource] = [
        {
            "id": rid(b, BRIDGE_KEY, "bridge"),
            "id_v1": "",
            "type": "bridge",
            "bridge_id": b,
            "owner": ref(b, BRIDGE_KEY, "device"),
            "time_zone": {"time_zone": "Europe/London"},
        },
        {
            "id": rid(b, BRIDGE_KEY, "device"),
            "id_v1": "",
            "type": "device",
            "metadata": {"name": "Hue Entertainment Bridge", "archetype": "bridge_v2"},
            "identify": {},
            "product_data": {
                "model_id": BRIDGE_MODEL_ID,
                "manufacturer_name": "Signify Netherlands B.V.",
                "product_name": "Philips hue",
                "product_archetype": "bridge_v2",
                "certified": True,
                "software_version": software_version(),
            },
            "services": [
                ref(b, BRIDGE_KEY, "bridge"),
                ref(b, BRIDGE_KEY, "entertainment"),
            ],
        },
        {
            "id": rid(b, BRIDGE_KEY, "entertainment"),
            "id_v1": "",
            "type": "entertainment",
            "owner": ref(b, BRIDGE_KEY, "device"),
            "renderer": False,
            "proxy": True,
            "equalizer": False,
            "max_streams": 1,
        },
        {
            "id": rid(b, BRIDGE_KEY, "bridge_home"),
            "id_v1": "/groups/0",
            "type": "bridge_home",
            "children": [ref(b, BRIDGE_KEY, "device")]
            + [ref(b, light, "device") for light in lights],
            "services": [],
        },
    ]
    for i, light in enumerate(lights, 1):
        resources.extend(_light_resources(b, light, i))
    if lights:
        resources.append(_entertainment_configuration(b, lights, active=active, owner=owner))
    return resources


def _light_resources(b: str, light: str, light_id: int) -> list[Resource]:
    name = light[:_NAME_MAX]
    return [
        {
            "id": rid(b, light, "device"),
            "id_v1": f"/lights/{light_id}",
            "type": "device",
            "metadata": {"name": name, "archetype": "classic_bulb"},
            "identify": {},
            "product_data": {
                "model_id": "LCT015",
                "manufacturer_name": "Signify Netherlands B.V.",
                "product_name": "Hue color lamp",
                "product_archetype": "classic_bulb",
                "certified": True,
                "software_version": "1.104.2",
            },
            "services": [
                ref(b, light, "light"),
                ref(b, light, "zigbee_connectivity"),
                ref(b, light, "entertainment"),
            ],
        },
        {
            # Every field a real bridge sends for a colour bulb: strict clients
            # (Hue Essentials) reject a light missing any of them.
            "id": rid(b, light, "light"),
            "id_v1": f"/lights/{light_id}",
            "type": "light",
            "owner": ref(b, light, "device"),
            "metadata": {"name": name, "archetype": "classic_bulb", "function": "mixed"},
            "product_data": {"function": "mixed"},
            "identify": {},
            "service_id": 0,
            "on": {"on": True},
            "dimming": {"brightness": 100.0, "min_dim_level": 0.2},
            "dimming_delta": {},
            "color_temperature": {
                "mirek": None,
                "mirek_valid": False,
                "mirek_schema": {"mirek_minimum": 153, "mirek_maximum": 500},
            },
            "color_temperature_delta": {},
            "color": {
                "xy": {"x": 0.3127, "y": 0.329},
                "gamut": GAMUT_C,
                "gamut_type": "C",
            },
            "dynamics": {
                "status": "none",
                "status_values": ["none", "dynamic_palette"],
                "speed": 0.0,
                "speed_valid": False,
            },
            "alert": {"action_values": ["breathe"]},
            "signaling": {"signal_values": ["no_signal", "on_off", "on_off_color", "alternating"]},
            "mode": "normal",
            "effects": {
                "status_values": ["no_effect"],
                "status": "no_effect",
                "effect_values": ["no_effect"],
            },
            "timed_effects": {
                "status_values": ["no_effect"],
                "status": "no_effect",
                "effect_values": ["no_effect"],
            },
            "powerup": {
                "preset": "safety",
                "configured": True,
                "on": {"mode": "on", "on": {"on": True}},
                "dimming": {"mode": "dimming", "dimming": {"brightness": 100.0}},
                "color": {"mode": "color_temperature", "color_temperature": {"mirek": 366}},
            },
        },
        {
            "id": rid(b, light, "zigbee_connectivity"),
            "id_v1": f"/lights/{light_id}",
            "type": "zigbee_connectivity",
            "owner": ref(b, light, "device"),
            "status": "connected",
            "mac_address": light_mac_address(light),
        },
        {
            "id": rid(b, light, "entertainment"),
            "id_v1": f"/lights/{light_id}",
            "type": "entertainment",
            "owner": ref(b, light, "device"),
            "renderer": True,
            "renderer_reference": ref(b, light, "light"),
            "proxy": False,
            "equalizer": False,
            "segments": {
                "configurable": False,
                "max_segments": 1,
                "segments": [{"start": 0, "length": 1}],
            },
        },
    ]


def _entertainment_configuration(
    b: str, lights: list[str], *, active: bool, owner: str | None
) -> Resource:
    positions = synthetic_positions(len(lights))

    def _pos(i: int) -> dict[str, float]:
        x, y, z = positions[i]
        return {"x": x, "y": y, "z": z}

    config: Resource = {
        "id": entertainment_configuration_id(b),
        "id_v1": "/groups/1",
        "type": "entertainment_configuration",
        "name": ENTERTAINMENT_GROUP_NAME,
        "metadata": {"name": ENTERTAINMENT_GROUP_NAME},
        "configuration_type": "screen",
        "status": "active" if active else "inactive",
        "stream_proxy": {"mode": "auto", "node": ref(b, BRIDGE_KEY, "entertainment")},
        "light_services": [ref(b, light, "light") for light in lights],
        "locations": {
            "service_locations": [
                {
                    "service": ref(b, light, "entertainment"),
                    "position": _pos(i),
                    "positions": [_pos(i)],
                    "equalization_factor": 1.0,
                }
                for i, light in enumerate(lights)
            ]
        },
        "channels": [
            {
                "channel_id": i,
                "position": _pos(i),
                "members": [{"service": ref(b, light, "entertainment"), "index": 0}],
            }
            for i, light in enumerate(lights)
        ],
    }
    if active and owner:
        config["active_streamer"] = {"rid": application_id(owner), "rtype": "auth_v1"}
    return config


def envelope(data: list[Resource] | None = None, errors: list[str] | None = None) -> dict:
    """CLIP v2 response body: ``{"errors": [{description}], "data": [...]}``."""
    return {
        "errors": [{"description": e} for e in errors or []],
        "data": data or [],
    }
