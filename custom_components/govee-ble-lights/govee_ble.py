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
import array

import bleak_retry_connector as brc
from bleak import BleakClient

_LOGGER = logging.getLogger(__name__)


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
    BLE_INTERFRAME_DELAY = 0.05  # Seconds delay between frames in multi-packet
    BLE_HANDLE_RETRY = 3  # Number of connection retry attempts
    BLE_TIMEOUT = 7  # Timeout in seconds for operations

    @staticmethod
    async def send_multi_packet(client: BleakClient, protocol_type, header_array, data):
        """
        Send data as a multi-frame packet stream.

        Args:
            client: Connected BleakClient.
            protocol_type: First byte of each frame (0x33 command / 0xAA request).
            header_array: Initial header bytes (usually 0x02 or 0x03).
            data: Payload bytes, chunked into 17-byte frames if needed.

        Frames are sent sequentially with a 50ms delay between them; see
        https://github.com/Jaano/govee_lights/commit/a9ded50ca6b341a30a02aaf22970f4b8be28d871#diff-cb5033302ec76b56b44c29678bc2d1f03472d762cae718fe31cb8d934eb447b7R161
        """

        result = []

        # Initialize the initial buffer (20 bytes total)
        header_length = len(header_array)
        header_offset = header_length + 4

        initial_buffer = array.array("B", [0] * 20)
        initial_buffer[0] = protocol_type
        initial_buffer[1] = 0
        initial_buffer[2] = 1
        initial_buffer[4 : 4 + header_length] = header_array

        # Create the additional buffer for overflow data
        additional_buffer = array.array("B", [0] * 20)
        additional_buffer[0] = protocol_type
        additional_buffer[1] = 255  # Flag for additional packet

        remaining_space = 14 - header_length + 1

        # Check if data fits in initial buffer
        if len(data) <= remaining_space:
            # Data fits - just copy it into the initial buffer
            initial_buffer[header_offset : header_offset + len(data)] = data
        else:
            # Data is too large - must chunk it
            excess = len(data) - remaining_space
            # Calculate number of 17-byte chunks needed
            chunks = excess // 17
            remainder = excess % 17

            # If there's a remainder, we need one more chunk
            if remainder > 0:
                chunks += 1
            else:
                # Edge case: exact division, still need to account for it
                remainder = 17

            # Copy first chunk into initial buffer
            initial_buffer[header_offset : header_offset + remaining_space] = data[
                0:remaining_space
            ]
            current_index = remaining_space

            # Create additional chunks for overflow data
            for i in range(1, chunks + 1):
                # Create a 17-byte chunk
                chunk = array.array("B", [0] * 17)
                chunk_size = remainder if i == chunks else 17
                chunk[0:chunk_size] = data[current_index : current_index + chunk_size]
                current_index += chunk_size

                # For the last chunk, add to additional buffer
                if i == chunks:
                    additional_buffer[2 : 2 + chunk_size] = chunk[0:chunk_size]
                else:
                    # For intermediate chunks, create a full packet buffer
                    chunk_buffer = array.array("B", [0] * 20)
                    chunk_buffer[0] = protocol_type
                    chunk_buffer[1] = i  # Sequence number for this chunk
                    chunk_buffer[2 : 2 + chunk_size] = chunk
                    chunk_buffer[19] = GoveeBLE.sign_payload(chunk_buffer[0:19])
                    result.append(chunk_buffer)

        # Calculate total packet count including additional buffer
        initial_buffer[3] = len(result) + 2
        initial_buffer[19] = GoveeBLE.sign_payload(initial_buffer[0:19])
        result.insert(0, initial_buffer)

        # Additional buffer for final overflow chunk
        additional_buffer[19] = GoveeBLE.sign_payload(additional_buffer[0:19])
        result.append(additional_buffer)

        # https://github.com/Jaano/govee_lights/commit/a9ded50ca6b341a30a02aaf22970f4b8be28d871#diff-cb5033302ec76b56b44c29678bc2d1f03472d762cae718fe31cb8d934eb447b7R161
        for i, r in enumerate(result):
            _LOGGER.debug(
                "Sending multi-packet frame %d/%d: %s",
                i + 1,
                len(result),
                r.tobytes().hex(),
            )
            await GoveeBLE.send_single_frame(client, r)
            await asyncio.sleep(0.05)

    @staticmethod
    async def send_keepalive_packet(client: BleakClient):
        """Send the minimal keepalive frame (0xAA + zero padding + checksum)."""

        # Start with the frame type byte
        frame = bytes([0xAA])

        # Pad frame data to 19 bytes (plus checksum makes 20 total)
        frame += bytes([0] * (19 - len(frame)))

        # Calculate the XOR checksum of all data bytes
        # This provides integrity verification for the packet
        checksum = 0
        for b in frame:
            checksum ^= b

        # Append the checksum byte to complete the frame
        frame += bytes([GoveeBLE.sign_payload(frame)])

        # Send the frame without expecting a response
        # Note: We pass frame directly to send_single_frame with no response
        await GoveeBLE.send_single_frame(client, frame, False)

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

        # Validate payload type and content
        if not isinstance(payload, bytes) and not (
            isinstance(payload, list) and all(isinstance(x, int) for x in payload)
        ):
            raise ValueError("Invalid payload")

        # Payload must not exceed 17 bytes (plus checksum)
        if len(payload) > 17:
            raise ValueError("Payload too long")

        # Convert command to single byte
        cmd = cmd & 0xFF
        # Convert payload to bytes if it's a list
        payload = bytes(payload)

        # Build the frame: frame type + command + payload
        # The frame type determines if the device will respond or execute
        frame = bytes([frame_type, cmd]) + bytes(payload)

        # Pad frame data to 19 bytes (plus checksum makes 20 total)
        frame += bytes([0] * (19 - len(frame)))

        # Calculate the XOR checksum of all data bytes
        # This provides integrity verification for the packet
        checksum = 0
        for b in frame:
            checksum ^= b

        # Append the signed checksum byte to complete the frame
        frame += bytes([GoveeBLE.sign_payload(frame)])

        # Send the frame with debug logging
        await GoveeBLE.send_single_frame(client, frame)

    @staticmethod
    async def set_segments_color(client: BleakClient, color, mask_lo, mask_hi):
        """
        Paint the segments selected by a bitmask with one color.

        ``mask_lo`` selects segments 1-8 (bit 0 = segment 1), ``mask_hi``
        segments 9-15 (bit 0 = segment 9). Repeated calls with different
        masks/colors build arbitrary patterns.

        Args:
            client: Connected BleakClient.
            color: (red, green, blue) tuple, 0-255.
            mask_lo: Segment mask for segments 1-8.
            mask_hi: Segment mask for segments 9-15.
        """
        red, green, blue = color
        payload = [
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
        ]
        await GoveeBLE.send_single_packet(client, GoveeBLE.LEDCommand.COLOR, payload)

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
    # Sends a single BLE data frame. log_frame indicates whether or not to log it.
    # Turn log_frame off when sending keepalive packets to prevent log spam.
    async def send_single_frame(client: BleakClient, frame, log_frame=True) -> None:
        """
        Write a pre-built frame to the control characteristic, reconnecting
        if disconnected (up to BLE_HANDLE_RETRY times). Expects a complete
        20-byte frame including the checksum byte.

        Args:
            client: Connected BleakClient.
            frame: Complete frame bytes to send.
            log_frame: Log the frame; disable for keepalive to reduce spam.
        """
        retry = 0
        # Retry connection if client is not connected
        while not client.is_connected:
            if retry >= GoveeBLE.BLE_HANDLE_RETRY:
                raise TimeoutError
            await client.connect()
            retry += 1

        # Log the frame if logging is enabled
        if log_frame:
            _LOGGER.debug("Writing frame: %s", bytes(frame).hex())

        # Write the frame to the control characteristic
        # The False parameter indicates we're not expecting a response
        await client.write_gatt_char(
            GoveeBLE.BLE_UUID_CONTROL_CHARACTERISTIC, frame, False
        )

    @staticmethod
    async def read_attribute(client: BleakClient, attribute: LEDCommand):
        """Read a GATT characteristic, reconnecting if disconnected."""
        retry = 0
        # Retry connection if client is not connected
        while not client.is_connected:
            if retry >= GoveeBLE.BLE_HANDLE_RETRY:
                raise TimeoutError
            await client.connect()
            retry += 1

        # Read the GATT characteristic
        return await client.read_gatt_char(attribute)

    @staticmethod
    async def create_connection(ble_device, identifier, hass) -> BleakClient:
        """Establish a BLE connection via bleak_retry_connector."""

        # Establish connection using bleak_retry_connector
        # This handles connection retries and error recovery automatically
        client = await brc.establish_connection(
            BleakClient, ble_device, identifier, max_attempts=GoveeBLE.BLE_HANDLE_RETRY
        )

        # Create a background task to keep the BLE connection active
        # This helps remove the delay when turning on/off lights
        return client

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
