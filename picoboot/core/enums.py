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

import enum
from typing import Union

class NamedIntEnum(enum.IntEnum):
    def __str__(self):
        return self.name

    def __format__(self, fmt):
        if any(c in fmt for c in "xXod"):
            return format(self.value, fmt)
        return self.name

    @classmethod
    def from_string(cls, value: Union[str, int]) -> "NamedIntEnum":
        if not value:
            return cls.UNKNOWN

        value = value.strip().lower()

        for member in cls:
            if member.value == value or member.name.lower() == value:
                return member

        return cls.UNKNOWN

