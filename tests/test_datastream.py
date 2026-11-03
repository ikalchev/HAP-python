"""HomeKit data stream transport tests."""

import asyncio

import pytest

import pyhap.datastream as h


async def fragments(owner, stream_id):
    yield b"init", False
    yield b"video", True


async def run():
    server = h.HDSServer(fragments)
    port = await server.start("127.0.0.1")
    try:
        secret, controller_salt = b"x" * 32, b"y" * 32
        owner = object()
        _, accessory_salt = server.prepare_session(secret, controller_salt, owner)
        outgoing, incoming = h.derive_keys(secret, controller_salt, accessory_salt)
        tx, rx = h.HDSCipher(incoming), h.HDSCipher(outgoing)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)

        async def send(header, body):
            frame = tx.encrypt(h.encode_message(header, body))
            # Deliberately split framing across TCP writes.
            writer.write(frame[:3])
            await writer.drain()
            writer.write(frame[3:])
            await writer.drain()

        async def read():
            return h.decode_message(
                rx.decrypt(await asyncio.wait_for(server._read_frame(reader), 2))
            )

        await send({"protocol": "control", "request": "hello", "id": 1}, {})
        header, body = await read()
        assert header == {
            "protocol": "control",
            "response": "hello",
            "id": 1,
            "status": 0,
        }
        await send(
            {"protocol": "dataSend", "request": "open", "id": 2},
            {"streamId": 42, "type": "ipcamera.recording", "target": "controller"},
        )
        header, body = await read()
        assert header["status"] == 0 and body["status"] == 0
        _, first = await read()
        _, last = await read()
        assert first["packets"][0]["metadata"]["dataType"] == "mediaInitialization"
        assert last["packets"][0]["metadata"]["dataType"] == "mediaFragment"
        assert last["endOfStream"] is True
        await send(
            {"protocol": "dataSend", "event": "ack"},
            {"streamId": 42, "endOfStream": True},
        )
        close_header, close_body = await read()
        assert close_header == {"protocol": "dataSend", "event": "close"}
        assert close_body == {"streamId": 42, "reason": 0}
        await asyncio.sleep(0)
        server.close_owner(owner)
        try:
            assert await asyncio.wait_for(reader.read(), 2) == b""
        except ConnectionResetError:
            pass
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass

    finally:
        await server.close()


def test_hds_session_and_recording(request):
    try:
        request.getfixturevalue("socket_enabled")
    except pytest.FixtureLookupError:
        pass
    asyncio.run(run())


def test_opack_reference_vectors():
    # Wire bytes from the HAP-NodeJS tag definitions, including reference
    # indices over previously decoded primitive values.
    assert h.opack_decode(bytes.fromhex("d243666f6fa0")) == ["foo", "foo"]
    assert h.opack_decode(bytes.fromhex("d2302aa0")) == [42, 42]
    assert h.opack_decode(bytes.fromhex("d4010843616263a0")) == [True, 0, "abc", "abc"]
    assert h.opack_decode(h.opack_encode(39)) == 39


def test_early_ack_and_final_ack(request):
    try:
        request.getfixturevalue("socket_enabled")
    except pytest.FixtureLookupError:
        pass

    async def scenario():
        gate = asyncio.Event()

        async def delayed(owner, stream_id):
            yield b"init", False
            await gate.wait()
            yield b"last", True

        server = h.HDSServer(delayed)
        port = await server.start("127.0.0.1")
        writer = None
        try:
            key, salt = b"x" * 32, b"y" * 32
            _, server_salt = server.prepare_session(key, salt, object())
            out, incoming = h.derive_keys(key, salt, server_salt)
            tx, rx = h.HDSCipher(incoming), h.HDSCipher(out)
            reader, writer = await asyncio.open_connection("127.0.0.1", port)

            async def send(header, body):
                writer.write(tx.encrypt(h.encode_message(header, body)))
                await writer.drain()

            async def read():
                return h.decode_message(
                    rx.decrypt(await asyncio.wait_for(server._read_frame(reader), 2))
                )

            await send({"protocol": "control", "request": "hello", "id": 1}, {})
            await read()
            await send(
                {"protocol": "dataSend", "request": "open", "id": 2},
                {"streamId": 42, "type": "ipcamera.recording", "target": "controller"},
            )
            await read()
            await read()
            await send(
                {"protocol": "dataSend", "event": "ack"},
                {"streamId": 42, "endOfStream": True},
            )
            await send(
                {"protocol": "dataSend", "request": "open", "id": 3},
                {"streamId": 43, "type": "ipcamera.recording", "target": "controller"},
            )
            header, body = await read()
            assert header["status"] == 6 and body["status"] == 2
            gate.set()
            _, body = await read()
            assert body["endOfStream"] is True
            await send(
                {"protocol": "dataSend", "event": "ack"},
                {"streamId": 42, "endOfStream": True},
            )
            header, body = await read()
            assert header["event"] == "close" and body["reason"] == 0
        finally:
            if writer:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionError:
                    pass
            await server.close()

    asyncio.run(scenario())


def test_node_crypto_frame_vectors():
    """Match independent Node crypto vectors for the first two frame nonces."""
    cipher = h.HDSCipher(bytes(range(32)))
    assert (
        cipher.encrypt(b"hello").hex()
        == "0100000570dd2e5dc27e534961aacd3db01b371b9cce2cce44"
    )
    assert (
        cipher.encrypt(b"world").hex()
        == "01000005e8388133d19874b6f17a0f3e42721a10a82e232da1"
    )
