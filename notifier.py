import asyncio
from config import FILTERS, TELEGRAM_TOKEN, TELEGRAM_CHAT_IDS
from telegram import Bot


async def send_telegram_message(text):
    """Отправляет сообщение во все настроенные чаты Telegram."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_IDS:
        print("[!] Токен или chat_id не настроены")
        print("--- Сообщение ---")
        print(text)
        return False

    bot = Bot(token=TELEGRAM_TOKEN)
    # Telegram ограничивает сообщение 4096 символами
    if len(text) <= 4096:
        chunks = [text]
    else:
        chunks = []
        current = ""
        for line in text.split("\n"):
            if len(current) + len(line) + 1 > 4000:
                chunks.append(current)
                current = line + "\n"
            else:
                current += line + "\n"
        if current:
            chunks.append(current)

    delivery_succeeded = True
    for chat_id in TELEGRAM_CHAT_IDS:
        for chunk in chunks:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode="Markdown",
                    disable_web_page_preview=False,
                )
            except Exception as exc:
                delivery_succeeded = False
                print(f"[!] Не удалось отправить в чат {chat_id}: {exc}")
            await asyncio.sleep(0.5)
    return delivery_succeeded


def format_report(analyses):
    """Форматирует результаты в текст для Telegram с актуальными ссылками."""
    lines = []
    lines.append("🔍 *Подтверждённые предложения рынка*")
    lines.append(f"Найдено объявлений: {len(analyses)}")
    lines.append(f"Показано топ-{len(analyses)}")
    lines.append("──────────────────────────")
    lines.append("")

    if not analyses:
        lines.append(
            "Пока нет объявлений с подтверждённой скидкой к рынку. "
            f"Для оценки нужно минимум {FILTERS['market_min_samples']} сопоставимых объявлений."
        )

    for i, a in enumerate(analyses, 1):
        source_labels = {"avito": "Avito", "autoru": "Auto.ru", "drom": "Drom"}
        source = source_labels.get(a.get("site", ""), a.get("site") or "не указан")
        lines.append(f"*#{i} — {a['priority']}*")
        lines.append(f"Источник: {source}")
        lines.append(f"🚗 {a['brand']} {a['model']} {a['year']}, {a['mileage']:,} км".replace(",", " "))
        lines.append(f"💰 Цена: {a['price']:,} руб".replace(",", " "))

        if a["market_price"]:
            lines.append(
                f"📊 Рынок: медиана {a['market_price']:,} руб "
                f"по {a['market_samples']} аналогам ({a['market_year_range']} г.)"
                .replace(",", " ")
            )
            match_details = ["марка/модель"]
            if a.get("generation"):
                match_details.append(f"поколение {a['generation']}")
            if a.get("body"):
                match_details.append(f"кузов {a['body']}")
            if a.get("engine") or a.get("engine_volume"):
                engine = " ".join(str(value) for value in (a.get("engine"), a.get("engine_volume")) if value)
                match_details.append(f"двигатель {engine}")
            if a.get("transmission"):
                match_details.append(f"КПП {a['transmission']}")
            match_details.append("сопоставимый пробег")
            lines.append(f"🔎 Сопоставимость: {', '.join(match_details)}")
            market_site_labels = {
                "avito": "Avito",
                "autoru": "Auto.ru",
                "drom": "Drom",
            }
            market_sites = [
                market_site_labels.get(site, site)
                for site in a.get("market_sites", [])
            ]
            if market_sites:
                lines.append(f"Источники аналогов: {', '.join(market_sites)}")
            lines.append(
                f"📉 Ниже рынка: {a['discount_pct']}% "
                f"({a['discount_amount']:,} руб)".replace(",", " ")
            )
        else:
            lines.append(
                f"📊 Рынок: есть {a['market_samples']} аналогов, "
                f"нужно {a['market_min_samples']}"
            )
            comparables = a.get("comparables") or []
            if comparables:
                lines.append("🔍 Найденные аналоги (для справки):")
                for comparable in comparables[:3]:
                    lines.append(
                        f"   • {comparable['brand']} {comparable['model']} {comparable['year']}, "
                        f"{comparable['mileage']:,} км, {comparable['price']:,} руб".replace(",", " ")
                    )
                    if comparable.get("url"):
                        lines.append(f"     🔗 [Открыть]({comparable['url']})")

        if a["red_flags"]:
            lines.append(f"🚩 Красные флаги: {', '.join(a['red_flags'])}")

        lines.append(f"🎯 {a['priority']}")
        lines.append(f"📝 {a['recommendation']}")

        # Актуальная ссылка на объявление
        if a["url"]:
            lines.append(f"🔗 [Открыть объявление]({a['url']})")

        lines.append("──────────────────────────")
        lines.append("")

    return "\n".join(lines)


def send_report_sync(analyses):
    """Синхронная обёртка для отправки отчёта."""
    text = format_report(analyses)
    asyncio.run(send_telegram_message(text))
