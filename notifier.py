import asyncio
from config import FILTERS, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
from telegram import Bot


async def send_telegram_message(text):
    """Отправляет сообщение в Telegram."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[!] Токен или chat_id не настроены в config.py")
        print("--- Сообщение ---")
        print(text)
        return

    bot = Bot(token=TELEGRAM_TOKEN)
    # Telegram ограничивает сообщение 4096 символами
    if len(text) <= 4096:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            parse_mode="Markdown",
            disable_web_page_preview=False,
        )
    else:
        # Разбиваем на части
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

        for chunk in chunks:
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=chunk,
                parse_mode="Markdown",
                disable_web_page_preview=False,
            )
            await asyncio.sleep(0.5)


def format_report(analyses):
    """Форматирует результаты в текст для Telegram с актуальными ссылками."""
    lines = []
    lines.append("🔍 *Парсер авто — новый заход*")
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
            if not a["market_mileage_matched"]:
                lines.append("⚠️ Сопоставимого пробега мало; оценка только по модели и году")
            lines.append(
                f"📉 Ниже рынка: {a['discount_pct']}% "
                f"({a['discount_amount']:,} руб)".replace(",", " ")
            )
            lines.append(f"💸 Оценка после расходов: {a['profit']:,} руб".replace(",", " "))
            if a.get("alternatives"):
                lines.append("🔁 Дешевле в глобальном поиске:")
                for alt in a["alternatives"][:3]:
                    lines.append(
                        f"   • {alt['brand']} {alt['model']} {alt['year']}, "
                        f"{alt['mileage']:,} км, {alt['price']:,} руб".replace(",", " ")
                    )
                    if alt.get("url"):
                        lines.append(f"     🔗 [Открыть]({alt['url']})")
        else:
            lines.append(
                f"📊 Рынок: есть {a['market_samples']} аналогов, "
                f"нужно {a['market_min_samples']}"
            )

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
