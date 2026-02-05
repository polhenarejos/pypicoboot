
from .core.enums import NamedIntEnum


class Platform(NamedIntEnum):
    RP2040  = 0x01754d
    RP2350  = 0x02754d
    ESP32S3 = 0x03754d
    ESP32S2 = 0x04754d
    ESP32C3 = 0x05754d
    UNKNOWN = 0x000000