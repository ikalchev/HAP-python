#!/usr/bin/env python3
"""Generate pyhap/resources/characteristics.json and services.json from Apple metadata.

HOW THE PROCESS WORKS
====================

This script keeps HAP-python's characteristic/service catalog in sync with Apple's
official HomeKit definitions without hand-editing JSON files. It mirrors the
approach used by HAP-NodeJS (homebridge/HAP-NodeJS, `src/lib/definitions/
generate-definitions.ts`).

Sources, in order of increasing precedence:

1. plain-metadata.config  (PRIMARY, always the newest)
   Located at:
     /System/Library/PrivateFrameworks/HomeKitDaemon.framework/Resources/plain-metadata.config
   This file ships WITH macOS and is updated with every OS release, so it does not
   require Xcode or the HomeKit Accessory Simulator to be installed. It is a plain
   binary plist and contains, under PlistDictionary -> HAP:
     - Characteristics:  "<slug>" -> { DefaultDescription, Format, Properties (bitmask),
                                    ShortUUID, MinValue/MaxValue/StepValue, Units, ... }
     - Services:        "<slug>" -> { Characteristics: {Required: [...], Optional: [...]},
                                      ShortUUID, DefaultDescription }
     - Units, Properties (bit position legend for the Properties bitmask), Base UUID.

2. HomeKit Accessory Simulator plists  (OPTIONAL OVERLAY, needs Xcode tools)
   Located inside the "HomeKit Accessory Simulator.app" (see SIMULATOR_PLIST below).
   The simulator metadata lags behind plain-metadata.config, but it carries two
   things plain-metadata encodes poorly or not at all:
     - Permissions in string form ("read", "write", "cnotify", "timedWrite",
       "writeResponse", "uncnotify", ...) - richer than the bitmask.
     - ValidValues constraints for enumerated characteristics.
   When the simulator is not installed the script still works: permissions fall
   back to the Properties bitmask and ValidValues fall back to source 4 below.

3. scripts/hap_manual_overrides.json  (MANUAL OVERRIDES - the maintenance escape hatch)
   Apple's machine-readable metadata only covers definitions that have SHIPPED in
   an OS release. Things published only in a developer-preview document (e.g. the
   HomeKit Secure Video 17.99 Open Source Compatibility Guide, which is where the
   CameraBufferManagement / CameraWebRTCStreamManagement / ... 0x80xx definitions
   come from) do not appear in any plist yet. Those must be added by hand - but only
   ONCE, here, instead of re-parsing the PDF guide every time. When Apple eventually
   ships them in plain-metadata.config, the manual entry can simply be deleted.
   The overrides file supports:
     characteristicNameOverrides  slug -> HAP-python name (when the derived name is wrong)
     characteristicAdditions       new characteristics, same schema as characteristics.json
     characteristicOverrides       full replacement of a generated characteristic entry
     serviceAdditions              new services, same schema as services.json
     serviceOverrides              full replacement of a generated service entry

4. Previously generated output  (FIELD INHERITANCE)
   When the simulator is unavailable, some fields (most importantly "ValidValues",
   occasionally "maxLen"/"unit") cannot be derived from plain-metadata.config. To
   avoid silently dropping them, fields present in the existing
   characteristics.json / services.json entry (matched by UUID) are carried over
   when the freshly generated entry is missing them. This keeps the catalog
   stable across regenerations on machines without the simulator.

OUTPUT
======
By default the script runs as a DRY RUN: it prints a report of what would be
added/removed/changed relative to the committed JSON files. Pass --write to
actually rewrite them. Always review the diff before committing.

The old version of this script parsed ONLY the HomeKit Accessory Simulator plist;
it has been rewritten because that plist lags far behind (163 characteristics
vs 242+ in plain-metadata.config at the time of writing) and requires Xcode.
"""

import argparse
import json
import plistlib
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Source locations
# ---------------------------------------------------------------------------

# Primary source: ships with macOS, updated with every OS release.
PLAIN_METADATA_PATH = Path(
    "/System/Library/PrivateFrameworks/HomeKitDaemon.framework/Resources/plain-metadata.config"
)

