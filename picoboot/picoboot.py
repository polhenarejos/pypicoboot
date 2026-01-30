"""
/*
 * This file is part of the pypicoboot distribution (https://github.com/polhenarejos/pypicoboot).
 * Copyright (c) 2025 Pol Henarejos.
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as published by
 * the Free Software Foundation, version 3.
 *
 * This program is distributed in the hope that it will be useful, but
 * WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
 * Affero General Public License for more details.
 *
 * You should have received a copy of the GNU Affero General Public License
 * along with this program. If not, see <https://www.gnu.org/licenses/>.
 */
"""

from binascii import hexlify
from typing import Optional
import usb.core
import usb.util
import struct
import itertools
from .utils import uint_to_int, crc32_ieee
from .core.enums import NamedIntEnum
from .picobootmonitor import PicoBootMonitor, PicoBootMonitorObserver
from .core.log import get_logger
from .core.exceptions import PicoBootError, PicoBootNotFoundError, PicoBootInvalidStateError

logger = get_logger("PicoBoot")

# Valors per defecte segons el datasheet (es poden canviar via OTP) :contentReference[oaicite:4]{index=4}
DEFAULT_VID = 0x2E8A
DEFAULT_PID_RP2040 = 0x0003
DEFAULT_PID_RP2350 = 0x000F

PICOBOOT_MAGIC = 0x431FD10B

# Bit 7 = direcció de transferència de dades (IN si està posat) :contentReference[oaicite:5]{index=5}
CMD_DIR_IN = 0x80

# IDs de comanda (secció 5.6.4) :contentReference[oaicite:6]{index=6}
class CommandID(NamedIntEnum):
    EXCLUSIVE_ACCESS = 0x01
    REBOOT           = 0x02
    FLASH_ERASE      = 0x03
    READ             = 0x84
    WRITE            = 0x05
    EXIT_XIP         = 0x06
    ENTER_XIP        = 0x07
    REBOOT2          = 0x0A
    GET_INFO         = 0x8B
    OTP_READ         = 0x8C
    OTP_WRITE        = 0x0D

# Control requests (secció 5.6.5) :contentReference[oaicite:7]{index=7}
class ControlRequest(NamedIntEnum):
    REQ_INTERFACE_RESET    = 0x41
    REQ_GET_COMMAND_STATUS = 0x42
    BMREQ_RESET            = 0x41  # Host->Device, Class, Interface
    BMREQ_GET_STATUS       = 0xC1  # Device->Host, Class, Interface

class InfoType(NamedIntEnum):
    SYS                    = 0x01
    PARTITION              = 0x02
    UF2_TARGET_PARTITION   = 0x03
    UF2_STATUS             = 0x04

class SysInfoFlags(NamedIntEnum):
    CHIP_INFO        = 0x01
    CRITICAL         = 0x02
    CPU              = 0x04
    FLASH            = 0x08
    BOOT_RANDOM      = 0x10
    NONCE            = 0x20
    BOOT_INFO        = 0x40

class CriticalRegister(NamedIntEnum):
    SECURE_BOOT             = 0x01
    SECURE_DEBUG_DISABLE    = 0x02
    DEBUG_DISABLE           = 0x04
    DEFAULT_ARCHSEL         = 0x08
    GLITCH_DETECTOR_ENABLE  = 0x10
    GLITCH_DETECTOR_SENS    = 0x60
    ARM_DISABLE             = 0x10000
    RISCV_DISABLE           = 0x20000

