"""Integration tests: real HAP clients against a running AccessoryDriver.

These tests start an ``AccessoryDriver`` in-process on a loopback port and
drive it with :class:`pyhap.client.HAPClient` instances that perform the
real pair-verify handshake and speak the encrypted HAP transport, covering
standalone and bridged accessories, the setter/getter callbacks and the
EVENT notification paths end to end.
"""
