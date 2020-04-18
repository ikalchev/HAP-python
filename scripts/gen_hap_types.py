#!/usr/bin/env python3
"""Create a json representation from the HomeKit Accessory Simulator Types."""
import plistlib
import json

# This path could be different.
HOMEKIT_TYPES_PLIST = "/Applications/Xcode.app/Contents/Applications/HomeKit Accessory Simulator.app/Contents/Frameworks/HAPAccessoryKit.framework/Versions/A/Resources/default.metadata.plist"
CHAR_OUT_FILE = "./pyhap/resources/characteristics.json"
SERVICE_OUT_FILE = "./pyhap/resources/services.json"

PERMS_MAP = {
    "read": "pr",
    "write": "pw",
    "cnotify": "ev",
   # TODO: find the 'symbol' for this one - "uncnotify": None,
}

CONSTRAINTS_MAP = {
    "MaximumValue": "maxValue",
    "MinimumValue": "minValue",
    "StepValue": "minStep",
}


def create_uuid2name_map(char_info):
    """Return a mapping UUIDs to Names."""
    uuid2name = {}
    for char in char_info:
        uuid2name[char["UUID"]] = char["Name"]
    return uuid2name


def fix_valid_values(char_info):
    """Valid values are given in a value: key format. Reverse them."""
    for char in char_info:
        if "ValidValues" not in char:
            continue
        valid_values = {}
        for value, state in char["ValidValues"].items():
            if "int" in char["Format"]:
                value = int(value)
            elif "float" == char["Format"]:
                value = float(value)
            else:
                raise ValueError
            state = state.replace(" ", "")  # To camel case.
            valid_values[state] = value
        char["ValidValues"] = valid_values


def tidy_char(char_info):
    """Various things we would like to change about a Characteristic representation."""
    for char in char_info:
        if "Unit" in char:
            char["unit"] = char.pop("Unit")
        if "Properties" in char:
            permissions = []
            for perm in char.pop("Properties"):
                if perm == "uncnotify":
                    continue
                permissions.append(PERMS_MAP[perm])
            char["Permissions"] = permissions
        if char["Format"] == "int32":
            char["Format"] = "int"
        if "Constraints" in char:
            constraints = char.pop("Constraints")
            for key, value in constraints.items():
                char[CONSTRAINTS_MAP.get(key, key)] = value


def replace_char_uuid(service_info, uuid2name):
    """Replace characteristics' UUID with their name for convenience."""
    for service in service_info:
        if "RequiredCharacteristics" in service:
            req_chars = []
            for char in service["RequiredCharacteristics"]:
                req_chars.append(uuid2name[char])
            service["RequiredCharacteristics"] = req_chars
        if "OptionalCharacteristics" in service:
            opt_chars = []
            for char in service.get("OptionalCharacteristics", []):
                opt_chars.append(uuid2name[char])
            service["OptionalCharacteristics"] = opt_chars


def camel_name(infos):
    """Transform the name to camel case, no spaces."""
    for info in infos:
        info["Name"] = info["Name"].replace(" ", "")


def list2dict(infos):
    """We want a mapping name: char/service for convenience, not a list."""
    info_dict = {}
    for info in infos:
        info_dict[info["Name"]] = info
        del info["Name"]
    return info_dict


def main():
    """Reads the HomeKit Simulator types and creates a HAP-python json representation."""
    with open(HOMEKIT_TYPES_PLIST, "rb") as types_plist_fp:
        type_info = plistlib.load(types_plist_fp)
    char_info = type_info["Characteristics"]
    service_info = type_info["Services"]

    camel_name(char_info)
    camel_name(service_info)

    uuid2name = create_uuid2name_map(char_info)

    tidy_char(char_info)
    fix_valid_values(char_info)
    with open(CHAR_OUT_FILE, "w") as char_fp:
        json.dump(list2dict(char_info), char_fp, indent=3, sort_keys=True)

    replace_char_uuid(service_info, uuid2name)
    with open(SERVICE_OUT_FILE, "w") as services_fp:
        json.dump(list2dict(service_info), services_fp, indent=3, sort_keys=True)


if __name__ == "__main__":
    main()