# Optional overlay: only present when Xcode's HomeKit Accessory Simulator is installed.
SIMULATOR_PLIST = Path(
    "/Applications/Xcode.app/Contents/Applications/HomeKit Accessory Simulator.app/"
    "Contents/Frameworks/HAPAccessoryKit.framework/Versions/A/Resources/default.metadata.plist"
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CHAR_OUT_FILE = REPO_ROOT / "pyhap/resources/characteristics.json"
SERVICE_OUT_FILE = REPO_ROOT / "pyhap/resources/services.json"
MANUAL_OVERRIDES_FILE = Path(__file__).resolve().parent / "hap_manual_overrides.json"

# ---------------------------------------------------------------------------
# Mapping tables
# ---------------------------------------------------------------------------

# plain-metadata.config encodes characteristic permissions as a bitmask over
# HAP.Properties (key -> bit position). Decode into HAP-python permission
# strings. Bits HAP-python does not understand (aa, broadcast, hidden via bit,
# writeResponse) are handled separately below.
PERMISSION_BITS = {
    "read": "pr",
    "write": "pw",
    "notify": "ev",
    "timedWrite": "tw",
    "writeResponse": "wr",
}

# Simulator plist permission strings -> HAP-python permission strings.
# (Kept from the previous version of this script; "uncnotify" has no
# HAP-python representation and is skipped.)
SIMULATOR_PERMS_MAP = {
    "read": "pr",
    "write": "pw",
    "cnotify": "ev",
    "timedWrite": "tw",
    "writeResponse": "wr",
}

# Simulator plist constraint keys -> HAP-python characteristic field names.
SIMULATOR_CONSTRAINTS_MAP = {
    "MaximumValue": "maxValue",
    "MinimumValue": "minValue",
    "StepValue": "minStep",
    "MaxLength": "maxLen",
}

# plain-metadata.config char fields -> HAP-python characteristic field names.
METADATA_FIELDS_MAP = {
    "MaxValue": "maxValue",
    "MinValue": "minValue",
    "StepValue": "minStep",
    "MaxLength": "maxLen",
    "Units": "unit",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def to_hap_name(description):
    """Convert a human-readable description to the HAP-python catalog name.

    Apple metadata uses e.g. "Current Ambient Light Level" or "PM2.5 Density",
    while the JSON catalog uses the spaces-stripped form ("PM25Density").
    Hyphens ("Wi-Fi", "Heater-Cooler") are stripped as well so newly added
    entries follow the same convention.
    """
    return description.replace(" ", "").replace("-", "")


def long_uuid(short_uuid, base_uuid="-0000-1000-8000-0026BB765291"):
    """Expand a short UUID (e.g. "000000B0") into the full HAP UUID form."""
    return f"{short_uuid}{base_uuid}"


def perms_from_bitmask(properties, hap_properties):
    """Decode the plain-metadata Properties bitmask into HAP-python permissions.

    `properties` is the integer bitmask; `hap_properties` is the HAP.Properties
    legend (name -> {"Position": n}) from the same file.
    Admin-only characteristics (the "adminOnly" bit) are recorded separately in
    "AdminOnlyAccess": ["write"] in the output, mirroring HAP-NodeJS; HAP-python
    does not enforce this, it is informational.
    """
    perms = []
    admin_only = False
    for name, info in hap_properties.items():
        if not properties & (1 << info["Position"]):
            continue
        if name == "adminOnly":
            admin_only = True
        elif name in PERMISSION_BITS:
            perms.append(PERMISSION_BITS[name])
    # Keep a stable, conventional order.
    order = {"pr": 0, "pw": 1, "ev": 2, "tw": 3, "wr": 4}
    perms.sort(key=lambda p: order.get(p, 99))
    return perms, admin_only


# ---------------------------------------------------------------------------
# Source parsers
# ---------------------------------------------------------------------------


def load_plain_metadata(path):
    """Parse plain-metadata.config and return (characteristics, services, legend).

    Returns raw Apple dicts keyed by slug:
      characteristics: slug -> {DefaultDescription, Format, Properties, ShortUUID, ...}
      services:        slug -> {ShortUUID, Characteristics: {Required, Optional}, ...}
      units:           slug -> DefaultDescription (e.g. "percentage" -> "Percentage")
      hap_properties:  the HAP.Properties bitmask legend
    """
    with path.open("rb") as fp:
        metadata = plistlib.load(fp)
    hap = metadata["PlistDictionary"]["HAP"]
    return (
        hap["Characteristics"],
        hap["Services"],
        hap["Units"],
        hap["Properties"],
    )


def load_simulator_plist(path):
    """Parse the HomeKit Accessory Simulator plist (optional overlay).

    Returns {UUID -> char entry} with Name, Format, Permissions, Constraints.
    The simulator keys entries by full UUID; the Permissions strings are richer
    than the plain-metadata bitmask and Constraints carry ValidValues.
    """
    with path.open("rb") as fp:
        data = plistlib.load(fp)
    chars = {}
    for char in data.get("Characteristics", []):
        chars[char["UUID"].upper()] = char
    return chars


def fix_valid_values(char_info):
    """Valid values are given in a value: key format. Reverse them."""
    for char in char_info.values():
        if "ValidValues" not in char:
            continue
        valid_values = {}
        for value, state in char["ValidValues"].items():
            if "int" in char["Format"] or char["Format"].startswith("uint"):
                value = int(value)
            elif "float" == char["Format"]:
                value = float(value)
            state = state.replace(" ", "")  # To camel case.
            valid_values[state] = value
        char["ValidValues"] = valid_values


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def generate_characteristics(meta_chars, units, hap_properties, sim_chars,
                             manual, existing_chars, name_overrides):
    """Build the HAP-python characteristics catalog.

    Steps:
      1. Convert every plain-metadata characteristic to catalog form.
      2. Overlay simulator Permissions/ValidValues/constraints where available.
      3. Inherit fields (ValidValues etc.) from the previously generated file.
      4. Apply manual overrides and additions last (highest precedence).
    Returns (catalog, slug_to_final) where slug_to_final maps each Apple slug
    to the FINAL catalog name (after UUID-based name resolution). Service
    definitions reference characteristics by slug, so this map is required to
    resolve them to names that actually exist in the catalog.
    """
    catalog = {}
    slug_to_final = {}

    # Names: prefer the name already used in the committed catalog for a given
    # UUID so that regenerating never renames an existing characteristic
    # (Apple's descriptions drift over time, e.g. "CarbonDioxideDetected" vs
    # "Carbon dioxide Detected"). New UUIDs get a name derived from the
    # description, with any manual override taking precedence.
    uuid_to_name = {}
    for prev_name, prev in existing_chars.items():
        uuid_to_name.setdefault(prev.get("UUID", "").upper(), prev_name)
    for slug, info in meta_chars.items():
        uuid = long_uuid(info["ShortUUID"])
        name = (name_overrides.get(slug)
                or uuid_to_name.get(uuid.upper())
                or to_hap_name(info["DefaultDescription"]))
        slug_to_final[slug] = name
        entry = {
            "Format": info["Format"],
            "Permissions": None,  # filled below
            "UUID": long_uuid(info["ShortUUID"]),
        }
        for meta_key, out_key in METADATA_FIELDS_MAP.items():
            if meta_key in info:
                entry[out_key] = info[meta_key]

        perms, admin_only = perms_from_bitmask(info["Properties"], hap_properties)

        # Simulator overlay: richer permission strings + constraints.
        sim = sim_chars.get(entry["UUID"].upper())
        if sim:
            perms = [SIMULATOR_PERMS_MAP[p] for p in sim.get("Permissions", [])
                     if p in SIMULATOR_PERMS_MAP]
            constraints = sim.get("Constraints") or {}
            for sim_key, out_key in SIMULATOR_CONSTRAINTS_MAP.items():
                if sim_key in constraints:
                    entry[out_key] = constraints[sim_key]

        entry["Permissions"] = perms
        if admin_only:
            entry["AdminOnlyAccess"] = ["write"]

        # Field inheritance from the previously generated catalog (matched by
        # UUID) for anything plain-metadata cannot express.
        prev = existing_chars.get(name)
        if prev and prev.get("UUID") == entry["UUID"]:
            for key, value in prev.items():
                if key not in entry and key != "ValidValues":
                    entry[key] = value
        catalog[name] = entry

    if sim_chars:
        # Apply ValidValues conversion on the overlay entries.
        for name, entry in catalog.items():
            sim = sim_chars.get(entry["UUID"].upper())
            if sim and "ValidValues" in (sim.get("Constraints") or {}):
                c = {"ValidValues": sim["Constraints"]["ValidValues"],
                     "Format": entry["Format"]}
                fix_valid_values(c)
                entry["ValidValues"] = c["ValidValues"]
    else:
        # No simulator: inherit ValidValues from the previous catalog.
        for name, entry in catalog.items():
            prev = existing_chars.get(name)
            if prev and prev.get("UUID") == entry["UUID"] and "ValidValues" in prev:
                entry["ValidValues"] = prev["ValidValues"]

    # If a characteristic has enumerated ValidValues, drop the numeric
    # min/max/step constraints. HAP-python clamps values to min/max BEFORE
    # checking ValidValues, so keeping both would turn invalid values into
    # silent clamping (e.g. a stored 7 for Active would clamp to maxValue=1
    # and read back as "active") instead of being rejected.
    for entry in catalog.values():
        if "ValidValues" in entry:
            for key in ("maxValue", "minValue", "minStep"):
                entry.pop(key, None)

    # Manual overrides and additions (highest precedence).
    for name, entry in manual.get("characteristicOverrides", {}).items():
        catalog[name] = entry
    for name, entry in manual.get("characteristicAdditions", {}).items():
        catalog[name] = entry

    return catalog, slug_to_final


def generate_services(meta_services, characteristics, manual, name_overrides,
                        existing_services):
    """Build the HAP-python services catalog from plain-metadata + manual files.

    plain-metadata references characteristics by slug; each slug is translated
    to the catalog name computed in generate_characteristics(). Slugs that are
    unknown (dropped from Apple metadata) are reported and skipped.
    """
    catalog = {}

    # Same UUID-based naming rule as for characteristics: services keep the
    # name already used in the committed catalog (e.g. "Slat" instead of
    # Apple's newer "Slats" description for UUID 0x000000B9).
    uuid_to_name = {}
    for prev_name, prev in existing_services.items():
        uuid_to_name.setdefault(prev.get("UUID", "").upper(), prev_name)
    for slug, info in meta_services.items():
        uuid = long_uuid(info["ShortUUID"])
        name = uuid_to_name.get(uuid.upper()) or to_hap_name(info["DefaultDescription"])
        # Resolve the characteristic slugs referenced by the service into
        # catalog names using the slug -> name table (name_overrides["_slug_to_name"]
        # plus any manual name overrides).
        def resolve(slug_c):
            return (name_overrides.get(slug_c)
                    or name_overrides.get("_slug_to_name", {}).get(slug_c))

        req_opt = info["Characteristics"]
        entry = {
            "OptionalCharacteristics": [],
            "RequiredCharacteristics": [],
            "UUID": long_uuid(info["ShortUUID"]),
        }
        unknown = []
        for slug_c in req_opt.get("Optional", []):
            cname = resolve(slug_c)
            if cname and cname in characteristics:
                entry["OptionalCharacteristics"].append(cname)
            else:
                unknown.append(slug_c)
        for slug_c in req_opt.get("Required", []):
            cname = resolve(slug_c)
            if cname and cname in characteristics:
                entry["RequiredCharacteristics"].append(cname)
            else:
                unknown.append(slug_c)
        if unknown:
            print(f"  ! service {name}: unknown characteristics {unknown}", file=sys.stderr)

        # COMPATIBILITY INHERITANCE from the previously generated catalog:
        # pyhap's Service() auto-instantiates the REQUIRED characteristics at
        # runtime, so a required->optional demotion in Apple's metadata would
        # silently remove characteristics from existing accessories. Apple
        # occasionally does this (e.g. FirmwareRevision on AccessoryInformation)
        # while the HAP specification still lists them as required. Rule:
        #   - a characteristic that was Required stays Required;
        #   - a characteristic that was Optional stays Optional (allowed lists
        #     only ever grow).
        # Genuine removals must be done explicitly via a serviceOverride entry
        # in the manual overrides file.
        prev = existing_services.get(name)
        if prev and prev.get("UUID") == entry["UUID"]:
            # Required characteristics that were demoted to Optional by Apple
            # are re-promoted to Required.
            for c in prev.get("RequiredCharacteristics", []):
                if c not in characteristics or c in entry["RequiredCharacteristics"]:
                    continue
                entry["RequiredCharacteristics"].append(c)
                if c in entry["OptionalCharacteristics"]:
                    entry["OptionalCharacteristics"].remove(c)
            # Optional characteristics that were dropped entirely are re-added
            # (the allowed list only ever grows).
            for c in prev.get("OptionalCharacteristics", []):
                if c in characteristics and c not in entry["RequiredCharacteristics"] \
                        and c not in entry["OptionalCharacteristics"]:
                    entry["OptionalCharacteristics"].append(c)
        catalog[name] = entry

    for name, entry in manual.get("serviceOverrides", {}).items():
        catalog[name] = entry
    for name, entry in manual.get("serviceAdditions", {}).items():
        catalog[name] = entry

    return catalog


# ---------------------------------------------------------------------------
# Reporting / main
# ---------------------------------------------------------------------------


def diff_report(old, new, label):
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(k for k in set(new) & set(old) if new[k] != old[k])
    print(f"{label}: {len(new)} entries ({len(added)} added, {len(removed)} removed, "
          f"{len(changed)} changed)")
    for k in added:
        print(f"  + {k} ({new[k].get('UUID')})")
    for k in removed:
        print(f"  - {k} ({old[k].get('UUID')})")
    return added, removed, changed


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--plain-metadata", type=Path, default=PLAIN_METADATA_PATH,
                        help="Path to plain-metadata.config")
    parser.add_argument("--simulator-plist", type=Path, default=SIMULATOR_PLIST,
                        help="Path to the HomeKit Accessory Simulator default.metadata.plist")
    parser.add_argument("--manual-overrides", type=Path, default=MANUAL_OVERRIDES_FILE,
                        help="Path to hap_manual_overrides.json")
    parser.add_argument("--write", action="store_true",
                        help="Write the generated files (default is a dry-run report)")
    args = parser.parse_args()

    if not args.plain_metadata.exists():
        sys.exit(f"Primary metadata not found at {args.plain_metadata}. "
                 "This script must run on macOS.")

    manual = json.loads(args.manual_overrides.read_text())

    meta_chars, meta_services, _units, hap_properties = load_plain_metadata(args.plain_metadata)
    print(f"plain-metadata.config: {len(meta_chars)} characteristics, "
          f"{len(meta_services)} services")

    sim_chars = {}
    if args.simulator_plist.exists():
        sim_chars = load_simulator_plist(args.simulator_plist)
        print(f"simulator plist: {len(sim_chars)} characteristics (overlay enabled)")
    else:
        print("simulator plist: not found - using bitmask permissions and "
              "inherited ValidValues")

    existing_chars = json.loads(CHAR_OUT_FILE.read_text())
    existing_services = json.loads(SERVICE_OUT_FILE.read_text())

    # slug -> HAP-python name, used to resolve the characteristic references
    # inside service definitions.
    # Base slug -> name table from descriptions. Used for legacy slugs that
    # Apple still references in service definitions but no longer ships as
    # characteristic entries (e.g. "on" when the UUID 0x25 entry is now
    # described as "PowerState"). The FINAL names recorded during generation
    # (below) take precedence over this description-derived fallback.
    slug_to_name = {
        slug: to_hap_name(info["DefaultDescription"])
        for slug, info in meta_chars.items()
    }
    name_overrides = dict(manual.get("characteristicNameOverrides", {}))
    name_overrides["_slug_to_name"] = slug_to_name

    new_chars, slug_to_final = generate_characteristics(
        meta_chars, _units, hap_properties, sim_chars, manual, existing_chars,
        name_overrides)
    name_overrides["_slug_to_name"].update(slug_to_final)
    new_services = generate_services(
        meta_services, new_chars, manual, name_overrides, existing_services)

    print()
    diff_report(existing_chars, new_chars, "characteristics.json")
    print()
    diff_report(existing_services, new_services, "services.json")

    if args.write:
        CHAR_OUT_FILE.write_text(json.dumps(new_chars, indent=3, sort_keys=True) + "\n")
        SERVICE_OUT_FILE.write_text(json.dumps(new_services, indent=3, sort_keys=True) + "\n")
        print("\nWrote", CHAR_OUT_FILE, "and", SERVICE_OUT_FILE)
    else:
        print("\nDry run - nothing written. Re-run with --write to update the files.")


if __name__ == "__main__":
    main()
