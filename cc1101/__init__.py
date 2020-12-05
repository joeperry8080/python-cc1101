# python-cc1101 - Python Library to Transmit RF Signals via C1101 Transceivers
#
# Copyright (C) 2020 Fabian Peter Hammerle <fabian@hammerle.me>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import collections
import contextlib
import enum
import logging
import math
import typing

import spidev

from cc1101.addresses import (
    StrobeAddress,
    ConfigurationRegisterAddress,
    StatusRegisterAddress,
    FIFORegisterAddress,
)
from cc1101.options import PacketLengthMode, SyncMode, ModulationFormat


_LOGGER = logging.getLogger(__name__)


class Pin(enum.Enum):
    GDO0 = "GDO0"


class _TransceiveMode(enum.IntEnum):
    """
    PKTCTRL0.PKT_FORMAT
    """

    FIFO = 0b00
    SYNCHRONOUS_SERIAL = 0b01
    RANDOM_TRANSMISSION = 0b10
    ASYNCHRONOUS_SERIAL = 0b11


class MainRadioControlStateMachineState(enum.IntEnum):
    """
    MARCSTATE - Main Radio Control State Machine State
    """

    # see "Figure 13: Simplified State Diagram"
    # and "Figure 25: Complete Radio Control State Diagram"
    IDLE = 0x01
    STARTCAL = 0x08  # after IDLE
    BWBOOST = 0x09  # after STARTCAL
    FS_LOCK = 0x0A
    RX = 0x0D
    RXFIFO_OVERFLOW = 0x11
    TX = 0x13
    # TXFIFO_UNDERFLOW = 0x16


class _ReceivedPacket:  # unstable

    # "Table 31: Typical RSSI_offset Values"
    _RSSI_OFFSET_dB = 74

    def __init__(
        self,
        *,
        data: bytes,
        rssi_index: int,
        checksum_valid: bool,
        link_quality_indicator: int,  # 7bit
    ):
        self.data = data
        self._rssi_index = rssi_index
        assert 0 <= rssi_index < (1 << 8), rssi_index
        self.checksum_valid = checksum_valid
        self.link_quality_indicator = link_quality_indicator
        assert 0 <= link_quality_indicator < (1 << 7), link_quality_indicator

    @property
    def rssi_dbm(self) -> float:
        """
        Estimated Received Signal Strength Indicator (RSSI) in dBm

        see section "17.3 RSSI"
        """
        if self._rssi_index >= 128:
            return (self._rssi_index - 256) / 2 - self._RSSI_OFFSET_dB
        return self._rssi_index / 2 - self._RSSI_OFFSET_dB

    def __str__(self) -> str:
        return "{}(RSSI {:.0f}dBm, 0x{})".format(
            type(self).__name__,
            self.rssi_dbm,
            "".join("{:02x}".format(b) for b in self.data),
        )


