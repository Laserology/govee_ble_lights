"""
This file contains all functionality pertaining to Govee BLE lights, including data structures.
"""

from enum import IntEnum
import array

from bleak import BleakClient
import bleak_retry_connector as brc

class GoveeBLE(object):
    """
    This class is used to connect to and control Govee branded BLE LED lights.
    """

    class LEDCommand(IntEnum):
        """ A control command packet's type. """
        POWER = 0x01
        BRIGHTNESS = 0x04
        COLOR = 0x05

    class LEDMode(IntEnum):
        """
        The mode in which a color change happens in. Only manual is supported.
        """
        MANUAL = 0x02
        MICROPHONE = 0x06
        SCENES = 0x05
        SEGMENTS = 0x15

    UUID_CONTROL_CHARACTERISTIC = '00010203-0405-0607-0809-0a0b0c0d2b11'
    SEGMENTED_MODELS = ['H6053', 'H6072', 'H6102', 'H6199', 'H617A', 'H617C']
    PERCENT_MODELS = ['H617A', 'H617C']

    @staticmethod
    def send_multi_packet(client, protocol_type, header_array, data):
        """
        Creates a multi-packed packet.
        """

        result = []

        # Initialize the initial buffer
        header_length = len(header_array)
        header_offset = header_length + 4

        initial_buffer = array.array('B', [0] * 20)
        initial_buffer[0] = protocol_type
        initial_buffer[1] = 0
        initial_buffer[2] = 1
        initial_buffer[4:4+header_length] = header_array

        # Create the additional buffer
        additional_buffer = array.array('B', [0] * 20)
        additional_buffer[0] = protocol_type
        additional_buffer[1] = 255

        remaining_space = 14 - header_length + 1

        if len(data) <= remaining_space:
            initial_buffer[header_offset:header_offset + len(data)] = data
        else:
            excess = len(data) - remaining_space
            chunks = excess // 17
            remainder = excess % 17

            if remainder > 0:
                chunks += 1
            else:
                remainder = 17

            initial_buffer[header_offset:header_offset + remaining_space] = data[0:remaining_space]
            current_index = remaining_space

            for i in range(1, chunks + 1):
                chunk = array.array('B', [0] * 17)
                chunk_size = remainder if i == chunks else 17
                chunk[0:chunk_size] = data[current_index:current_index + chunk_size]
                current_index += chunk_size

                if i == chunks:
                    additional_buffer[2:2 + chunk_size] = chunk[0:chunk_size]
                else:
                    chunk_buffer = array.array('B', [0] * 20)
                    chunk_buffer[0] = protocol_type
                    chunk_buffer[1] = i
                    chunk_buffer[2:2+chunk_size] = chunk
                    chunk_buffer[19] = GoveeBLE.sign_payload(chunk_buffer[0:19])
                    result.append(chunk_buffer)

        initial_buffer[3] = len(result) + 2
        initial_buffer[19] = GoveeBLE.sign_payload(initial_buffer[0:19])
        result.insert(0, initial_buffer)

        additional_buffer[19] = GoveeBLE.sign_payload(additional_buffer[0:19])
        result.append(additional_buffer)

        for r in result:
            GoveeBLE.send_single_frame(client, r)

    @staticmethod
    def send_single_packet(client, cmd, payload):
        """
        Creates, signs, and sends a complete BLE packet to the device.
        Functions according to the input command and payload.
        """

        if not isinstance(cmd, int):
            raise ValueError('Invalid command')
        if not isinstance(payload, bytes) and not (
                isinstance(payload, list) and all(isinstance(x, int) for x in payload)):
            raise ValueError('Invalid payload')
        if len(payload) > 17:
            raise ValueError('Payload too long')

        cmd = cmd & 0xFF
        payload = bytes(payload)

        frame = bytes([0x33, cmd]) + bytes(payload)
        # pad frame data to 19 bytes (plus checksum)
        frame += bytes([0] * (19 - len(frame)))

        # The checksum is calculated by XORing all data bytes
        checksum = 0
        for b in frame:
            checksum ^= b

        frame += bytes([GoveeBLE.sign_payload(frame)])

        GoveeBLE.send_single_frame(client, frame)

    @staticmethod
    async def send_single_frame(client, frame):
        """
        Sends a pre-made BLE frame to the device.v
        """
        client.write_gatt_char(GoveeBLE.UUID_CONTROL_CHARACTERISTIC, frame, False)

    @staticmethod
    async def connect_to(device, identifier):
        """" This method connects to and returns a handle for the target BLE device. """
        for _ in range(3):
            try:
                return await brc.establish_connection(BleakClient, device, identifier)
            except:
                continue

    @staticmethod
    def sign_payload(data):
        """ 'Signs' a payload. Not sure what it does. """
        checksum = 0
        for b in data:
            checksum ^= b
        return checksum & 0xFF