class DiagnosticPartition(NamedIntEnum):
    REGION_SEARCHED                         = 0x01
    INVALID_BLOCK_LOOPS                     = 0x02
    VALID_BLOCK_LOOPS                       = 0x04
    VALID_IMAGE_DEFAULTS                    = 0x08
    HAS_PARTITION_TABLE                     = 0x10
    CONSIDERED                              = 0x20
    CHOSEN                                  = 0x40
    PARTITION_TABLE_MATCHING_KEY_FOR_VERIFY = 0x80
    PARTITION_TABLE_HASH_FOR_VERIFY         = 0x100
    PARTITION_TABLE_VERIFIED_OK             = 0x200
    IMAGE_DEF_MATCHING_KEY_FOR_VERIFY       = 0x400
    IMAGE_DEF_HASH_FOR_VERIFY               = 0x800
    IMAGE_DEF_VERIFIED_OK                   = 0x1000
    LOAD_MAP_ENTRIES_LOADED                 = 0x2000
    IMAGE_LAUNCHED                          = 0x4000
    IMAGE_CONDITION_FAILURES                = 0x8000

class PartitionInfoType(NamedIntEnum):
    PARTITION_0       = 0
    PARTITION_1       = 1
    PARTITION_2       = 2
    PARTITION_3       = 3
    PARTITION_4       = 4
    PARTITION_5       = 5
    PARTITION_6       = 6
    PARTITION_7       = 7
    PARTITION_8       = 8
    PARTITION_9       = 9
    PARTITION_10      = 10
    PARTITION_11      = 11
    PARTITION_12      = 12
    PARTITION_13      = 13
    PARTITION_14      = 14
    PARTITION_15      = 15
    NONE              = -1
    SLOT_0            = -2
    SLOT_1            = -3
    IMAGE            = -4

class Platform(NamedIntEnum):
    RP2040  = 0x01754d
    RP2350  = 0x02754d
    UNKNOWN = 0x000000

class Addresses(NamedIntEnum):
    BOOTROM_MAGIC = 0x00000010
    PHYMARKER     = 0x10100000

