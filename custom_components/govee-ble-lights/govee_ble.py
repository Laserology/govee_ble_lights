"""
Govee BLE protocol and connection handling.

Packets are 20 bytes: frame type, command, zero-padded data, then an XOR
checksum. Model behavior (segmented/percentage) is described in config.json;
see models.py.

Reference: https://github.com/egold555/Govee-Reverse-Engineering/blob/master/Products/H6127.md
"""

from enum import IntEnum
import asyncio
import logging
import time
from weakref import WeakKeyDictionary

import bleak_retry_connector as brc
from bleak import BleakClient

_LOGGER = logging.getLogger(__name__)

# Per-client transport state. The write lock serializes every packet on a
# connection - a whole multi-packet frame is sent under one lock hold so the
# keepalive (or a racing service call) can never interleave between its
# writes, and ``last_write`` lets the keepalive loop skip pings while the
# link is demonstrably active.
_client_transport: WeakKeyDictionary = WeakKeyDictionary()


class GoveeBLE:
    """
    Govee BLE protocol: connection management, keepalive, and packet
    construction/parsing. Stateless - all methods are static or take the
    client explicitly.
    """

    class LEDCommand(IntEnum):
        """Command byte values: power, brightness, color, segment."""

        POWER = 0x01
        BRIGHTNESS = 0x04
        COLOR = 0x05
        SEGMENT = 0xA5

    class LEDMode(IntEnum):
        """Color command modes (mode byte after the command)."""

        MANUAL = 0x02
        MICROPHONE = 0x06
        SCENES = 0x05
        SEGMENTS = 0x15

    class LEDFrameType(IntEnum):
        """First byte of a packet: REQUEST (device responds) or COMMAND."""

        REQUEST = 0xAA
        COMMAND = 0x33

    # UUIDs for Govee BLE characteristics
    # These are custom UUIDs used by Govee devices, not standard GATT
    BLE_UUID_STATUS_CHARACTERISTIC = "00010203-0405-0607-0809-0a0b0c0d2b10"
    BLE_UUID_CONTROL_CHARACTERISTIC = "00010203-0405-0607-0809-0a0b0c0d2b11"

    # BLE connection and packet timing parameters
    BLE_KEEPALIVE_INTERVAL = 1.0  # Seconds between keepalive packets
    BLE_HANDLE_RETRY = 3  # Number of connection retry attempts

    @staticmethod
    def _transport_for(client: BleakClient) -> dict:
        """Return the per-client lock + last-write-time dict (created lazily)."""
        transport = _client_transport.get(client)
        if transport is None:
            transport = {"lock": asyncio.Lock(), "last_write": 0.0}
            _client_transport[client] = transport
        return transport

    @staticmethod
    def keepalive_due(client: BleakClient) -> bool:
        """True when no packet was written recently, so a ping is worthwhile.

        During a fade or animation the link is being written to constantly;
        pinging then only adds traffic and risks stalling a frame, so the
        keepalive loop skips it.
        """
        transport = GoveeBLE._transport_for(client)
        return (
            time.monotonic() - transport["last_write"]
            >= GoveeBLE.BLE_KEEPALIVE_INTERVAL
        )

    @staticmethod
    async def send_keepalive_packet(client: BleakClient):
        """Send the minimal keepalive frame (REQUEST + zero payload)."""
        await GoveeBLE.send_single_frame(
            client,
            GoveeBLE.build_packet(GoveeBLE.LEDFrameType.REQUEST, 0, []),
            False,
        )

    @staticmethod
    async def send_single_packet(
        client: BleakClient, cmd, payload, frame_type=LEDFrameType.COMMAND
    ):
        """
        Build and send a single command/request packet: frame_type, cmd,
        payload (zero-padded to 19 bytes), then the XOR checksum byte.

        Args:
            client: Connected BleakClient.
            cmd: Command byte (LEDCommand value).
            payload: Data bytes (list/bytes, up to 17 bytes; empty for requests).
            frame_type: REQUEST for state queries, COMMAND otherwise.

        Raises:
            ValueError: If cmd or payload is invalid.
        """
        # Validate command is an integer
        if not isinstance(cmd, int):
            raise ValueError("Invalid command")

        await GoveeBLE.send_single_frame(
            client, GoveeBLE.build_packet(frame_type, cmd, payload)
        )

    @staticmethod
    def build_packet(frame_type, cmd, payload) -> bytes:
        """Build a signed 20-byte packet: frame_type, cmd, zero-padded
        payload, then the XOR checksum byte. Raises ValueError for oversized
        payloads.
        """
        if not isinstance(payload, bytes) and not (
            isinstance(payload, list) and all(isinstance(x, int) for x in payload)
        ):
            raise ValueError("Invalid payload")
        if len(payload) > 17:
            raise ValueError("Payload too long")

        frame = bytes([frame_type & 0xFF, cmd & 0xFF]) + bytes(payload)
        frame += bytes([0] * (19 - len(frame)))
        return frame + bytes([GoveeBLE.sign_payload(frame)])

    @staticmethod
    def build_segment_packet(color, mask_lo, mask_hi) -> bytes:
        """Build the packet painting the given segment mask with one color."""
        red, green, blue = color
        return GoveeBLE.build_packet(
            GoveeBLE.LEDFrameType.COMMAND,
            GoveeBLE.LEDCommand.COLOR,
            [
                GoveeBLE.LEDMode.SEGMENTS,
                0x01,  # Segment color mode
                red,
                green,
                blue,
                0x00,
                0x00,
                0x00,
                0x00,
                0x00,
                mask_lo,
                mask_hi,
            ],
        )

    @staticmethod
    def build_color_packet(red, green, blue) -> bytes:
        """Build the packet setting a solid color on a non-segmented model."""
        return GoveeBLE.build_packet(
            GoveeBLE.LEDFrameType.COMMAND,
            GoveeBLE.LEDCommand.COLOR,
            [GoveeBLE.LEDMode.MANUAL, red, green, blue],
        )

    @staticmethod
    async def send_writes(client: BleakClient, frames: list, log_frame=True) -> None:
        """Send a set of pre-built frames as one atomic visual update.

        All frames are written under a single lock hold, so another task
        (keepalive ping, a racing service call) cannot interleave between the
        packets that make up one rendering frame, and the device sees the
        update as a complete, ordered unit.

        Args:
            client: Connected BleakClient.
            frames: Complete 20-byte frames to send, in order.
            log_frame: Log each frame; disable to reduce spam.
        """
        async with GoveeBLE._transport_for(client)["lock"]:
            for frame in frames:
                await GoveeBLE._write_frame(client, frame, log_frame)

    @staticmethod
    def verify_frame(frame):
        """Return True when the frame's XOR checksum byte is valid."""
        # Compare calculated checksum of frame (without final byte) to stored checksum
        return (
            GoveeBLE.sign_payload(frame[:-1]) == frame[-1]
        )  # Compare checksum of frame to calculated checksum

    @staticmethod
    def parse_frame(frame):
        """
        Validate a frame and return (head, cmd, payload).

        Raises:
            ValueError: If the frame is too short or has a bad checksum.
        """
        # Validate frame length and checksum before parsing
        if len(frame) < 3 or not GoveeBLE.verify_frame(frame):
            raise ValueError("Invalid frame")

        # Extract components from the validated frame
        head = frame[0]  # Frame type
        cmd = frame[1]  # Command type
        payload = frame[2:-1]  # Data payload (excluding checksum)
        return head, cmd, payload

    @staticmethod
    async def send_single_frame(client: BleakClient, frame, log_frame=True) -> None:
        """
        Write one pre-built 20-byte frame to the control characteristic,
        serialized with all other writes on this connection.

        Args:
            client: Connected BleakClient.
            frame: Complete 20-byte frame including the checksum byte.
            log_frame: Log the frame; disable for keepalive to reduce spam.
        """
        async with GoveeBLE._transport_for(client)["lock"]:
            await GoveeBLE._write_frame(client, frame, log_frame)

    @staticmethod
    async def _write_frame(client: BleakClient, frame, log_frame: bool) -> None:
        """Write one frame without taking the write lock; reconnect first if
        disconnected (up to BLE_HANDLE_RETRY times). Update the transport's
        last-write timestamp so the keepalive loop knows the link is active.
        """
        retry = 0
        while not client.is_connected:
            if retry >= GoveeBLE.BLE_HANDLE_RETRY:
                raise TimeoutError
            await client.connect()
            retry += 1

        if log_frame:
            _LOGGER.debug("Writing frame: %s", bytes(frame).hex())

        # False = write-without-response (no GATT round trip to wait for)
        await client.write_gatt_char(
            GoveeBLE.BLE_UUID_CONTROL_CHARACTERISTIC, frame, False
        )
        GoveeBLE._transport_for(client)["last_write"] = time.monotonic()

    @staticmethod
    async def create_connection(ble_device, identifier) -> BleakClient:
        """Establish a BLE connection via bleak_retry_connector (handles
        retries and recovery). The caller keeps it alive with a keepalive
        task (see ``ensure_connection``)."""
        return await brc.establish_connection(
            BleakClient, ble_device, identifier, max_attempts=GoveeBLE.BLE_HANDLE_RETRY
        )

    @staticmethod
    async def ensure_connection(client: BleakClient, reconnect_callback=None) -> None:
        """
        Background keepalive loop: every BLE_KEEPALIVE_INTERVAL, reconnect if
        needed and send a keepalive packet. reconnect_callback restores GATT
        notifications after reconnects (subscriptions are lost on disconnect).
        """

        # Loop forever as a background task
        while True:
            # Delay to avoid the loop spamming BLE packets
            await asyncio.sleep(GoveeBLE.BLE_KEEPALIVE_INTERVAL)

            # Keep inside try block to avoid the loop dying
            try:
                # Ensure client is connected
                if not client.is_connected:
                    await client.connect()

                    # Re-register BLE notifications after reconnection.
                    # GATT subscriptions are lost when the underlying transport
                    # disconnects, so this is critical for keeping state updates flowing.
                    if reconnect_callback is not None:
                        try:
                            await reconnect_callback()
                        except Exception as cb_err:
                            _LOGGER.debug("Reconnect callback failed: %s", cb_err)

                # Skip the ping while writes are flowing (fades/animations);
                # the connection is obviously alive and a ping could stall a
                # frame mid-send.
                if not GoveeBLE.keepalive_due(client):
                    continue

                # Send data packet to keep the connection alive
                await GoveeBLE.send_keepalive_packet(client)
            except Exception:
                # Catch any exception and continue the loop
                # This prevents crashes if connection temporarily fails
                continue

    @staticmethod
    def sign_payload(data):
        """'Signs' a payload. Not sure what it does."""
        checksum = 0
        for b in data:
            checksum ^= b
        return checksum & 0xFF
