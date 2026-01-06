"""
This file contains all functionality pertaining to Govee BLE lights, including data structures.
"""

from enum import IntEnum
import array

class GoveeBLE:
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

    def prep_multi_packet(self, protocol_type, header_array, data):
        result = []

        # Initialize the initial buffer
        header_length = len(header_array)
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
            initial_buffer[header_length + 4:header_length + 4 + len(data)] = data
        else:
            excess = len(data) - remaining_space
            chunks = excess // 17
            remainder = excess % 17

            if remainder > 0:
                chunks += 1
            else:
                remainder = 17

            initial_buffer[header_length + 4:header_length + 4 + remaining_space] = data[0:remaining_space]
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
                    chunk_buffer[19] = self.sign_payload(chunk_buffer[0:19])
                    result.append(chunk_buffer)

        initial_buffer[3] = len(result) + 2
        initial_buffer[19] = self.sign_payload(initial_buffer[0:19])
        result.insert(0, initial_buffer)

        additional_buffer[19] = self.sign_payload(additional_buffer[0:19])
        result.append(additional_buffer)

        return result

    def prep_single_packet(self, cmd, payload):
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

        frame += bytes([checksum & 0xFF])
        return frame

    def sign_payload(self, data):
        checksum = 0
        for b in data:
            checksum ^= b
        return checksum & 0xFF
