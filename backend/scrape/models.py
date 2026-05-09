from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class Event:
    source: str
    title: str
    date: str
    time: str
    location: str
    url: str
    composer: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)
