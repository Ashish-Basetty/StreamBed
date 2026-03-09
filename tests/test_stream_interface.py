import asyncio

from shared.interfaces.stream_interface import StreamBedUDPSender, StreamBedUDPReceiver, StreamFrame


def make_frame():
    return StreamFrame(
        timestamp=123.45,
        frame=None,
        embedding=None,
        model_version="v1",
        source_device_id="device123",
    )


def test_udp_sender_receiver_cycle():
    async def inner():
        receiver = StreamBedUDPReceiver()
        # bind to port 0 to get an ephemeral port
        await receiver.listen("127.0.0.1", 0)
        port = receiver._transport.get_extra_info("socket").getsockname()[1]

        # start consumer task
        got = []

        async def consume():
            async for frame in receiver.receive_stream():
                got.append(frame)
                break

        consumer_task = asyncio.create_task(consume())

        sender = StreamBedUDPSender()
        await sender.connect("127.0.0.1", port)
        sent = await sender.send(make_frame())
        assert sent

        # give receiver a moment to process
        await asyncio.sleep(0.1)
        await sender.close()
        await receiver.stop()
        await consumer_task

        assert len(got) == 1
        assert got[0].timestamp == 123.45
        assert got[0].source_device_id == "device123"

    asyncio.run(inner())
