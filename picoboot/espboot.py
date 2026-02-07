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

from __future__ import annotations

from contextlib import ExitStack
from enum import Enum
from typing import Iterable, Optional

from esptool.cmds import attach_flash, detect_chip, flash_id, reset_chip, run_stub, write_flash

from .core.log import get_logger
from .core.exceptions import EspBootError, EspBootNotFoundError
from .espbootmonitor import EspBootMonitor, EspBootMonitorObserver
from .platform import Platform

logger = get_logger("EspBoot")

DEFAULT_CONNECT_ATTEMPTS = 1
ESP_SERIAL_VIDS = {
    0x303A,  # Espressif
    0x10C4,  # Silicon Labs CP210x
    0x1A86,  # WCH CH34x/CH910x
    0x0403,  # FTDI
    0x067B,  # Prolific
}
ESP_SERIAL_KEYWORDS = ("esp", "cp210", "ch340", "ch910", "ftdi", "silicon labs")


class EspBoot:
    def __init__(self, port: str, esp, stack: ExitStack) -> None:
        self.port = port
        self._esp = esp
        self._stack = stack

    @staticmethod
    def _list_serial_ports() -> list[str]:
        try:
            from serial.tools import list_ports
        except Exception as e:  # pragma: no cover
            raise EspBootError("pyserial is required for auto port detection") from e
        ports = list(list_ports.comports())
        if not ports:
            return []
        preferred = []
        others = []
        for p in ports:
            desc = " ".join(filter(None, [
                getattr(p, "description", ""),
                getattr(p, "manufacturer", ""),
                getattr(p, "product", ""),
            ])).lower()
            vid = getattr(p, "vid", None)
            if (vid in ESP_SERIAL_VIDS) or any(k in desc for k in ESP_SERIAL_KEYWORDS):
                preferred.append(p.device)
            else:
                others.append(p.device)
        return preferred + others

    @classmethod
    def _auto_detect_port(cls, stack: ExitStack):
        ports = cls._list_serial_ports()
        if not ports:
            raise EspBootNotFoundError("No serial ports found for ESP32 detection")
        for port in ports:
            try:
                esp = stack.enter_context(detect_chip(port, connect_attempts=DEFAULT_CONNECT_ATTEMPTS))
                logger.debug(f"ESP32 detected on port {port}")
                return port, esp
            except Exception:
                continue
        raise EspBootNotFoundError("No ESP32 device detected. Pass port explicitly.")

    @classmethod
    def _is_port_present(cls, port: str) -> bool:
        try:
            ports = cls._list_serial_ports()
        except EspBootError:
            return False
        return port in ports

    @classmethod
    def open(cls, port: Optional[str] = None, run_stub_flasher: bool = True) -> "EspBoot":
        stack = ExitStack()
        try:
            try:
                from esptool.logger import log as esptool_log
                esptool_log.set_verbosity("silent")
            except Exception:
                pass
            if port is None or port == "auto":
                port, esp = cls._auto_detect_port(stack)
            else:
                esp = stack.enter_context(detect_chip(port, connect_attempts=DEFAULT_CONNECT_ATTEMPTS))
            if run_stub_flasher:
                esp = run_stub(esp)
            attach_flash(esp)
        except Exception as e:
            stack.close()
            raise EspBootError(f"Failed to open ESP32 on port {port}: {e}") from e
        logger.info(f"ESP32 opened on port {port}")
        device = cls(port, esp, stack)
        device._flash_size = None
        try:
            from esptool.cmds import _get_flash_info  # type: ignore
            _, _, size = _get_flash_info(esp)
            device._flash_size = cls._flash_size_to_bytes(size)
        except Exception:
            pass

        class EspBootObserver(EspBootMonitorObserver):
            def __init__(self, dev: EspBoot):
                self.__device = dev

            def update(self, actions: tuple[Optional[str], Optional[str]]) -> None:
                (connected, disconnected) = actions
                if connected:
                    logger.debug("ESP32 device connected")
                if disconnected:
                    logger.debug("ESP32 device disconnected")
                    self.__device.close()

        device.__observer = EspBootObserver(device)
        device.__monitor = EspBootMonitor(port=port, cls_callback=device.__observer)
        return device

    def close(self) -> None:
        if self._stack is not None:
            if hasattr(self, "_EspBoot__monitor") and self.__monitor is not None:
                self.__monitor.stop()
            self._stack.close()
            if hasattr(self, "_EspBoot__monitor"):
                self.__monitor = None
            self._stack = None
            self._esp = None
            logger.debug("ESP32 connection closed")

    def __enter__(self) -> "EspBoot":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def chip_name(self) -> str:
        if self._esp is None:
            raise EspBootError("Device not connected")
        return getattr(self._esp, "CHIP_NAME", "unknown")

    @property
    def chip_id(self) -> Optional[int]:
        if self._esp is None:
            raise EspBootError("Device not connected")
        get_id = getattr(self._esp, "get_chip_id", None)
        if callable(get_id):
            try:
                return get_id()
            except Exception:
                return None
        return getattr(self._esp, "chip_id", None)

    def _determine_platform(self) -> Platform:
        name = self.chip_name.lower()
        if "esp32-s3" in name:
            return Platform.ESP32S3
        if "esp32-s2" in name:
            return Platform.ESP32S2
        if "esp32c3" in name:
            return Platform.ESP32C3
        if "esp32" in name:
            return Platform.ESP32
        return Platform.UNKNOWN

    @property
    def platform(self) -> Platform:
        return self._determine_platform()

    def set_serial_number_str(self, serial: str) -> None:
        self._cached_serial_number = bytes.fromhex(serial[:12])[::-1]

    @property
    def serial_number_str(self) -> str:
        if self._esp is None:
            raise EspBootError("Device not connected")
        mac = None
        if hasattr(self._esp, "read_mac"):
            try:
                mac = self._esp.read_mac()
            except Exception:
                mac = None
        if mac is None:
            if hasattr(self, "_cached_serial_number"):
                mac = self._cached_serial_number
            else:
                mac = b"\x00" * 6
        if isinstance(mac, int):
            mac = mac.to_bytes(6, "big")
        if isinstance(mac, (bytes, bytearray)):
            mac_bytes = mac
        else:
            mac_bytes = bytes(mac)
        mac_bytes = b"\x00" * (8 - len(mac_bytes)) + mac_bytes  # Pad to 8 bytes if shorter
        if len(mac_bytes) != 8:
            raise EspBootError("Invalid MAC address length")
        # Return little-endian without separators to match expected format.
        return "".join(f"{b:02X}" for b in mac_bytes[::-1])

    def get_chip_info(self) -> dict:
        if self._esp is None:
            raise EspBootError("Device not connected")
        info = {
            "chip_name": self.chip_name,
            "chip_id": self.chip_id,
            "platform": self.platform.value,
            "port": self.port,
        }
        get_desc = getattr(self._esp, "get_chip_description", None)
        if callable(get_desc):
            try:
                info["description"] = get_desc()
            except Exception:
                pass
        get_features = getattr(self._esp, "get_chip_features", None)
        if callable(get_features):
            try:
                info["features"] = get_features()
            except Exception:
                pass
        return info

    def get_flash_size(self):
        if self._esp is None:
            raise EspBootError("Device not connected")
        if getattr(self, "_flash_size", None) is not None:
            return self._flash_size
        size = None
        try:
            from esptool.cmds import _get_flash_info  # type: ignore
        except Exception:
            _get_flash_info = None
        if _get_flash_info is not None:
            try:
                _, _, size = _get_flash_info(self._esp)
            except Exception:
                size = None
        if size is None:
            for attr in ("flash_size", "FLASH_SIZE", "FLASH_SIZE_BYTES"):
                if hasattr(self._esp, attr):
                    size = getattr(self._esp, attr)
                    break
        if size is None:
            try:
                flash_id(self._esp)  # Emits detected flash size via esptool logger.
            except Exception:
                pass
            raise EspBootError("Unable to determine flash size programmatically")
        size = self._flash_size_to_bytes(size)
        if size is None:
            raise EspBootError("Unable to determine flash size programmatically")
        self._flash_size = size
        return size

    @property
    def memory(self) -> Optional[int]:
        try:
            return self.get_flash_size()
        except EspBootError:
            return 0

    @staticmethod
    def _flash_size_to_bytes(size):
        if size is None:
            return None
        if isinstance(size, int):
            return size
        if isinstance(size, str):
            s = size.strip().upper()
            try:
                if s.endswith("MB"):
                    return int(s[:-2]) * 1024 * 1024
                if s.endswith("KB"):
                    return int(s[:-2]) * 1024
                if s.endswith("B"):
                    return int(s[:-1])
                return int(s)
            except Exception:
                return None
        try:
            return int(size)
        except Exception:
            return None

    def is_connected(self) -> bool:
        if self.port is None:
            return False
        return self._is_port_present(self.port)

    def write_flash_files(self, segments: Iterable[tuple[int, str]]) -> None:
        if self._esp is None:
            raise EspBootError("Device not connected")
        opened = []
        try:
            for addr, path in segments:
                f = open(path, "rb")
                opened.append((addr, f))
            write_flash(self._esp, opened)
        finally:
            for _, f in opened:
                try:
                    f.close()
                except Exception:
                    pass

    def write_flash(self, addr: int, data: bytes) -> None:
        if self._esp is None:
            raise EspBootError("Device not connected")
        write_flash(self._esp, [(addr, data)])

    def reset(self, mode: str = "hard-reset") -> None:
        if self._esp is None:
            raise EspBootError("Device not connected")
        try:
            reset_chip(self._esp, mode)
        except Exception:
            pass

    def reboot(self, bootsel: bool = False) -> None:
        if self._esp is None:
            raise EspBootError("Device not connected")
        try:
            reset_chip(self._esp, "reset" if bootsel else "hard-reset")
        except Exception:
            pass
