import orjson  # pylint: disable=import-error


def to_hap_json(dump_obj):
    """Convert an object to HAP json."""
    return orjson.dumps(dump_obj)  # pragma: nocover


def to_sorted_hap_json(dump_obj):
    """Convert an object to sorted HAP json."""
    return orjson.dumps(dump_obj, option=orjson.OPT_SORT_KEYS)  # pragma: nocover


def from_hap_json(json_str):
    """Convert json to an object."""
    return orjson.loads(json_str)  # pragma: nocover
