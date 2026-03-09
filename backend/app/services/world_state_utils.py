"""世界状态规范化工具。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Iterable, Set


STANDARD_WORLD_STATE_FIELDS = {
    "current_location",
    "inventory",
    "relationships",
    "status_changes",
    "last_time_reference",
}

RESERVED_WORLD_STATE_FIELDS = {
    "_state_hidden_keys",
}


STATUS_KEY_ALIASES = {
    "cultivation_level": "修为",
    "realm": "修为",
    "主角修为": "修为",
    "境界": "修为",
    "main_cultivation": "修为",
    "wealth": "财富",
    "money": "财富",
    "gold": "财富",
    "生命值": "hp",
    "血量": "hp",
    "气血": "hp",
    "health": "hp",
    "health_points": "hp",
}

KNOWN_STATUS_KEYS = set(STATUS_KEY_ALIASES.values()) | {
    "修为",
    "hp",
    "财富",
    "生命状态",
    "精神状态",
    "伤势",
    "天命状态",
}

ABSOLUTE_STATE_KEY_MARKERS = {
    "修为",
    "境界",
    "职位",
    "身份",
    "称号",
    "头衔",
    "position",
    "title",
    "identity",
    "role",
    "rank",
    "grade",
}

BODY_PART_KEYWORDS = (
    "手掌",
    "虎口",
    "胸口",
    "肩膀",
    "肩头",
    "手臂",
    "胳膊",
    "小腹",
    "腹部",
    "五脏六腑",
    "脏腑",
    "骨头缝",
    "额头",
    "嘴角",
    "肋骨",
    "经脉",
)

INJURY_VALUE_KEYWORDS = (
    "流血",
    "裂",
    "断",
    "伤",
    "血肉",
    "模糊",
    "淤",
    "疼",
    "见骨",
)


def _clean_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def normalize_state_key(raw_key: Any) -> str:
    """规范化状态键名，尽量收敛同义字段。"""
    if raw_key is None:
        return ""

    key = str(raw_key).strip()
    if not key or key.startswith("_"):
        return ""

    alias = STATUS_KEY_ALIASES.get(key)
    if alias:
        return alias

    key_lower = key.lower()
    alias = STATUS_KEY_ALIASES.get(key_lower)
    if alias:
        return alias

    return key


def is_absolute_state_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker.lower() in lowered for marker in ABSOLUTE_STATE_KEY_MARKERS)


def looks_like_status_key(key: str, value: Any = None) -> bool:
    """判断一个顶层键是否更像“状态”而不是普通自定义字段。"""
    canonical_key = normalize_state_key(key)
    if not canonical_key:
        return False

    if canonical_key in KNOWN_STATUS_KEYS:
        return True

    markers = ("状态", "修为", "境界", "伤势", "财富", "天命", "精神", "气运")
    if any(marker in canonical_key for marker in markers):
        return True

    if canonical_key.lower() in {"hp", "mp", "sp"}:
        return True

    return False


def is_noisy_status_entry(key: str, value: Any) -> bool:
    """过滤局部伤口等低价值、易污染全局状态的字段。"""
    if not isinstance(value, str):
        return False

    cleaned_value = value.strip()
    if not cleaned_value:
        return False

    return any(part in key for part in BODY_PART_KEYWORDS) and any(
        marker in cleaned_value for marker in INJURY_VALUE_KEYWORDS
    )


def merge_status_value(existing_value: Any, new_value: Any) -> Any:
    """合并同义状态键，优先保留信息量更高的值。"""
    if existing_value is None:
        return deepcopy(new_value)

    if isinstance(existing_value, (int, float)) and isinstance(new_value, (int, float)):
        if existing_value == new_value:
            return existing_value
        return new_value if abs(new_value) >= abs(existing_value) else existing_value

    if isinstance(existing_value, str) and isinstance(new_value, str):
        existing_cleaned = existing_value.strip()
        new_cleaned = new_value.strip()
        if not existing_cleaned:
            return new_cleaned
        if not new_cleaned:
            return existing_cleaned
        return new_cleaned if len(new_cleaned) >= len(existing_cleaned) else existing_cleaned

    return deepcopy(new_value)


def normalize_string_list(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []

    results: list[str] = []
    seen: set[str] = set()
    for item in items:
        cleaned = _clean_string(item)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        results.append(cleaned)
    return results


def normalize_relationships(raw_relationships: Any) -> Dict[str, str]:
    if not isinstance(raw_relationships, dict):
        return {}

    results: Dict[str, str] = {}
    for raw_key, raw_value in raw_relationships.items():
        key = _clean_string(raw_key)
        value = _clean_string(raw_value)
        if not key or not value:
            continue
        results[key] = value
    return results


def get_suppressed_state_keys(world_state: Any) -> Set[str]:
    if not isinstance(world_state, dict):
        return set()

    raw_keys = world_state.get("_state_hidden_keys", [])
    if not isinstance(raw_keys, list):
        return set()

    return {
        normalized
        for normalized in (normalize_state_key(key) for key in raw_keys)
        if normalized
    }


def set_suppressed_state_keys(world_state: Dict[str, Any], keys: Iterable[str]) -> Dict[str, Any]:
    normalized_keys = sorted({normalize_state_key(key) for key in keys if normalize_state_key(key)})
    if normalized_keys:
        world_state["_state_hidden_keys"] = normalized_keys
    else:
        world_state.pop("_state_hidden_keys", None)
    return world_state


def normalize_status_changes(raw_status_changes: Any, suppressed_keys: Iterable[str] | None = None) -> Dict[str, Any]:
    if not isinstance(raw_status_changes, dict):
        return {}

    suppressed = {
        normalized
        for normalized in (normalize_state_key(key) for key in (suppressed_keys or []))
        if normalized
    }

    results: Dict[str, Any] = {}
    for raw_key, raw_value in raw_status_changes.items():
        key = normalize_state_key(raw_key)
        if not key or key in suppressed:
            continue

        value = _clean_string(raw_value) if isinstance(raw_value, str) else raw_value
        if value in (None, "", [], {}):
            continue

        if is_noisy_status_entry(key, value):
            continue

        if key in results:
            results[key] = merge_status_value(results[key], value)
        else:
            results[key] = deepcopy(value)

    return results


def normalize_state_change_payload(raw_payload: Any, suppressed_keys: Iterable[str] | None = None) -> Dict[str, Any]:
    """规范化章节状态提取结果。"""
    if not isinstance(raw_payload, dict):
        return {}

    payload = deepcopy(raw_payload)
    result: Dict[str, Any] = {}

    location_change = payload.get("location_change")
    if isinstance(location_change, dict):
        from_location = _clean_string(location_change.get("from"))
        to_location = _clean_string(location_change.get("to"))
        if from_location or to_location:
            result["location_change"] = {
                "from": from_location,
                "to": to_location,
            }

    items_gained = normalize_string_list(payload.get("items_gained"))
    if items_gained:
        result["items_gained"] = items_gained

    items_lost = normalize_string_list(payload.get("items_lost"))
    if items_lost:
        result["items_lost"] = items_lost

    status_changes = normalize_status_changes(payload.get("status_changes", {}), suppressed_keys=suppressed_keys)
    if status_changes:
        result["status_changes"] = status_changes

    relationships = normalize_relationships(payload.get("relationships", {}))
    if relationships:
        result["relationships"] = relationships

    time_passed = _clean_string(payload.get("time_passed"))
    if time_passed:
        result["time_passed"] = time_passed

    summary = _clean_string(payload.get("summary"))
    if summary:
        result["summary"] = summary

    end_hook = payload.get("end_hook")
    if isinstance(end_hook, dict):
        hook_type = _clean_string(end_hook.get("type"))
        hook_content = _clean_string(end_hook.get("content"))
        must_respond_next = bool(end_hook.get("must_respond_next", False))
        if hook_type or hook_content:
            result["end_hook"] = {
                "type": hook_type or "未知",
                "content": hook_content or "",
                "must_respond_next": must_respond_next,
            }

    return result


def normalize_world_state_structure(world_state: Any, preserve_suppressed: bool = True) -> Dict[str, Any]:
    """规范化 world_state 结构，避免顶层与 status_changes 双写。"""
    raw_state = deepcopy(world_state) if isinstance(world_state, dict) else {}
    suppressed_keys = get_suppressed_state_keys(raw_state) if preserve_suppressed else set()

    result: Dict[str, Any] = {}

    current_location = _clean_string(raw_state.get("current_location"))
    if current_location:
        result["current_location"] = current_location

    inventory = normalize_string_list(raw_state.get("inventory"))
    if inventory:
        result["inventory"] = inventory

    relationships = normalize_relationships(raw_state.get("relationships", {}))
    if relationships:
        result["relationships"] = relationships

    last_time_reference = _clean_string(raw_state.get("last_time_reference"))
    if last_time_reference:
        result["last_time_reference"] = last_time_reference

    combined_status_sources: Dict[str, Any] = {}
    if isinstance(raw_state.get("status_changes"), dict):
        combined_status_sources.update(raw_state["status_changes"])

    for key, value in raw_state.items():
        if key in STANDARD_WORLD_STATE_FIELDS or key in RESERVED_WORLD_STATE_FIELDS or key.startswith("_"):
            continue

        if key in combined_status_sources or looks_like_status_key(key, value):
            combined_status_sources[key] = value
        else:
            result[key] = deepcopy(value)

    normalized_status_changes = normalize_status_changes(combined_status_sources, suppressed_keys=suppressed_keys)
    if normalized_status_changes:
        result["status_changes"] = normalized_status_changes

    return set_suppressed_state_keys(result, suppressed_keys)

