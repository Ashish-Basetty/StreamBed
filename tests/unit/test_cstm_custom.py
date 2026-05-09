"""StreamBedUDPSender.send_custom round-trips through the receiver's
custom-payload queue.

Verifies StreamBed's user-facing extension point: any application can
ship arbitrary bytes via send_custom(payload, reliable=...) and the
receiver hands them back via recv_custom() with the originating tag so
the app can tell apart lossy (CSTL) vs reliable (CSTR) deliveries.
"""
import asyncio
import os

import pytest

from shared.interfaces.stream_interface import (
    StreamBedUDPReceiver,
    StreamBedUDPSender,
)
from shared.stream_chunks import (
    CSTM_LOSSY_MAGIC,
    CSTM_RELIABLE_MAGIC,
    CHUNK_SIZE,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_send_custom_reliable_roundtrips_with_csmr_tag():
    receiver = StreamBedUDPReceiver()
    await receiver.listen("127.0.0.1", 0)
    port = receiver.get_local_port()

    sender = StreamBedUDPSender()
    await sender.connect("127.0.0.1", port)

    payload = b"advice-payload-bytes" + os.urandom(64)
    await sender.send_custom(payload, reliable=True)

    got = await asyncio.wait_for(receiver.recv_custom(), timeout=2.0)
    assert got is not None
    tag, body = got
    assert tag == CSTM_RELIABLE_MAGIC
    assert body == payload

    await sender.close()
    await receiver.stop()


@pytest.mark.asyncio
async def test_send_custom_lossy_carries_cstl_tag():
    receiver = StreamBedUDPReceiver()
    await receiver.listen("127.0.0.1", 0)
    port = receiver.get_local_port()

    sender = StreamBedUDPSender()
    await sender.connect("127.0.0.1", port)

    payload = b"bulk-telemetry"
    await sender.send_custom(payload, reliable=False)

    got = await asyncio.wait_for(receiver.recv_custom(), timeout=2.0)
    assert got is not None
    tag, body = got
    assert tag == CSTM_LOSSY_MAGIC
    assert body == payload

    await sender.close()
    await receiver.stop()


@pytest.mark.asyncio
async def test_send_custom_chunks_large_payload():
    """Payloads larger than one chunk are reassembled byte-for-byte."""
    receiver = StreamBedUDPReceiver()
    await receiver.listen("127.0.0.1", 0)
    port = receiver.get_local_port()

    sender = StreamBedUDPSender()
    await sender.connect("127.0.0.1", port)

    payload = os.urandom(CHUNK_SIZE * 4 + 173)  # forces multi-chunk
    await sender.send_custom(payload, reliable=True)

    got = await asyncio.wait_for(receiver.recv_custom(), timeout=3.0)
    assert got is not None
    tag, body = got
    assert tag == CSTM_RELIABLE_MAGIC
    assert body == payload

    await sender.close()
    await receiver.stop()


@pytest.mark.asyncio
async def test_custom_queue_is_separate_from_streamframe_queue():
    """CSTM payloads must not pollute the StreamFrame queue and vice versa."""
    receiver = StreamBedUDPReceiver()
    await receiver.listen("127.0.0.1", 0)
    port = receiver.get_local_port()

    sender = StreamBedUDPSender()
    await sender.connect("127.0.0.1", port)

    await sender.send_custom(b"only-custom", reliable=True)

    # Custom queue gets the message...
    custom = await asyncio.wait_for(receiver.recv_custom(), timeout=2.0)
    assert custom is not None and custom[1] == b"only-custom"

    # ...and StreamFrame queue stays empty.
    assert receiver.queue_size() == 0

    await sender.close()
    await receiver.stop()
