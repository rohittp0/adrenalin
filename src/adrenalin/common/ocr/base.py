from __future__ import annotations

from dataclasses import dataclass, field


class OcrUnavailable(RuntimeError):
    """No usable OCR engine on this machine."""


@dataclass
class OcrLine:
    text: str
    confidence: float
    # (x, y, w, h), normalised 0-1, origin top-left
    box: tuple[float, float, float, float]


@dataclass
class OcrResult:
    lines: list[OcrLine] = field(default_factory=list)
    method: str = ""

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def confidence(self) -> float:
        if not self.lines:
            return 0.0
        return sum(line.confidence for line in self.lines) / len(self.lines)
