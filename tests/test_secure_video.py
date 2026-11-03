"""Secure Video service and negotiation tests."""

import pyhap.secure_video as m


def test_hksv_services():
    c = m.RecordingCapabilities(1280, 720, 15, 0, 0, 0, 1)
    r, v, a = c.supported()
    assert m.parse_tlv(m.unb64(r))[1] == (4000).to_bytes(4, "little")
    assert m.parse_tlv(m.unb64(v))[1]
    assert m.parse_tlv(m.unb64(a))[1]

    class HDS:
        def prepare_session(self, key, salt, owner):
            assert key == b"k" * 32 and salt == b"s" * 32
            self.owner = owner
            return 1234, b"a" * 32

        def close_owner(self, owner):
            self.closed = owner

    class Session:
        shared_key = b"k" * 32
        closed = False

        def add_close_callback(self, callback):
            self.close_callback = callback

    h = HDS()
    svc = object.__new__(m.HKSVServices)
    svc.hds = h
    svc._responses = {}
    session = Session()
    request = m.b64(m.tlv((1, b"\0"), (2, b"\0"), (3, b"s" * 32)))
    response = svc._setup_transport(request, session)
    fields = m.parse_tlv(m.unb64(response))
    assert (
        fields[1] == b"\0"
        and m.parse_tlv(fields[2])[1] == (1234).to_bytes(2, "little")
        and fields[3] == b"a" * 32
    )
    assert svc._get_transport(session) == response
    session.close_callback()
    assert h.closed is session and svc._get_transport(session) == ""
    try:
        svc._setup_transport(m.b64(m.tlv((1, b"\0"), (2, b"\0"), (3, b"x"))), session)
    except ValueError:
        pass
    else:
        raise AssertionError("accepted bad salt")

    svc = m.HKSVServices(h, c, lambda value: None)
    assert len(svc.recording.characteristics) == 6
    assert len(svc.transport.characteristics) == 3
    assert svc.setup.session_setter_callback(request, session)


def test_selected_recording_config():
    c = m.RecordingCapabilities(1280, 720, 15, 0, 0, 0, 1)
    u = m._u
    t = m.tlv
    container = t((1, b"\0"), (2, t((1, u(4000, 4)))))
    recording = t((1, u(4000, 4)), (2, u(1, 8)), (3, container))
    vp = t((1, b"\0"), (2, b"\0"), (3, u(1000, 4)), (4, u(4000, 4)))
    attrs = t((1, u(1280, 2)), (2, u(720, 2)), (3, u(15, 1)))
    video = t((1, b"\0"), (2, vp), (3, attrs))
    ap = t((1, b"\1"), (2, b"\0"), (3, b"\1"), (4, u(32, 4)))
    audio = t((1, b"\0"), (2, ap))
    data = t((1, recording), (2, video), (3, audio))
    assert m.parse_selected(data, c)["video_bitrate_kbps"] == 1000
    incompatible = m.RecordingCapabilities(1920, 1080, 15, 0, 0, 0, 1)
    try:
        m.parse_selected(data, incompatible)
    except ValueError:
        pass
    else:
        raise AssertionError("unsupported resolution accepted")


def test_multiple_advertised_resolutions_and_selections():
    """TLV lists use separators, and only advertised tuples are accepted."""
    import pytest

    u, t = m._u, m.tlv
    resolutions = ((1280, 720, 15), (1280, 720, 30), (1920, 1080, 15), (1920, 1080, 30))
    caps = m.RecordingCapabilities(
        1280,
        720,
        15,
        0,
        0,
        0,
        1,
        resolutions=resolutions,
        profiles=(0, 1),
        levels=(0, 2),
    )
    raw = m.unb64(caps.supported()[1])
    codec = m.parse_tlv(raw)[1]
    # Four separated ATTRIBUTES records, as required by TLV8 list encoding.
    attrs = [t((1, u(w, 2)), (2, u(h, 2)), (3, u(fps, 1))) for w, h, fps in resolutions]
    assert b"\x00\x00".join(t((3, item)) for item in attrs) in codec
    recording = t(
        (1, u(4000, 4)), (2, u(1, 8)), (3, t((1, b"\0"), (2, t((1, u(4000, 4))))))
    )
    vp = t((1, b"\x01"), (2, b"\x02"), (3, u(1000, 4)), (4, u(4000, 4)))
    ap = t((1, b"\x01"), (2, b"\x00"), (3, b"\x01"), (4, u(32, 4)))
    audio = t((1, b"\0"), (2, ap))

    def selection(w, h, fps):
        video = t(
            (1, b"\0"), (2, vp), (3, t((1, u(w, 2)), (2, u(h, 2)), (3, u(fps, 1))))
        )
        return t((1, recording), (2, video), (3, audio))

    for resolution in resolutions:
        assert m.parse_selected(selection(*resolution), caps)["width"] == resolution[0]
    with pytest.raises(ValueError):
        m.parse_selected(selection(1920, 1080, 24), caps)
