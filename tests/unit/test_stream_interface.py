import asyncio

import pytest

from shared.interfaces.stream_interface import StreamBedUDPSender, StreamBedUDPReceiver, StreamFrame

pytestmark = pytest.mark.unit


def make_frame():
    return StreamFrame(
        timestamp=123.45,
        frame=None,
        embedding=None,
        model_version="v1",
        source_device_id="device123",
        frame_interleaving_rate=30.0,
    )


def test_udp_sender_receiver_cycle():
    async def inner():
        receiver = StreamBedUDPReceiver()
        await receiver.listen("127.0.0.1", 0)
        port = receiver.get_local_port()

        sender = StreamBedUDPSender()
        await sender.connect("127.0.0.1", port)
        sent = await sender.send(make_frame())
        assert sent

        frame = await receiver.recv_one(timeout=2.0)
        await sender.close()
        await receiver.stop()

        assert frame is not None
        assert frame.timestamp == 123.45
        assert frame.source_device_id == "device123"
        assert frame.frame_interleaving_rate == 30.0

    asyncio.run(inner())
