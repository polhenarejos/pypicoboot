import enum
from typing import Union

class NamedIntEnum(enum.IntEnum):
    def __str__(self):
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

