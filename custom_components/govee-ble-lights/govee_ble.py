"""
The core bluetooth stack for Govee BLE LEDs.
This file contains data structures and methods representing BLE data packets.
"""

# Import core libraries.
from enum import IntEnum
import array

# Import bluetooth libraries.
from bleak import BleakClient, BleakCharacteristicNotFoundError
import bleak_retry_connector as brc

class GoveeBLE:
    """ Global utilities used multiple times throughout the project. """

    UUID_CONTROL_CHARACTERISTIC = '00010203-0405-0607-0809-0a0b0c0d2b11'

    class LEDCommand(IntEnum):
        """ A control command packet's type. """
        POWER = 0x01
        BRIGHTNESS = 0x04
        COLOR = 0x05

    class LEDMode(IntEnum):
        """ The mode in which a color change happens in. Currently only manual is supported. """
        MANUAL = 0x02
        MICROPHONE = 0x06
        SCENES = 0x05
        SEGMENTS = 0x15

    async def send_single(self, ble_device, unique_id, cmd, payload, value: bool) -> None:
        """ Single-line method to send single-packet signals. """
        GoveeBLE.send_command(self, ble_device, unique_id, self.prepare_packet(cmd, payload), value)

    async def send_command(self, ble_device, unique_id, packet: bytes, value: bool) -> None:
        """ Central function to send a command over bluetooth using the bleak client. """
        # Maybe show an error when a connection can't be established?
        # To-Do: Implement catching more exception types.
        for _ in range(3):
            try:
                client = await brc.establish_connection(BleakClient, ble_device, unique_id)
                await client.write_gatt_char(self.UUID_CONTROL_CHARACTERISTIC, packet, value)
                break
            except BleakCharacteristicNotFoundError:
                continue
            # Remove this eventually
            # We will seperate exceptions into known categories to give better feedback.
            except Exception:
                continue

    def prepare_multi_packet(self, protocol_type, header_array, data) -> array:
        """
        Prepares a multi part BLE data packet with the given input.
        """

        result = []

        # Initialize the initial buffer
        header_length = len(header_array)
        header_offset = header_length + 4

        initial_buffer = array.array('B', [0] * 20)
        initial_buffer[0] = protocol_type
        initial_buffer[1] = 0
        initial_buffer[2] = 1
        initial_buffer[4:header_offset] = header_array

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
                    chunk_buffer[19] = self.sign_payload(chunk_buffer[0:19])
                    result.append(chunk_buffer)

        initial_buffer[3] = len(result) + 2
        initial_buffer[19] = self.sign_payload(initial_buffer[0:19])
        result.insert(0, initial_buffer)

        additional_buffer[19] = self.sign_payload(additional_buffer[0:19])
        result.append(additional_buffer)

        return result

    def prepare_packet(self, cmd, payload) -> bytes:
        """
        Prepares a single packed with the given arguments.
        Originally squashed into light.py
        """

        if not isinstance(cmd, int):
            raise ValueError('Invalid command')
        if not isinstance(payload, bytes) and not (
                isinstance(payload, list) and all(isinstance(x, int) for x in payload)):
            raise ValueError('Invalid payload')
        if len(payload) > 17:
            raise ValueError('Payload too long')

        cmd = cmd & 0xFF
        payload: bytes = bytes(payload)

        frame: bytes = bytes([0x33, cmd]) + bytes(payload)
        # pad frame data to 19 bytes (plus checksum)
        frame += bytes([0] * (19 - len(frame)))

        # The checksum is calculated by XORing all data bytes
        checksum = 0
        for b in frame:
            checksum ^= b

        frame += bytes([checksum & 0xFF])
        return frame

    def sign_payload(self, data):
        """
        Signs a payload - Not sure what this does exactly.
        Only the original maintainer would know.
        """
        checksum = 0
        for b in data:
            checksum ^= b
        return checksum & 0xFF
