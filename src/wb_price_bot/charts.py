from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .domain import format_money
from .models import PriceHistory


def render_price_chart(rows: list[PriceHistory], *, title: str, days: int) -> bytes:
    points = [(row.observed_at, row.price) for row in rows if row.price is not None]
    if len(points) < 2:
        raise ValueError("Для графика нужны хотя бы две точки цены")

    width, height = 1200, 700
    left, top, right, bottom = 105, 95, 55, 100
    image = Image.new("RGB", (width, height), "#10131a")
    draw = ImageDraw.Draw(image)
    font = _font(25)
    small = _font(20)
    bold = _font(31)
    draw.text((left, 30), f"{title[:62]} — {days} дней", fill="#f7f8fa", font=bold)

    prices = [price for _, price in points]
    minimum, maximum = min(prices), max(prices)
    spread = max(100, maximum - minimum)
    minimum = max(0, minimum - spread // 10)
    maximum += spread // 10
    chart_width = width - left - right
    chart_height = height - top - bottom

    for index in range(6):
        y = top + chart_height * index / 5
        draw.line((left, y, width - right, y), fill="#2b3240", width=1)
        value = round(maximum - (maximum - minimum) * index / 5)
        draw.text((10, y - 12), format_money(value), fill="#9aa5b5", font=small)

    first_time, last_time = points[0][0], points[-1][0]
    duration = max(1.0, (last_time - first_time).total_seconds())

    def coordinate(item: tuple[object, int]) -> tuple[float, float]:
        observed, price = item
        elapsed = (observed - first_time).total_seconds()  # type: ignore[operator]
        x = left + chart_width * elapsed / duration
        y = top + chart_height * (maximum - price) / max(1, maximum - minimum)
        return x, y

    coordinates = [coordinate(item) for item in points]
    draw.line(coordinates, fill="#8b5cf6", width=5, joint="curve")
    for x, y in coordinates:
        draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill="#c4b5fd")

    for index in range(5):
        moment = first_time + (last_time - first_time) * index / 4
        x = left + chart_width * index / 4
        label = moment.strftime("%d.%m")
        draw.text((x - 28, height - 70), label, fill="#9aa5b5", font=small)

    draw.text(
        (left, height - 35),
        f"Минимум: {format_money(min(prices))}   Максимум: {format_money(max(prices))}",
        fill="#d7dce5",
        font=font,
    )
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()
