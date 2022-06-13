import json


def to_hap_json(dump_obj):
    """Convert an object to HAP json."""
    return json.dumps(dump_obj, separators=(",", ":")).encode("utf-8")


def to_sorted_hap_json(dump_obj):
    """Convert an object to sorted HAP json."""
    return json.dumps(dump_obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def from_hap_json(json_str):
    """Convert json to an object."""
    return json.loads(json_str)
