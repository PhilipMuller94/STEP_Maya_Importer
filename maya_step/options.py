"""Options shared by Maya and the external converter (no Maya dependency)."""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class ImportOptions:
    linear_deflection: float = 0.1  # millimetres, after STEP unit conversion
    angular_deflection: float = 20.0  # degrees
    scale: float = 1.0
    source_up: str = "Z"
    timeout: float = 600.0
    use_cache: bool = True

    def __post_init__(self):
        if type(self.use_cache) is not bool:
            raise ValueError("use_cache must be True or False")
        for name in ("linear_deflection", "angular_deflection", "scale", "timeout"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError("{} must be a finite, positive number".format(name))
            object.__setattr__(self, name, value)
        if self.angular_deflection > 180:
            raise ValueError("angular_deflection must be at most 180 degrees")
        object.__setattr__(self, "source_up", self.source_up.upper())
        if self.source_up not in ("Y", "Z"):
            raise ValueError("source_up must be Y or Z")

    @classmethod
    def from_string(cls, text):
        values = {}
        allowed = cls.__dataclass_fields__
        for item in text.split(";"):
            if not item.strip():
                continue
            key, separator, value = item.partition("=")
            key, value = key.strip(), value.strip()
            if not separator or key not in allowed:
                raise ValueError("Unknown STEP import option: {}".format(item))
            if key == "use_cache":
                if value.lower() not in ("true", "false", "1", "0"):
                    raise ValueError("use_cache must be True or False")
                values[key] = value.lower() in ("true", "1")
            else:
                values[key] = value if key == "source_up" else float(value)
        return cls(**values)

    def to_string(self):
        return ";".join("{}={}".format(key, getattr(self, key))
                        for key in self.__dataclass_fields__)


DEFAULT_OPTIONS = ImportOptions()
