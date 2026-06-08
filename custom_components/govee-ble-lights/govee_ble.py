"""
This file contains all functionality pertaining to Govee BLE lights, including data structures.

This module implements the complete protocol for communicating with Govee BLE LED lights.
The protocol is based on reverse-engineering and uses custom binary packet formats
rather than standard Bluetooth GATT characteristics.

The implementation handles:
- BLE connection management and keepalive
- Power control (on/off)
- Brightness control (with percentage support for newer models)
- Color control (RGB)
- Scene/effect playback (not supported yet)
- Multi-packet support for segmented light strips

Good reference: https://github.com/egold555/Govee-Reverse-Engineering/blob/master/Products/H6127.md
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
    This class is used to connect to and control Govee branded LED lights.

    The Govee BLE protocol uses custom packet formats that differ from standard
    Bluetooth GATT. Packets have:
    - A frame type byte
    - Command byte indicating the operation
    - Variable payload data
    - 19-byte data section (zero-padded)
    - XOR checksum byte for integrity verification

    The implementation:
    1. Establishes connections using bleak with retry logic
    2. Creates background keepalive tasks to prevent connection drops
    3. Sends commands via characteristic writes
    4. Handles notifications for state changes
    5. Supports multi-packet commands for segmented strips

    The class is stateless - all methods are static or take the client explicitly.
    """

    class LEDCommand(IntEnum):
        """
        Control command types for sending to Govee devices.

        Each byte value represents a different operation the device can perform:

        0x01 - Power: Turn device on (0x01) or off (0x00)
        0x04 - Brightness: Set brightness level
        0x05 - Color: Set RGB color for non-segmented devices
        0xA5 - Segment: Set color for specific segments on segmented strips
        """

        POWER = 0x01
        BRIGHTNESS = 0x04
        COLOR = 0x05
        SEGMENT = 0xA5

    class LEDMode(IntEnum):
        """
        Operation mode for color commands.

        Different modes tell the device how to interpret the color data:

        MANUAL (0x02)       - Direct RGB values for standard strips (H6172/H6173).
        SCENE_ACTIVATE (0x04) - Activate a previously streamed scene blob;
                                payload is [scene_id]. Paired with the 0xA3
                                multi-packet write that loaded the scene.
        SCENES (0x05)       - Built-in scene/effect playback.
        MICROPHONE (0x06)   - Microphone-controlled effects (not implemented).
        MANUAL_RGBIC (0x0D) - Solid-color set for RGBIC strips like H6178.
                              Same payload layout as MANUAL (R,G,B) but a
                              different mode byte. H6178 firmware silently
                              ignores writes that use MANUAL or SEGMENTS.
        SEGMENTS (0x15)     - Segment-specific color control for H617A family.
        """

        MANUAL = 0x02
        SCENE_ACTIVATE = 0x04
        SCENES = 0x05
        MICROPHONE = 0x06
        MANUAL_RGBIC = 0x0D
        SEGMENTS = 0x15

    class LEDFrameType(IntEnum):
        """
        Frame type identifiers for BLE packets.

        The first byte of every packet indicates whether it's a request or command:

        REQUEST (0xAA) - The device will respond with its current state
                      - Used for querying power, brightness, color

        COMMAND (0x33) - The device will execute the command
                       - Used for turning on/off, setting brightness/color

        The frame type is the first byte that determines device behavior.
        All subsequent bytes are the actual command/data payload.
        """

        REQUEST = 0xAA
        COMMAND = 0x33

    # UUIDs for Govee BLE characteristics
    # These are custom UUIDs used by Govee devices, not standard GATT
    BLE_UUID_STATUS_CHARACTERISTIC = "00010203-0405-0607-0809-0a0b0c0d2b10"
    BLE_UUID_CONTROL_CHARACTERISTIC = "00010203-0405-0607-0809-0a0b0c0d2b11"

    # Models that use segmented LED strips (multiple colors in one strip)
    # These devices require special multi-packet commands when controlling specific segments.
    # Ignore for now.
    BLE_SEGMENTED_MODELS = [
        "H6053",
        "H6072",
        "H6102",
        "H6199",
        "H617A",
        "H617C",
        "H617E",
        "H618C",
    ]

    # Models that expect brightness as percentage (0-100) instead of 0-255.
    # H6178 confirmed by btsnoop capture: official app sends 0x19/0x32/0x4b/0x64
    # for the 25/50/75/100 percent slider positions.
    BLE_PERCENT_MODELS = ["H6199", "H617A", "H617C", "H618C", "H6178"]

    # RGBIC families (each LED individually addressable) that use color mode
    # 0x0D for solid-color writes instead of MANUAL (0x02) or SEGMENTS (0x15).
    # These devices silently ACK frames in either of the other two modes but
    # never apply them; only mode 0x0D actually drives the LEDs.
    #
    # Color query layout for this family is also distinct: the request must be
    # 0xAA 0x05 [0x01] (i.e. ask for segment 1) and the reply is
    # 0xAA 0x05 0x0D R G B ... -- the existing COLOR notification parser then
    # reads payload[1..3] as RGB, no separate notification handling needed.
    #
    # Orthogonal to BLE_PERCENT_MODELS: a model can be RGBIC and percent
    # (H6178), RGBIC and raw, segmented and percent, etc.
    BLE_RGBIC_MODELS = ["H6178"]

    # BLE connection and packet timing parameters
    BLE_KEEPALIVE_INTERVAL = 1.0  # Seconds between keepalive packets
    BLE_INTERFRAME_DELAY = 0.05  # Seconds delay between frames in multi-packet
    BLE_HANDLE_RETRY = 3  # Number of connection retry attempts
    BLE_TIMEOUT = 7  # Timeout in seconds for operations

    @staticmethod
    async def send_multi_packet(client: BleakClient, protocol_type, header_array, data):
        """
        Creates and sends a multi-packed packet to a Govee device.

        Multi-packet support is required for segmented LED strips (models like H6199)
        that have multiple LED zones controlled independently. These devices receive
        commands in multiple packets that must be sent sequentially with small delays
        between them.

        Args:
            client: BleakClient instance connected to the Govee BLE device
            protocol_type: First byte of packet (typically 0x33 for commands or 0xAA for requests)
            header_array: Initial header bytes for the packet (usually 0x02 or 0x03)
            data: Payload bytes to send (will be chunked if too large)

        Returns:
            None

        The packet construction follows this structure:

        1. Create initial buffer (20 bytes):
           - Byte 0: protocol_type
           - Byte 1: packet sequence number (0 for initial, 255 for additional)
           - Byte 2: flags byte
           - Bytes 4-6: header_array data
           - Rest: padding and checksum

        2. If data exceeds remaining space in initial buffer:
           - Split data into chunks
           - Create additional packet(s) for overflow
           - Each chunk gets its own sequence number

        3. Calculate XOR checksum for each packet

        4. Send each packet sequentially with 50ms delay between them

        Note: The implementation references Jaano's govee_lights project for verification.
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
        """
        Creates, signs, and sends a complete BLE keepalive packet to maintain connection.

        Keepalive packets are essential for preventing BLE connection timeouts
        and ensuring responsive control. Without periodic keepalive messages,
        the device may drop the connection after a period of inactivity,
        causing delays when turning lights on/off.

        Args:
            client: BleakClient instance connected to the Govee BLE device

        Returns:
            None

        The keepalive packet format:

        - Byte 0: Frame type 0xAA (request type, no response expected)
        - Bytes 1-19: Zero-padded to 19 bytes
        - Byte 20: XOR checksum of all preceding bytes

        This is a minimal packet with no command data - just the frame type
        and checksum to keep the connection alive.
        """

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
        Creates, signs, and sends a complete BLE packet to a Govee device.

        This is the primary method for sending commands to control lights.
        It handles validation, packet construction, checksum calculation,
        and transmission via GATT characteristic write.

        Args:
            client: BleakClient instance connected to the Govee BLE device
            cmd: Command byte (LEDCommand enum value like POWER=0x01, BRIGHTNESS=0x04)
            payload: Data bytes for the command (bytes, list of ints, or empty for requests)
            frame_type: 0xAA for request (device responds) or 0x33 for command (device executes)
                Defaults to COMMAND for normal operation

        Raises:
            ValueError: If cmd is not an int, payload is invalid, or payload > 17 bytes

        Returns:
            None

        The packet structure:

        Byte 0: Frame type (REQUEST/COMMAND)
        Byte 1: Command type
        Bytes 2-19: Command payload (zero-padded)
        Byte 20: XOR checksum

        The frame_type parameter allows sending requests to query device state
        without triggering a command execution.
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
    def verify_frame(frame):
        """
        Check whether a BLE frame has a valid checksum.

        This method is used for verifying received frames from the device.
        It calculates the expected XOR checksum and compares it to the
        checksum byte appended to the frame data.

        Args:
            frame: Complete frame bytes including checksum byte at the end

        Returns:
            bool: True if checksum matches, False otherwise

        The verification process:
        1. Calculate XOR checksum of all bytes except the last
        2. Compare to the last byte (the stored checksum)
        3. Return True if they match, False otherwise

        This ensures packet integrity and helps filter out corrupted frames.
        """
        # Compare calculated checksum of frame (without final byte) to stored checksum
        return (
            GoveeBLE.sign_payload(frame[:-1]) == frame[-1]
        )  # Compare checksum of frame to calculated checksum

    @staticmethod
    def parse_frame(frame):
        """
        Validate and parse a BLE frame into its components.

        This method is used when receiving frames from the device to
        extract state information (power, brightness, color).

        Args:
            frame: Complete frame bytes received from the device

        Raises:
            ValueError: If frame is too short or has invalid checksum

        Returns:
            tuple: (head, cmd, payload) where:
                head: Frame type byte (0xAA for response/request, 0x33 for command)
                cmd: Command byte
                payload: Remaining bytes before checksum

        Example:
            >>> head, cmd, payload = GoveeBLE.parse_frame(frame)
            >>> if cmd == GoveeBLE.LEDCommand.POWER:
            >>>     state = payload[0] == 0x01  # True = on, False = off
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
        Sends a pre-made BLE frame to the Govee device via GATT write.

        This is the lowest-level transmission method used by other methods.
        It handles connection retries and optional logging of sent frames.

        Args:
            client: BleakClient instance connected to the Govee BLE device
            frame: Complete frame bytes to send (20 bytes including checksum)
            log_frame: Whether to log the sent frame (default True)
                Set to False for keepalive packets to reduce log verbosity

        Returns:
            None

        The method:
        1. Retries connection up to 3 times if client is disconnected
        2. Writes the frame to the control characteristic
        3. Logs the frame if logging is enabled

        Note: This method expects the frame to be pre-built with proper
        checksum. Do not call directly unless you understand the protocol.
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
        """
        Attempts to read a device attribute via GATT characteristic read.

        This method is used to read the value of a GATT characteristic
        on the Govee device.

        Args:
            client: BleakClient instance connected to the Govee BLE device
            attribute: LEDCommand type to read (POWER, BRIGHTNESS, COLOR, SEGMENT)

        Returns:
            The read attribute value or raises an exception

        Raises:
            Exception: If the read fails due to connection issues or
                      unsupported characteristic type

        Note: This method may not work for all device models as many
        Govee BLE characteristics are write-only.
        """
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
        """
        Attempts to create a connection handle for the BLE device.

        This method uses bleak_retry_connector to establish and maintain
        the BLE connection with automatic retry logic. It also creates a
        background keepalive task to prevent connection drops.

        Args:
            ble_device: BleakBluetoothDevice object for the Govee device
            identifier: Unique identifier for this device (used for reconnects)
            hass: HomeAssistant instance for creating background tasks

        Returns:
            BleakClient: Connected BleakClient instance

        The method:
        1. Uses bleak_retry_connector to establish connection
        2. Creates a background task for connection maintenance
        3. Returns the client for use in other operations

        The background keepalive task is crucial for responsive control.
        Without it, the device would drop connections after inactivity,
        causing delays when toggling lights.
        """

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
        Background task that ensures a light's BLE connection stays active.

        This method is called as a background task.
        It continuously sends keepalive packets to prevent connection drops
        and ensures responsive light control.

        Args:
            client: BleakClient instance to keep alive
            reconnect_callback: Optional coroutine to run after each reconnection.
                Re-registering BLE notifications here is critical because GATT
                subscriptions are lost when the underlying transport disconnects,
                causing the device to appear unresponsive even though the connection
                loop itself recovers successfully.

        Returns:
            None

        The keepalive loop:
        1. Waits 1 second (BLE_KEEPALIVE_INTERVAL)
        2. Ensures client is connected (reconnects if needed)
        3. Sends a keepalive packet
        4. Continues indefinitely until stopped (homeassistant shutdown or device removed)

        The try-except block prevents exceptions from stopping the loop,
        which would cause the connection to die.

        0xaa is a known documented header type to describe a keepalive packet.
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