class CC1101:

    # pylint: disable=too-many-public-methods

    # > All transfers on the SPI interface are done
    # > most significant bit first.
    # > All transactions on the SPI interface start with
    # > a header byte containing a R/W bit, a access bit (B),
    # > and a 6-bit address (A5 - A0).
    # > [...]
    # > Table 45: SPI Address Space
    _WRITE_SINGLE_BYTE = 0x00
    # > Registers with consecutive addresses can be
    # > accessed in an efficient way by setting the
    # > burst bit (B) in the header byte. The address
    # > bits (A5 - A0) set the start address in an
    # > internal address counter. This counter is
    # > incremented by one each new byte [...]
    _WRITE_BURST = 0x40
    _READ_SINGLE_BYTE = 0x80
    _READ_BURST = 0xC0

    # 29.3 Status Register Details
    _SUPPORTED_PARTNUM = 0
    _SUPPORTED_VERSION = 0x14

    _CRYSTAL_OSCILLATOR_FREQUENCY_HERTZ = 26e6
    # see "21 Frequency Programming"
    # > f_carrier = f_XOSC / 2**16 * (FREQ + CHAN * ((256 + CHANSPC_M) * 2**CHANSPC_E-2))
    _FREQUENCY_CONTROL_WORD_HERTZ_FACTOR = _CRYSTAL_OSCILLATOR_FREQUENCY_HERTZ / 2 ** 16

    def __init__(self) -> None:
        self._spi = spidev.SpiDev()

    @staticmethod
    def _log_chip_status_byte(chip_status: int) -> None:
        # see "10.1 Chip Status Byte" & "Table 23: Status Byte Summary"
        # > The command strobe registers are accessed by transferring
        # > a single header byte [...]. That is, only the R/W̄ bit,
        # > the burst access bit (set to 0), and the six address bits [...]
        # > The R/W̄ bit can be either one or zero and will determine how the
        # > FIFO_BYTES_AVAILABLE field in the status byte should be interpreted.
        _LOGGER.debug(
            "chip status byte: CHIP_RDYn=%d STATE=%s FIFO_BYTES_AVAILBLE=%d",
            chip_status >> 7,
            bin((chip_status >> 4) & 0b111),
            chip_status & 0b1111,
        )

    def _read_single_byte(
        self, register: typing.Union[ConfigurationRegisterAddress, FIFORegisterAddress]
    ) -> int:
        response = self._spi.xfer([register | self._READ_SINGLE_BYTE, 0])
        assert len(response) == 2, response
        self._log_chip_status_byte(response[0])
        return response[1]

    def _read_burst(
        self,
        start_register: typing.Union[ConfigurationRegisterAddress, FIFORegisterAddress],
        length: int,
    ) -> typing.List[int]:
        response = self._spi.xfer([start_register | self._READ_BURST] + [0] * length)
        assert len(response) == length + 1, response
        self._log_chip_status_byte(response[0])
        return response[1:]

    def _read_status_register(self, register: StatusRegisterAddress) -> int:
        # > For register addresses in the range 0x30-0x3D,
        # > the burst bit is used to select between
        # > status registers when burst bit is one, and
        # > between command strobes when burst bit is
        # > zero. [...]
        # > Because of this, burst access is not available
        # > for status registers and they must be accessed
        # > one at a time. The status registers can only be
        # > read.
        response = self._spi.xfer([register | self._READ_BURST, 0])
        assert len(response) == 2, response
        self._log_chip_status_byte(response[0])
        return response[1]

    def _command_strobe(self, register: StrobeAddress) -> None:
        # see "10.4 Command Strobes"
        _LOGGER.debug("sending command strobe 0x%02x", register)
        response = self._spi.xfer([register | self._WRITE_SINGLE_BYTE])
        assert len(response) == 1, response
        self._log_chip_status_byte(response[0])

    def _write_burst(
        self,
        start_register: typing.Union[ConfigurationRegisterAddress, FIFORegisterAddress],
        values: typing.List[int],
    ) -> None:
        _LOGGER.debug(
            "writing burst: start_register=0x%02x values=%s", start_register, values
        )
        response = self._spi.xfer([start_register | self._WRITE_BURST] + values)
        assert len(response) == len(values) + 1, response
        self._log_chip_status_byte(response[0])
        assert all(v == response[0] for v in response[1:]), response

    def _reset(self) -> None:
        self._command_strobe(StrobeAddress.SRES)

    @classmethod
    def _filter_bandwidth_floating_point_to_real(
        cls, mantissa: int, exponent: int
    ) -> float:
        """
        See "13 Receiver Channel Filter Bandwidth"
        """
        return cls._CRYSTAL_OSCILLATOR_FREQUENCY_HERTZ / (
            8 * (4 + mantissa) * (2 ** exponent)
        )

    def _get_filter_bandwidth_hertz(self) -> float:
        """
        See "13 Receiver Channel Filter Bandwidth"

        MDMCFG4.CHANBW_E & MDMCFG4.CHANBW_M
        """
        mdmcfg4 = self._read_single_byte(ConfigurationRegisterAddress.MDMCFG4)
        return self._filter_bandwidth_floating_point_to_real(
            exponent=mdmcfg4 >> 6, mantissa=(mdmcfg4 >> 4) & 0b11
        )

    def _set_filter_bandwidth(self, *, mantissa: int, exponent: int) -> None:
        """
        MDMCFG4.CHANBW_E & MDMCFG4.CHANBW_M
        """
        mdmcfg4 = self._read_single_byte(ConfigurationRegisterAddress.MDMCFG4)
        mdmcfg4 &= 0b00001111
        assert 0 <= exponent <= 0b11, exponent
        mdmcfg4 |= exponent << 6
        assert 0 <= mantissa <= 0b11, mantissa
        mdmcfg4 |= mantissa << 4
        self._write_burst(
            start_register=ConfigurationRegisterAddress.MDMCFG4, values=[mdmcfg4]
        )

    def _get_symbol_rate_exponent(self) -> int:
        """
        MDMCFG4.DRATE_E
        """
        return self._read_single_byte(ConfigurationRegisterAddress.MDMCFG4) & 0b00001111

    def _set_symbol_rate_exponent(self, exponent: int):
        mdmcfg4 = self._read_single_byte(ConfigurationRegisterAddress.MDMCFG4)
        mdmcfg4 &= 0b11110000
        mdmcfg4 |= exponent
        self._write_burst(
            start_register=ConfigurationRegisterAddress.MDMCFG4, values=[mdmcfg4]
        )

    def _get_symbol_rate_mantissa(self) -> int:
        """
        MDMCFG3.DRATE_M
        """
        return self._read_single_byte(ConfigurationRegisterAddress.MDMCFG3)

    def _set_symbol_rate_mantissa(self, mantissa: int) -> None:
        self._write_burst(
            start_register=ConfigurationRegisterAddress.MDMCFG3, values=[mantissa]
        )

    @classmethod
    def _symbol_rate_floating_point_to_real(cls, mantissa: int, exponent: int) -> float:
        # see "12 Data Rate Programming"
        return (
            (256 + mantissa)
            * (2 ** exponent)
            * cls._CRYSTAL_OSCILLATOR_FREQUENCY_HERTZ
            / (2 ** 28)
        )

    @classmethod
    def _symbol_rate_real_to_floating_point(cls, real: float) -> typing.Tuple[int, int]:
        # see "12 Data Rate Programming"
        assert real > 0, real
        exponent = math.floor(
            math.log2(real / cls._CRYSTAL_OSCILLATOR_FREQUENCY_HERTZ) + 20
        )
        mantissa = round(
            real * 2 ** 28 / cls._CRYSTAL_OSCILLATOR_FREQUENCY_HERTZ / 2 ** exponent
            - 256
        )
        if mantissa == 256:
            exponent += 1
            mantissa = 0
        assert 0 < exponent <= 2 ** 4, exponent
        assert mantissa <= 2 ** 8, mantissa
        return mantissa, exponent

    def get_symbol_rate_baud(self) -> float:
        return self._symbol_rate_floating_point_to_real(
            mantissa=self._get_symbol_rate_mantissa(),
            exponent=self._get_symbol_rate_exponent(),
        )

    def set_symbol_rate_baud(self, real: float) -> None:
        # > The data rate can be set from 0.6 kBaud to 500 kBaud [...]
        mantissa, exponent = self._symbol_rate_real_to_floating_point(real)
        self._set_symbol_rate_mantissa(mantissa)
        self._set_symbol_rate_exponent(exponent)

    def get_modulation_format(self) -> ModulationFormat:
        mdmcfg2 = self._read_single_byte(ConfigurationRegisterAddress.MDMCFG2)
        return ModulationFormat((mdmcfg2 >> 4) & 0b111)

    def _set_modulation_format(self, modulation_format: ModulationFormat) -> None:
        mdmcfg2 = self._read_single_byte(ConfigurationRegisterAddress.MDMCFG2)
        mdmcfg2 &= ~(modulation_format << 4)
        mdmcfg2 |= modulation_format << 4
        self._write_burst(ConfigurationRegisterAddress.MDMCFG2, [mdmcfg2])

    def enable_manchester_code(self) -> None:
        """
        MDMCFG2.MANCHESTER_EN

        Enable manchester encoding & decoding for the entire packet,
        including the preamble and synchronization word.
        """
        mdmcfg2 = self._read_single_byte(ConfigurationRegisterAddress.MDMCFG2)
        mdmcfg2 |= 0b1000
        self._write_burst(ConfigurationRegisterAddress.MDMCFG2, [mdmcfg2])

    def get_sync_mode(self) -> SyncMode:
        mdmcfg2 = self._read_single_byte(ConfigurationRegisterAddress.MDMCFG2)
        return SyncMode(mdmcfg2 & 0b11)

    def set_sync_mode(self, mode: SyncMode) -> None:
        """
        MDMCFG2.SYNC_MODE

        see "14.3 Byte Synchronization"
        """
        mdmcfg2 = self._read_single_byte(ConfigurationRegisterAddress.MDMCFG2)
        mdmcfg2 &= 0b11111100
        mdmcfg2 |= mode
        self._write_burst(ConfigurationRegisterAddress.MDMCFG2, [mdmcfg2])

    def get_preamble_length_bytes(self) -> int:
        """
        MDMCFG1.NUM_PREAMBLE

        Minimum number of preamble bytes to be transmitted.

        See "15.2 Packet Format"
        """
        index = (
            self._read_single_byte(ConfigurationRegisterAddress.MDMCFG1) >> 4
        ) & 0b111
        return 2 ** (index >> 1) * (2 + (index & 0b1))

    def _set_preamble_length_index(self, index: int) -> None:
        assert 0 <= index <= 0b111
        mdmcfg1 = self._read_single_byte(ConfigurationRegisterAddress.MDMCFG1)
        mdmcfg1 &= 0b10001111
        mdmcfg1 |= index << 4
        self._write_burst(ConfigurationRegisterAddress.MDMCFG1, [mdmcfg1])

    def set_preamble_length_bytes(self, length: int) -> None:
        """
        see .get_preamble_length_bytes()
        """
        if length < 1:
            raise ValueError(
                "invalid preamble length {} given".format(length)
                + "\ncall .set_sync_mode(cc1101.SyncMode.NO_PREAMBLE_AND_SYNC_WORD)"
                + " to disable preamble"
            )
        if length % 3 == 0:
            index = math.log2(length / 3) * 2 + 1
        else:
            index = math.log2(length / 2) * 2
        if not index.is_integer() or index < 0 or index > 0b111:
            raise ValueError(
                "unsupported preamble length: {} bytes".format(length)
                + "\nsee MDMCFG1.NUM_PREAMBLE in cc1101 docs"
            )
        self._set_preamble_length_index(int(index))

    def _set_power_amplifier_setting_index(self, setting_index: int) -> None:
        """
        FREND0.PA_POWER

        > This value is an index to the PATABLE,
        > which can be programmed with up to 8 different PA settings.

        > In OOK/ASK mode, this selects the PATABLE index to use
        > when transmitting a '1'.
        > PATABLE index zero is used in OOK/ASK when transmitting a '0'.
        > The PATABLE settings from index 0 to the PA_POWER value are
        > used for > ASK TX shaping, [...]

        see "Figure 32: Shaping of ASK Signal"

        > If OOK modulation is used, the logic 0 and logic 1 power levels
        > shall be programmed to index 0 and 1 respectively.
        """
        frend0 = self._read_single_byte(ConfigurationRegisterAddress.FREND0)
        frend0 &= 0b000
        frend0 |= setting_index
        self._write_burst(ConfigurationRegisterAddress.FREND0, [setting_index])

    def __enter__(self) -> "CC1101":
        # https://docs.python.org/3/reference/datamodel.html#object.__enter__
        self._spi.open(0, 0)
        self._spi.max_speed_hz = 55700  # empirical
        self._reset()
        partnum = self._read_status_register(StatusRegisterAddress.PARTNUM)
        if partnum != self._SUPPORTED_PARTNUM:
            raise ValueError(
                "unexpected chip part number {} (expected: {})".format(
                    partnum, self._SUPPORTED_PARTNUM
                )
            )
        version = self._read_status_register(StatusRegisterAddress.VERSION)
        if version != self._SUPPORTED_VERSION:
            raise ValueError(
                "unexpected chip version number {} (expected: {})".format(
                    version, self._SUPPORTED_VERSION
                )
            )
        # 6:4 MOD_FORMAT: OOK (default: 2-FSK)
        self._set_modulation_format(ModulationFormat.ASK_OOK)
        self._set_power_amplifier_setting_index(1)
        self._disable_data_whitening()
        # 7:6 unused
        # 5:4 FS_AUTOCAL: calibrate when going from IDLE to RX or TX
        # 3:2 PO_TIMEOUT: default
        # 1 PIN_CTRL_EN: default
        # 0 XOSC_FORCE_ON: default
        self._write_burst(ConfigurationRegisterAddress.MCSM0, [0b010100])
        marcstate = self.get_main_radio_control_state_machine_state()
        if marcstate != MainRadioControlStateMachineState.IDLE:
            raise ValueError("expected marcstate idle (actual: {})".format(marcstate))
        return self

    def __exit__(self, exc_type, exc_value, traceback):  # -> typing.Literal[False]
        # https://docs.python.org/3/reference/datamodel.html#object.__exit__
        self._spi.close()
        return False

    def get_main_radio_control_state_machine_state(
        self,
    ) -> MainRadioControlStateMachineState:
        return MainRadioControlStateMachineState(
            self._read_status_register(StatusRegisterAddress.MARCSTATE)
        )

    def get_marc_state(self) -> MainRadioControlStateMachineState:
        """
        alias for get_main_radio_control_state_machine_state()
        """
        return self.get_main_radio_control_state_machine_state()

    @classmethod
    def _frequency_control_word_to_hertz(cls, control_word: typing.List[int]) -> float:
        return (
            int.from_bytes(control_word, byteorder="big", signed=False)
            * cls._FREQUENCY_CONTROL_WORD_HERTZ_FACTOR
        )

    @classmethod
    def _hertz_to_frequency_control_word(cls, hertz: float) -> typing.List[int]:
        return list(
            round(hertz / cls._FREQUENCY_CONTROL_WORD_HERTZ_FACTOR).to_bytes(
                length=3, byteorder="big", signed=False
            )
        )

    def _get_base_frequency_control_word(self) -> typing.List[int]:
        # > The base or start frequency is set by the 24 bitfrequency
        # > word located in the FREQ2, FREQ1, FREQ0 registers.
        return self._read_burst(
            start_register=ConfigurationRegisterAddress.FREQ2, length=3
        )

    def _set_base_frequency_control_word(self, control_word: typing.List[int]) -> None:
        self._write_burst(
            start_register=ConfigurationRegisterAddress.FREQ2, values=control_word
        )

    def get_base_frequency_hertz(self) -> float:
        return self._frequency_control_word_to_hertz(
            self._get_base_frequency_control_word()
        )

    def set_base_frequency_hertz(self, freq: float) -> None:
        self._set_base_frequency_control_word(
            self._hertz_to_frequency_control_word(freq)
        )

    def __str__(self) -> str:
        sync_mode = self.get_sync_mode()
        attrs = (
            "marcstate={}".format(
                self.get_main_radio_control_state_machine_state().name.lower()
            ),
            "base_frequency={:.2f}MHz".format(
                self.get_base_frequency_hertz() / 10 ** 6
            ),
            "symbol_rate={:.2f}kBaud".format(self.get_symbol_rate_baud() / 1000),
            "modulation_format={}".format(self.get_modulation_format().name),
            "sync_mode={}".format(sync_mode.name),
            "preamble_length={}B".format(self.get_preamble_length_bytes())
            if sync_mode != SyncMode.NO_PREAMBLE_AND_SYNC_WORD
            else None,
            "sync_word=0x{:02x}{:02x}".format(*self.get_sync_word())
            if sync_mode != SyncMode.NO_PREAMBLE_AND_SYNC_WORD
            else None,
            "packet_length{}{}B".format(
                "≤"
                if self.get_packet_length_mode() == PacketLengthMode.VARIABLE
                else "=",
                self.get_packet_length_bytes(),
            ),
        )
        return "CC1101({})".format(", ".join(filter(None, attrs)))

    def get_configuration_register_values(
        self,
        start_register: ConfigurationRegisterAddress = min(
            ConfigurationRegisterAddress
        ),
        end_register: ConfigurationRegisterAddress = max(ConfigurationRegisterAddress),
    ) -> typing.Dict[ConfigurationRegisterAddress, int]:
        assert start_register <= end_register, (start_register, end_register)
        values = self._read_burst(
            start_register=start_register, length=end_register - start_register + 1
        )
        return {
            ConfigurationRegisterAddress(start_register + i): v
            for i, v in enumerate(values)
        }

    def get_sync_word(self) -> bytes:
        """
        SYNC1 & SYNC0

        See "15.2 Packet Format"

        The first byte's most significant bit is transmitted first.
        """
        return bytes(
            self._read_burst(
                start_register=ConfigurationRegisterAddress.SYNC1, length=2
            )
        )

    def set_sync_word(self, sync_word: bytes) -> None:
        """
        See .set_sync_word()
        """
        if len(sync_word) != 2:
            raise ValueError("expected two bytes, got {!r}".format(sync_word))
        self._write_burst(
            start_register=ConfigurationRegisterAddress.SYNC1, values=list(sync_word)
        )

    def get_packet_length_bytes(self) -> int:
        """
        PKTLEN

        Packet length in fixed packet length mode,
        maximum packet length in variable packet length mode.

        > In variable packet length mode, [...]
        > any packet received with a length byte
        > with a value greater than PKTLEN will be discarded.
        """
        return self._read_single_byte(ConfigurationRegisterAddress.PKTLEN)

    def set_packet_length_bytes(self, packet_length: int) -> None:
        """
        see get_packet_length_bytes()
        """
        assert 1 <= packet_length <= 255, "unsupported packet length {}".format(
            packet_length
        )
        self._write_burst(
            start_register=ConfigurationRegisterAddress.PKTLEN, values=[packet_length]
        )

    def _disable_data_whitening(self):
        """
        PKTCTRL0.WHITE_DATA

        see "15.1 Data Whitening"

        > By setting PKTCTRL0.WHITE_DATA=1 [default],
        > all data, except the preamble and the sync word
        > will be XOR-ed with a 9-bit pseudo-random (PN9)
        > sequence before being transmitted.
        """
        pktctrl0 = self._read_single_byte(ConfigurationRegisterAddress.PKTCTRL0)
        pktctrl0 &= 0b10111111
        self._write_burst(
            start_register=ConfigurationRegisterAddress.PKTCTRL0, values=[pktctrl0]
        )

    def disable_checksum(self) -> None:
        """
        PKTCTRL0.CRC_EN

        Disable automatic 2-byte cyclic redundancy check (CRC) sum
        appending in TX mode and checking in RX mode.

        See "Figure 19: Packet Format".
        """
        pktctrl0 = self._read_single_byte(ConfigurationRegisterAddress.PKTCTRL0)
        pktctrl0 &= 0b11111011
        self._write_burst(
            start_register=ConfigurationRegisterAddress.PKTCTRL0, values=[pktctrl0]
        )

    def _get_transceive_mode(self) -> _TransceiveMode:
        pktctrl0 = self._read_single_byte(ConfigurationRegisterAddress.PKTCTRL0)
        return _TransceiveMode((pktctrl0 >> 4) & 0b11)

    def _set_transceive_mode(self, mode: _TransceiveMode) -> None:
        _LOGGER.info("changing transceive mode to %s", mode.name)
        pktctrl0 = self._read_single_byte(ConfigurationRegisterAddress.PKTCTRL0)
        pktctrl0 &= ~0b00110000
        pktctrl0 |= mode << 4
        self._write_burst(
            start_register=ConfigurationRegisterAddress.PKTCTRL0, values=[pktctrl0]
        )

    def get_packet_length_mode(self) -> PacketLengthMode:
        pktctrl0 = self._read_single_byte(ConfigurationRegisterAddress.PKTCTRL0)
        return PacketLengthMode(pktctrl0 & 0b11)

    def set_packet_length_mode(self, mode: PacketLengthMode) -> None:
        pktctrl0 = self._read_single_byte(ConfigurationRegisterAddress.PKTCTRL0)
        pktctrl0 &= 0b11111100
        pktctrl0 |= mode
        self._write_burst(
            start_register=ConfigurationRegisterAddress.PKTCTRL0, values=[pktctrl0]
        )

    def _flush_tx_fifo_buffer(self) -> None:
        # > Only issue SFTX in IDLE or TXFIFO_UNDERFLOW states.
        _LOGGER.debug("flushing tx fifo buffer")
        self._command_strobe(StrobeAddress.SFTX)

    def transmit(self, payload: bytes) -> None:
        """
        The most significant bit is transmitted first.

        In variable packet length mode,
        a byte indicating the packet's length will be prepended.

        > In variable packet length mode,
        > the packet length is configured by the first byte [...].
        > The packet length is defined as the payload data,
        > excluding the length byte and the optional CRC.
        from "15.2 Packet Format"

        Call .set_packet_length_mode(cc1101.PacketLengthMode.FIXED)
        to switch to fixed packet length mode.
        """
        # see "15.2 Packet Format"
        # > In variable packet length mode, [...]
        # > The first byte written to the TXFIFO must be different from 0.
        packet_length_mode = self.get_packet_length_mode()
        packet_length = self.get_packet_length_bytes()
        if packet_length_mode == PacketLengthMode.VARIABLE:
            if not payload:
                raise ValueError("empty payload {!r}".format(payload))
            if len(payload) > packet_length:
                raise ValueError(
                    "payload exceeds maximum payload length of {} bytes".format(
                        packet_length
                    )
                    + "\nsee .get_packet_length_bytes()"
                    + "\npayload: {!r}".format(payload)
                )
            payload = int.to_bytes(len(payload), length=1, byteorder="big") + payload
        elif (
            packet_length_mode == PacketLengthMode.FIXED
            and len(payload) != packet_length
        ):
            raise ValueError(
                "expected payload length of {} bytes, got {}".format(
                    packet_length, len(payload)
                )
                + "\nsee .set_packet_length_mode() and .get_packet_length_bytes()"
                + "\npayload: {!r}".format(payload)
            )
        marcstate = self.get_main_radio_control_state_machine_state()
        if marcstate != MainRadioControlStateMachineState.IDLE:
            raise Exception(
                "device must be idle before transmission (current marcstate: {})".format(
                    marcstate.name
                )
            )
        self._flush_tx_fifo_buffer()
        self._write_burst(FIFORegisterAddress.TX, list(payload))
        _LOGGER.info(
            "transmitting 0x%s (%r)",
            "".join("{:02x}".format(b) for b in payload),
            payload,
        )
        self._command_strobe(StrobeAddress.STX)

    @contextlib.contextmanager
    def asynchronous_transmission(self) -> typing.Iterator[Pin]:
        """
        see "27.1 Asynchronous Serial Operation"

        >>> with cc1101.CC1101() as transceiver:
        >>>     transceiver.set_base_frequency_hertz(433.92e6)
        >>>     transceiver.set_symbol_rate_baud(600)
        >>>     print(transceiver)
        >>>     with transceiver.asynchronous_transmission():
        >>>         # send digital signal to GDO0 pin
        """
        self._set_transceive_mode(_TransceiveMode.ASYNCHRONOUS_SERIAL)
        self._command_strobe(StrobeAddress.STX)
        try:
            # > In TX, the GDO0 pin is used for data input (TX data).
            yield Pin.GDO0
        finally:
            self._command_strobe(StrobeAddress.SIDLE)
            self._set_transceive_mode(_TransceiveMode.FIFO)

    def _enable_receive_mode(self) -> None:  # unstable
        self._command_strobe(StrobeAddress.SRX)

    def _get_received_packet(self) -> typing.Optional[_ReceivedPacket]:  # unstable
        """
        see section "20 Data FIFO"
        """
        rxbytes = self._read_status_register(StatusRegisterAddress.RXBYTES)
        # PKTCTRL1.APPEND_STATUS is enabled by default
        if rxbytes < 2:
            return None
        buffer = self._read_burst(start_register=FIFORegisterAddress.RX, length=rxbytes)
        return _ReceivedPacket(
            data=bytes(buffer[:-2]),
            rssi_index=buffer[-2],
            checksum_valid=bool(buffer[-1] >> 7),
            link_quality_indicator=buffer[-1] & 0b0111111,
        )
