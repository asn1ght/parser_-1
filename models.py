from dataclasses import dataclass, field
from typing import Optional

@dataclass
class CarAd:
    title: str
    price: int
    year: Optional[int] = None
    mileage: Optional[int] = None
    location: str = ""
    description: str = ""
    url: str = ""
    source: str = ""
    image_url: str = ""
    seller_name: str = ""
    # Аналитика
    market_price: int = 0
    prep_bonus: int = 0
    total_cost: int = 0
    estimated_profit: int = 0
    margin_pct: float = 0.0
    condition_score: int = 0
    red_flags: list = field(default_factory=list)
    good_signs: list = field(default_factory=list)
    priority: str = ""
    recommendation: str = ""
