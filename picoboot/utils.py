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

def uint_to_int(value: int, bits: int = 8) -> int:
    """Interpret the unsigned integer `value` as a signed integer with `bits` bits."""
    mask = (1 << bits) - 1
    value &= mask                     # ensure value fits in `bits`
    sign_bit = 1 << (bits - 1)
    return value - (1 << bits) if (value & sign_bit) else value

def crc32_ieee(data: bytes) -> int:
    crc = 0xFFFFFFFF

    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xEDB88320
            else:
                crc >>= 1

    return crc ^ 0xFFFFFFFF

