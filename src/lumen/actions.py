from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ActionDefinition:
    label: str
    ha_domain: str
    ha_service: str
    keywords: tuple[str, ...]
    risk_level: str = "medium"
    default_service_data: dict[str, Any] = field(default_factory=dict)


class ActionRegistry:
    _generic_services: dict[str, set[str]] = {
        "light": {"turn_on", "turn_off", "toggle"},
        "switch": {"turn_on", "turn_off", "toggle"},
        "fan": {"turn_on", "turn_off", "toggle"},
        "input_boolean": {"turn_on", "turn_off", "toggle"},
        "media_player": {"turn_on", "turn_off", "toggle", "media_play", "media_pause", "media_stop"},
        "cover": {"open_cover", "close_cover", "stop_cover", "toggle"},
        "lock": {"lock", "unlock", "open"},
        "scene": {"turn_on"},
        "script": {"turn_on"},
    }

    def __init__(self) -> None:
        self._actions: dict[str, ActionDefinition] = {
            "run_script_turnoffeverything": ActionDefinition(
                label="Run script.turnoffeverything",
                ha_domain="script",
                ha_service="turn_on",
                keywords=("turn off everything", "turnoffeverything", "all off", "вимкни все"),
                risk_level="medium",
                default_service_data={"entity_id": "script.turnoffeverything"},
            ),
            "guest_mode_on": ActionDefinition(
                label="Enable guest mode",
                ha_domain="input_boolean",
                ha_service="turn_on",
                keywords=("guest mode", "enable guest mode", "turn on guest mode", "гостьовий режим", "увімкни гостьовий режим"),
                risk_level="low",
                default_service_data={"entity_id": "input_boolean.guest_mode"},
            ),
            "guest_mode_off": ActionDefinition(
                label="Disable guest mode",
                ha_domain="input_boolean",
                ha_service="turn_off",
                keywords=("disable guest mode", "turn off guest mode", "вимкни гостьовий режим"),
                risk_level="low",
                default_service_data={"entity_id": "input_boolean.guest_mode"},
            ),
            "sleep_mode_on": ActionDefinition(
                label="Enable sleep mode",
                ha_domain="input_boolean",
                ha_service="turn_on",
                keywords=("sleep mode", "enable sleep mode", "bedtime mode", "режим сну", "увімкни режим сну"),
                risk_level="low",
                default_service_data={"entity_id": "input_boolean.sleeping"},
            ),
            "party_mode_on": ActionDefinition(
                label="Enable party mode",
                ha_domain="input_boolean",
                ha_service="turn_on",
                keywords=("party mode", "enable party mode", "режим вечірки", "увімкни режим вечірки"),
                risk_level="low",
                default_service_data={"entity_id": "input_boolean.party"},
            ),
        }

    def match(self, text: str) -> tuple[str, ActionDefinition] | None:
        lowered = text.lower()
        for action_key, definition in self._actions.items():
            if any(keyword in lowered for keyword in definition.keywords):
                return action_key, definition
        return None

    def is_allowed(self, domain: str, service: str, service_data: dict[str, Any]) -> bool:
        for definition in self._actions.values():
            if definition.ha_domain != domain or definition.ha_service != service:
                continue
            if definition.default_service_data.items() <= service_data.items():
                return True
        return self.is_generic_allowed(domain, service, service_data)

    def is_generic_allowed(self, domain: str, service: str, service_data: dict[str, Any]) -> bool:
        allowed_services = self._generic_services.get(domain)
        if not allowed_services or service not in allowed_services:
            return False
        if set(service_data) != {"entity_id"}:
            return False
        entity_id = service_data.get("entity_id")
        return isinstance(entity_id, str) and entity_id.startswith(f"{domain}.")