class PicoBoot:

    def __init__(self, dev: usb.core.Device, intf, ep_out, ep_in) -> None:
        logger.info("Initializing PicoBoot device...")
        self.dev = dev
        self.intf = intf
        self.ep_out = ep_out
        self.ep_in = ep_in
        self._token_counter = itertools.count(1)
        logger.debug("Resetting interface...")
        self.interface_reset()
        logger.debug("Guessing flash size...")
        self._memory = self._guess_flash_size()
        logger.debug(f"Detected flash size: {self._memory // 1024} kB")
        logger.debug("Determining platform...")
        self._platform = self._determine_platform()
        logger.debug(f"Detected platform: {self._platform.name}")

        class PicoBootObserver(PicoBootMonitorObserver):

                def __init__(self, device: PicoBoot):
                    self.__device = device

                def update(self, actions: tuple[list[PicoBoot], list[PicoBoot]]) -> None:
                    (connected, disconnected) = actions
                    if connected:
                        logger.debug("PicoBoot device connected")
                        pass
                    if disconnected:
                        logger.debug("PicoBoot device disconnected")
                        self.__device.close()

        logger.debug("Starting PicoBoot monitor...")
        self.__observer = PicoBootObserver(self)
        logger.debug("PicoBoot monitor started.")
        self.__monitor = PicoBootMonitor(device=self.dev, cls_callback=self.__observer)
        logger.debug("PicoBoot device initialized.")

    @classmethod
    def open(cls, vid: int = DEFAULT_VID, pid: list[int] = [DEFAULT_PID_RP2040, DEFAULT_PID_RP2350], serial: Optional[str] = None, slot = -1) -> "PicoBoot":
        logger.info(f"Opening PicoBoot device with VID={vid:04x} and PIDs={[f'{p:04x}' for p in pid]}...")
        class find_vidpids(object):

            def __init__(self, vid: int, pids: list[int]):
                self._vid = vid
                self._pids = pids

            def __call__(self, device: usb.core.Device) -> bool:
                if device.idProduct in self._pids and device.idVendor == self._vid:
                    return True
                return False

        devices = usb.core.find(find_all=True, custom_match=find_vidpids(vid, pid))
        devices = list(devices) if devices is not None else []
        if not devices:
            logger.error("No device found in PICOBOOT mode")
            raise PicoBootNotFoundError("No device found in PICOBOOT mode")

        if slot >= 0:
            logger.info(f"Looking for device in slot {slot}...")
            if (slot >= len(devices)):
                logger.error("No device found in the specified slot")
                raise PicoBootNotFoundError("No device found in the specified slot")
            devices = [devices[slot]] if slot < len(devices) else []

        dev = None
        if serial is None:
            logger.info("No serial number provided, using the first device found.")
            dev = devices[0]
        else:
            for d in devices:
                try:
                    s = usb.util.get_string(d, d.iSerialNumber)
                except usb.core.USBError:
                    continue
                if s == serial:
                    dev = d
                    logger.debug(f"Using device with serial number: {serial}")
                    break
        if dev is None:
            logger.error("No device found with this serial number")
            raise PicoBootNotFoundError("No device found with this serial number")

        # Ensure active configuration
        # macOS does not allow detach_kernel_driver, and often returns Access Denied
        try:
            logger.debug("Checking if kernel driver is active and detaching if necessary...")
            if dev.is_kernel_driver_active(1):
                logger.debug("Kernel driver is active, detaching...")
                dev.detach_kernel_driver(1)
                logger.debug("Kernel driver detached.")
        except usb.core.USBError:
            # If it fails, we continue anyway. It's normal on macOS.
            pass
        except NotImplementedError:
            # Also fine on backends that don't implement the function
            pass

        #dev.set_configuration()
        logger.debug("Getting active configuration...")
        cfg = dev.get_active_configuration()
        logger.debug("Searching for PICOBOOT interface...")

        intf = None
        for i in cfg:
            if i.bInterfaceClass == 0xFF and i.bInterfaceSubClass == 0 and i.bInterfaceProtocol == 0:
                intf = i
                break
        if intf is None:
            logger.error("No interface found with PICOBOOT at the device")
            raise PicoBootNotFoundError("No interface found with PICOBOOT at the device")

        #usb.util.claim_interface(dev, intf.bInterfaceNumber)

        logger.debug("Finding BULK_IN and BULK_OUT endpoints...")
        ep_in = ep_out = None
        for ep in intf.endpoints():
            if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN:
                logger.debug(f"Found BULK_IN endpoint: 0x{ep.bEndpointAddress:02X}")
                ep_in = ep
            else:
                logger.debug(f"Found BULK_OUT endpoint: 0x{ep.bEndpointAddress:02X}")
                ep_out = ep

        if ep_in is None or ep_out is None:
            logger.error("No PICOBOOT BULK_IN/BULK_OUT endpoints found")
            raise PicoBootNotFoundError("No PICOBOOT BULK_IN/BULK_OUT endpoints found")
        logger.info("PICOBOOT device opened successfully.")
        return cls(dev, intf, ep_out, ep_in)

    def close(self):
        logger.debug("Closing PicoBoot device...")
        if self.dev:
            self.__monitor.stop()
            logger.debug("Releasing USB resources...")
            usb.util.dispose_resources(self.dev)
            logger.debug("PicoBoot device closed.")
            self.dev = None

    def has_device(self):
        return self.dev is not None

    @property
    def serial_number_str(self) -> str:
        try:
            s = None
            if self.platform == Platform.RP2040:
                r = self.flash_read(Addresses.PHYMARKER, 24)
                magic = struct.unpack_from("<Q", r, 0)[0]
                if (magic == 0x5049434F4B455953):  # "PICOKEYS"
                    crc32 = crc32_ieee(r[0:20])
                    if crc32 == struct.unpack_from("<I", r, 20)[0]:
                        s = hexlify(r[12:20]).decode().upper()
            if not s:
                s = usb.util.get_string(self.dev, self.dev.iSerialNumber)
        except Exception:
            s = "unknown"
        return s

    @property
    def serial_number(self) -> int:
        if self.dev is None:
            raise PicoBootInvalidStateError("Device not connected")
        s = self.serial_number_str
        return int(s, 16)

    def interface_reset(self) -> None:
        logger.debug("Resetting interface...")
        self.dev.ctrl_transfer(
            ControlRequest.BMREQ_RESET,
            ControlRequest.REQ_INTERFACE_RESET,
            0,
            self.intf.bInterfaceNumber,
            None
        )
        logger.debug("Interface reset command sent.")

    def get_command_status(self) -> dict:
        logger.debug("Getting command status...")
        data = self.dev.ctrl_transfer(
            ControlRequest.BMREQ_GET_STATUS,
            ControlRequest.REQ_GET_COMMAND_STATUS,
            0,
            self.intf.bInterfaceNumber,
            16,
        )
        logger.debug(f"Command status data: {hexlify(data).decode()}")
        b = bytes(data)
        dToken, dStatusCode = struct.unpack_from("<II", b, 0)
        bCmdId = b[8]
        bInProgress = b[9]
        return {
            "token": dToken,
            "status": dStatusCode,
            "cmd_id": bCmdId,
            "in_progress": bool(bInProgress),
        }

    def _next_token(self) -> int:
        return next(self._token_counter) & 0xFFFFFFFF

    def _build_command(self, cmd_id: CommandID, args: bytes = b"", transfer_length: int = 0, token: Optional[int] = None) -> tuple[int, bytes]:
        if token is None:
            token = self._next_token()
        if len(args) > 16:
            raise ValueError("Too many args: maximum 16 bytes")
        bCmdSize = len(args)
        args = args.ljust(16, b"\x00")
        header = struct.pack(
            "<I I B B H I 16s",
            PICOBOOT_MAGIC,
            token,
            cmd_id & 0xFF,
            bCmdSize & 0xFF,
            0,                      # reserved
            transfer_length & 0xFFFFFFFF,
            args,
        )
        return token, header

    def _send_command(
        self,
        cmd_id: CommandID,
        args: bytes = b"",
        data_out: bytes | None = None,
        transfer_length: int | None = None,
        timeout: int = 3000,
    ) -> bytes:
        is_in = bool(cmd_id & CMD_DIR_IN)

        if transfer_length is None:
            transfer_length = 0 if data_out is None else len(data_out)

        logger.debug(f"Preparing to send command {cmd_id} (0x{cmd_id:02X}) with args length {len(args)} and transfer_length {transfer_length}")
        try:
            token, header = self._build_command(cmd_id, args=args, transfer_length=transfer_length)
        except ValueError as e:
            logger.error(f"Error building command: {e}")
            raise
        logger.debug(f"Sending command {cmd_id} (0x{cmd_id:02X}) with token {token} (0x{token:08X}) and transfer_length {transfer_length}")

        logger.trace(f"Command header: {hexlify(header).decode()}")
        try:
            self.ep_out.write(header, timeout=timeout)
        except usb.core.USBError as e:
            logger.error(f"Failed to send command header: {e}")
            raise PicoBootInvalidStateError("Failed to send command header: " + str(e))
        logger.debug(f"Command header sent: {hexlify(header).decode()}")

        data_in = b""

        if transfer_length:
            if is_in:
                remaining = transfer_length
                chunks = []
                maxpkt = self.ep_in.wMaxPacketSize
                while remaining > 0:
                    try:
                        chunk = bytes(self.ep_in.read(min(maxpkt, remaining), timeout=timeout))
                    except usb.core.USBError as e:
                        logger.error(f"Failed to read data_in: {e}")
                        raise PicoBootInvalidStateError("Failed to read data_in: " + str(e))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                data_in = b"".join(chunks)
                logger.trace(f"Received data_in: {hexlify(data_in).decode()}")
                if len(data_in) != transfer_length:
                    logger.error(f"Expected {transfer_length} bytes, got {len(data_in)}")
                    raise PicoBootError(f"Expected {transfer_length} bytes, got {len(data_in)}")
            else:
                if data_out is None or len(data_out) < transfer_length:
                    logger.error("data_out missing or too short for OUT command")
                    raise ValueError("data_out missing or too short for OUT command")
                logger.trace(f"Sending data_out: {hexlify(data_out[:transfer_length]).decode()}")
                try:
                    self.ep_out.write(data_out[:transfer_length], timeout=timeout)
                except usb.core.USBError as e:
                    logger.error(f"Failed to send data_out: {e}")
                    raise PicoBootInvalidStateError("Failed to send data_out: " + str(e))

        try:
            logger.debug("Waiting for ACK...")
            if is_in:
                self.ep_out.write(b"", timeout=timeout)
            else:
                ack = self.ep_in.read(1, timeout=timeout)
        except usb.core.USBError:
            logger.error("No ACK received after command")
            raise PicoBootError("No ACK received after command")
        logger.debug("ACK received.")

        return data_in


    def flash_erase(self, addr: int, size: int) -> None:
        logger.debug(f"Erasing flash at address 0x{addr:08X} with size {size} bytes")
        if addr % 4096 != 0 or size % 4096 != 0:
            logger.error("addr i size must be aligned to 4kB")
            raise ValueError("addr i size must be aligned to 4kB")
        args = struct.pack("<II", addr, size)
        self._send_command(CommandID.FLASH_ERASE, args=args, transfer_length=0)

    def flash_read(self, addr: int, size: int) -> bytes:
        logger.debug(f"Reading flash at address 0x{addr:08X} with size {size} bytes")
        args = struct.pack("<II", addr, size)
        data = self._send_command(CommandID.READ, args=args, transfer_length=size)
        if len(data) != size:
            logger.error(f"READ returned {len(data)} bytes, expected {size}")
            raise PicoBootError(f"READ returned {len(data)} bytes, expected {size}")
        return data

    def flash_write(self, addr: int, data: bytes) -> None:
        logger.debug(f"Writing flash at address 0x{addr:08X} with size {len(data)} bytes")
        if addr % 256 != 0 or len(data) % 256 != 0:
            logger.error("addr i len(data) must be aligned/multiple of 256 bytes")
            raise ValueError("addr i len(data) must be aligned/multiple of 256 bytes")
        args = struct.pack("<II", addr, len(data))
        self._send_command(CommandID.WRITE, args=args, data_out=data, transfer_length=len(data))

    def reboot1(self, pc: int = 0, sp: int = 0, delay_ms: int = 0) -> None:
        logger.debug(f"Rebooting device (REBOOT1) with pc=0x{pc:08X}, sp=0x{sp:08X}, delay_ms={delay_ms}")
        args = struct.pack("<III", pc, sp, delay_ms)
        self._send_command(CommandID.REBOOT, args=args, transfer_length=0)

    def reboot2(self, flags: int = 0, delay_ms: int = 0, p0: int = 0, p1: int = 0) -> None:
        logger.debug(f"Rebooting device (REBOOT2) with flags=0x{flags:08X}, delay_ms={delay_ms}, p0=0x{p0:08X}, p1=0x{p1:08X}")
        args = struct.pack("<IIII", flags, delay_ms, p0, p1)
        self._send_command(CommandID.REBOOT2, args=args, transfer_length=0)

    def reboot(self, delay_ms: int = 100) -> None:
        logger.debug(f"Rebooting device with delay_ms={delay_ms}")
        if (self.platform == Platform.RP2040):
            self.reboot1(delay_ms=delay_ms)
        elif (self.platform == Platform.RP2350):
            self.reboot2(delay_ms=delay_ms)

    def exit_xip(self) -> None:
        logger.debug("Exiting XIP mode...")
        self._send_command(CommandID.EXIT_XIP, transfer_length=0)

    def exclusive_access(self) -> None:
        logger.debug("Requesting exclusive access to flash...")
        self._send_command(CommandID.EXCLUSIVE_ACCESS, args=struct.pack("<B", 1), transfer_length=0)

    def _determine_platform(self) -> str:
        logger.debug("Determining device platform...")
        if (hasattr(self, "_platform")) and (self._platform is not None):
            return self._platform
        data = self.flash_read(Addresses.BOOTROM_MAGIC, 4)
        (magic,) = struct.unpack("<I", data)
        return Platform(magic & 0xf0ffffff)

    @property
    def platform(self) -> str:
        return self._platform

    def _guess_flash_size(self) -> int:
        logger.debug("Guessing flash size...")
        if (hasattr(self, "_memory")) and (self._memory is not None):
            return self._memory
        FLASH_BASE = 0x10000000
        PAGE_SIZE = 256

        self.exclusive_access()
        self.exit_xip()

        pages = self.flash_read(FLASH_BASE, 2 * PAGE_SIZE)

        if pages[:PAGE_SIZE] == pages[PAGE_SIZE:]:
            if (pages[:PAGE_SIZE] == b'\xFF' * PAGE_SIZE):
                self.flash_write(FLASH_BASE, b'\x50\x49\x43\x4F' + b'\xFF' * (PAGE_SIZE - 4))
                return self._guess_flash_size()

        candidates = [
            8*1024*1024,
            4*1024*1024,
            2*1024*1024,
            1*1024*1024,
            512*1024,
            256*1024,
        ]

        for size in candidates:
            new_pages = self.flash_read(FLASH_BASE + size, 2 * PAGE_SIZE)
            if new_pages == pages:
                continue
            else:
                return size * 2

        return candidates[-1]

    @property
    def memory(self) -> int:
        return self._memory

    def get_info(self, info_type: InfoType, param0: int = 0, max_len: int = 32) -> bytes:
        logger.debug(f"Getting info of type {info_type} with param0={param0} and max_len={max_len}")
        args = struct.pack("<IIII", info_type, param0, 0, 0)
        data = self._send_command(CommandID.GET_INFO, args=args, transfer_length=max_len)
        return data

    @staticmethod
    def build_diagnostic_partition_info(value: int) -> dict:
        return {
            'value': value,
            'region_searched': bool(value & DiagnosticPartition.REGION_SEARCHED),
            'invalid_block_loops': bool(value & DiagnosticPartition.INVALID_BLOCK_LOOPS),
            'valid_block_loops': bool(value & DiagnosticPartition.VALID_BLOCK_LOOPS),
            'valid_image_defaults': bool(value & DiagnosticPartition.VALID_IMAGE_DEFAULTS),
            'has_partition_table': bool(value & DiagnosticPartition.HAS_PARTITION_TABLE),
            'considered': bool(value & DiagnosticPartition.CONSIDERED),
            'chosen': bool(value & DiagnosticPartition.CHOSEN),
            'partition_table_matching_key_for_verify': bool(value & DiagnosticPartition.PARTITION_TABLE_MATCHING_KEY_FOR_VERIFY),
            'partition_table_hash_for_verify': bool(value & DiagnosticPartition.PARTITION_TABLE_HASH_FOR_VERIFY),
            'partition_table_verified_ok': bool(value & DiagnosticPartition.PARTITION_TABLE_VERIFIED_OK),
            'image_def_matching_key_for_verify': bool(value & DiagnosticPartition.IMAGE_DEF_MATCHING_KEY_FOR_VERIFY),
            'image_def_hash_for_verify': bool(value & DiagnosticPartition.IMAGE_DEF_HASH_FOR_VERIFY),
            'image_def_verified_ok': bool(value & DiagnosticPartition.IMAGE_DEF_VERIFIED_OK),
            'load_map_entries_loaded': bool(value & DiagnosticPartition.LOAD_MAP_ENTRIES_LOADED),
            'image_launched': bool(value & DiagnosticPartition.IMAGE_LAUNCHED),
            'image_condition_failures': bool(value & DiagnosticPartition.IMAGE_CONDITION_FAILURES),
        }

    def get_info_sys(self, flags: SysInfoFlags = SysInfoFlags.CHIP_INFO | SysInfoFlags.CRITICAL | SysInfoFlags.CPU | SysInfoFlags.FLASH | SysInfoFlags.BOOT_RANDOM | SysInfoFlags.BOOT_INFO) -> dict:
        logger.debug(f"Getting system info with flags: {flags}")
        data = self.get_info(InfoType.SYS, param0=flags, max_len=256)
        if len(data) < 24:
            raise PicoBootError("INFO_SYS response too short")

        offset = 0
        (count,rflags,) = struct.unpack_from("<II", data, offset)
        offset += 8
        ret = {}
        if (rflags & SysInfoFlags.CHIP_INFO):
            (chip_info, dev_id_low, dev_id_high) = struct.unpack_from("<III", data, offset)
            offset += 12
            ret['chip_info'] = {
                'package_sel': chip_info,
                'device_id_low': dev_id_low,
                'device_id_high': dev_id_high,
            }
        if (rflags & SysInfoFlags.CRITICAL):
            (critical_flags,) = struct.unpack_from("<I", data, offset)
            offset += 4
            ret['critical_flags'] = {
                'value': critical_flags,
                'secure_boot': bool(critical_flags & CriticalRegister.SECURE_BOOT),
                'secure_debug_disable': bool(critical_flags & CriticalRegister.SECURE_DEBUG_DISABLE),
                'debug_disable': bool(critical_flags & CriticalRegister.DEBUG_DISABLE),
                'default_archsel': bool(critical_flags & CriticalRegister.DEFAULT_ARCHSEL),
                'glitch_detector_enable': bool(critical_flags & CriticalRegister.GLITCH_DETECTOR_ENABLE),
                'glitch_detector_sensitivity': (critical_flags & CriticalRegister.GLITCH_DETECTOR_SENS) >> 5,
                'arm_disable': bool(critical_flags & CriticalRegister.ARM_DISABLE),
                'riscv_disable': bool(critical_flags & CriticalRegister.RISCV_DISABLE),
            }

        if (rflags & SysInfoFlags.CPU):
            (architecture,) = struct.unpack_from("<I", data, offset)
            offset += 4
            ret['architecture'] = architecture
        if (rflags & SysInfoFlags.FLASH):
            (flash_size, ) = struct.unpack_from("<I", data, offset)
            offset += 4
            bits1 = (flash_size & 0xF000) >> 12
            bits0 = (flash_size & 0x0F00) >> 8
            print(bits0, bits1)
            ret['flash_size'] = {
                'slot0': 4096 << bits0 if bits0 != 0 else 0,
                'slot1': 4096 << bits1 if bits1 != 0 else 0,
                'raw': flash_size,
            }
        if (rflags & SysInfoFlags.BOOT_RANDOM):
            (boot_random0, boot_random1, boot_random2, boot_random3) = struct.unpack_from("<IIII", data, offset)
            offset += 16
            ret['boot_random'] = (boot_random0, boot_random1, boot_random2, boot_random3)
        if (rflags & SysInfoFlags.BOOT_INFO):
            (w0, w1, w2, w3) = struct.unpack_from("<IIII", data, offset)
            offset += 16
            d1 = (w1 & 0xFFFF0000) >> 16
            d0 = (w1 & 0x0000FFFF)
            ret['boot_info'] = {
                'tbyb': uint_to_int((w0 & 0xFF000000) >> 24),
                'recent_boot_partition': PartitionInfoType(uint_to_int((w0 & 0x00FF0000) >> 16)),
                'boot_type_recent_boot': uint_to_int((w0 & 0x0000FF00) >> 8),
                'recent_boot_diagnostic_partition': PartitionInfoType(uint_to_int((w0 & 0x000000FF))),
                'recent_boot_diagnostic': uint_to_int((w1 & 0xFFFFFFFF)),
                'last_reboot_param0': uint_to_int(w2),
                'last_reboot_param1': uint_to_int(w3),
                'diagnostic_slot1': PicoBoot.build_diagnostic_partition_info(d1),
                'diagnostic_slot0': PicoBoot.build_diagnostic_partition_info(d0),
            }
        return ret
