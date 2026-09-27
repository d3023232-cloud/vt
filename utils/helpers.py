"""Вспомогательные функции: форматирование времени, состояния подика и текстов."""

from datetime import datetime, timedelta
from typing import Any

from config import config


def parse_dt(value: Any) -> datetime | None:
    """Разбирает значение TIMESTAMP из SQLite в datetime (UTC).

    Args:
        value: строка вида 'YYYY-MM-DD HH:MM:SS', datetime или None.

    Returns:
        Объект datetime в наивном UTC-времени либо None.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        # Приводим aware-datetime к наивному UTC для единообразия с CURRENT_TIMESTAMP
        if value.tzinfo is not None:
            return value.astimezone(tz=None).replace(tzinfo=None)
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
    return None


def now_utc() -> datetime:
    """Текущее время в формате SQLite (наивной UTC-таймстемп).

    Все таймстемпы в БД пишутся через strftime('%Y-%m-%d %H:%M:%S') и
    сравниваются с strftime('now') — оба значения в UTC.
    """
    return datetime.utcnow()


def fmt_dt(dt: datetime) -> str:
    """Форматирует datetime в строку для записи/сравнения с SQLite (UTC)."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def humanize_delta(target: datetime | None) -> str:
    """Возвращает человекочитаемую строку 'осталось X мин Y сек' до момента target.

    Args:
        target: момент времени (UTC) или None.

    Returns:
        Строка вида '15 мин 30 сек', '2 ч 5 мин', '0 сек' или '—'.
    """
    if target is None:
        return "—"
    delta_seconds = int((target - now_utc()).total_seconds())
    if delta_seconds <= 0:
        return "0 сек"
    hours, rem = divmod(delta_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} ч")
    if minutes:
        parts.append(f"{minutes} мин")
    if not hours:
        parts.append(f"{seconds} сек")
    return " ".join(parts) if parts else "0 сек"


def condition_emoji(condition: int) -> str:
    """Цветовая индикация состояния подика.

    Зеленый — больше 70%, жёлтый — от 30% до 70%, красный — меньше 30%.
    """
    if condition > 70:
        return "🟢"
    if condition >= 30:
        return "🟡"
    return "🔴"


def power_emoji(power: int) -> str:
    """Индикация мощности подика: чем выше — тем «злее» смайлик."""
    if power >= 50:
        return "🔥"
    if power >= 20:
        return "⚡"
    return "💧"


def format_stats_block(
    name: str | None = None,
    multiplier: int | None = None,
    power: int | None = None,
    max_puffs: int | None = None,
    condition: int | None = None,
    current_puffs: int | None = None,
    quantity: int | None = None,
    lots_sold: int | None = None,
    indent: str = "",
) -> str:
    """Единый формат статистики подика/лота — каждая строка с новой строки и со своим смайликом.

    Используется ВЕЗДЕ: карточки в боте (главный экран, инвентарь, экран аукциона),
    посты в канале аукционов и превью в админ-панели. Параметры, равные None,
    просто не выводятся.

    Смайлики: 💨 название | 💠 множитель | ⚡/🔥/💧 мощность | 🛢 бак |
    🔋/🟢🟡🔴 состояние | 📦 партия.

    Args:
        name: название подика/лота (выводится жирным, если задано).
        multiplier: множитель дохода (x N).
        power: мощность в затяжках в минуту.
        max_puffs: объём бака.
        condition: состояние в процентах (0..100).
        current_puffs: текущая заполненность бака (если None — бак показывается как ёмкость).
        quantity: количество штук в партии (для аукционных лотов).
        lots_sold: сколько лотов уже продано (для аукционных лотов).
        indent: префикс-отступ для всех строк блока.

    Returns:
        Готовая HTML-строка блока (строки разделены \\n).
    """
    lines: list[str] = []
    if name is not None:
        lines.append(f"{indent}💨 <b>{name}</b>")
    if multiplier is not None:
        lines.append(f"{indent}💠 Множитель: x{int(multiplier)}")
    if power is not None:
        p = int(power)
        lines.append(f"{indent}{power_emoji(p)} Мощность: {p} зат./мин")
    if max_puffs is not None:
        mp = int(max_puffs)
        if current_puffs is not None:
            lines.append(f"{indent}🛢 Бак: {int(current_puffs)}/{mp} затяжек")
        else:
            lines.append(f"{indent}🛢 Бак: {mp} затяжек")
    if condition is not None:
        c = min(100, max(0, int(condition)))
        lines.append(f"{indent}🔋 Состояние: {condition_emoji(c)} {c}%")
    if quantity is not None:
        sold_part = f", продано: {int(lots_sold)}" if lots_sold is not None else ""
        lines.append(f"{indent}📦 Партия: {int(quantity)} шт{sold_part}")
    return "\n".join(lines)


def format_vape(vape: dict[str, Any]) -> str:
    """Формирует карточку подика для отображения в инвентаре / основном экране."""
    cond: int = int(vape["condition"])
    current_puffs: int = int(vape["current_puffs"])
    lines: list[str] = [
        f"{'🔧' if vape['is_broken'] else '💨'} <b>{vape['name']}</b>"
        + (" <i>(сломан)</i>" if vape["is_broken"] else "")
        + (" <i>[экипирован]</i>" if vape["is_equipped"] else ""),
        format_stats_block(
            multiplier=vape["multiplier"],
            power=vape["power"],
            max_puffs=vape["max_puffs"],
            condition=cond,
            current_puffs=current_puffs,
            indent="   ",
        ),
    ]
    if vape["is_broken"]:
        lines.append("   ⛔ Подик сломан, его можно выбросить.")
    elif current_puffs == 0 and parse_dt(vape.get("regen_at")) is not None:
        regen_dt = parse_dt(vape["regen_at"])
        assert regen_dt is not None
        lines.append(f"   ♻️ Регенерация бака через: {humanize_delta(regen_dt)}")
    return "\n".join(lines)


def channel_mention() -> str:
    """Строка с упоминанием канала аукционов (username из .env)."""
    if config.auction_channel_username:
        return f"@{config.auction_channel_username.lstrip('@')}"
    return "канал аукционов"


def calc_vapors(current_puffs: int, puffs_per_vapor: int, multiplier: int) -> int:
    """Вычисляет количество Паров: затяжки × Паров за затяжку × множитель.

    Множитель устройства увеличивает доход добычи.
    """
    return max(0, current_puffs) * max(1, puffs_per_vapor) * max(1, multiplier)


def add_minutes(minutes: int) -> datetime:
    """Возвращает момент «сейчас + N минут» (UTC)."""
    return now_utc() + timedelta(minutes=minutes)
