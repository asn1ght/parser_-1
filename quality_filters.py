import re


COMPANY_SELLER_MARKERS = (
    "автосалон",
    "автоцентр",
    "дилерский центр",
    "официальный дилер",
    "отдел продаж",
    "группа компаний",
    "юридическое лицо",
    "комиссионная площадка",
    "компания по продаже",
)
LEGAL_ENTITY_PATTERN = re.compile(r"\b(?:ооо|ао|пао|зао|ип)\b", re.IGNORECASE)
NEGATED_DAMAGE_PATTERN = re.compile(
    r"\bне\s+(?:(?:был[аои]?|является)\s+)?(?:бит\w*(?:\s+в\s+тотал)?|тотал(?:ьн\w*)?)\b",
    re.IGNORECASE,
)
COMPANY_DESCRIPTION_PATTERNS = (
    re.compile(r"\b(?:автосалон|автоцентр|компания|дилер)\s+(?:предлагает|предлагаем|продает|продаем|продажа)\b", re.IGNORECASE),
    re.compile(r"\b(?:продаем|продаем|продажа от|предлагаем)\b.{0,120}\b(?:автосалон|автоцентр|дилер|компан)", re.IGNORECASE),
    re.compile(r"\b(?:автосалон|автоцентр|дилерский центр|официальный дилер)\b.{0,160}\b(?:продаж|предлагаем|наши автомобили)", re.IGNORECASE),
    re.compile(r"\b(?:автомобильная компания|группа компаний)\b.{0,250}\b(?:продаж|продаем|автомобил)", re.IGNORECASE),
    re.compile(r"\b(?:ооо|ао|пао|зао|ип)\b.{0,160}\b(?:продаж|автомобил|авто)", re.IGNORECASE),
)
PRIVATE_SELLER_MARKERS = (
    "частное лицо",
    "частный продавец",
    "частник",
    "физическое лицо",
    "собственник",
    "от собственника",
    "продам",
    "продаю",
    "из первых рук",
    "продаю свой автомобиль",
    "продаю личный автомобиль",
    "личный автомобиль",
    "я владелец",
    "я собственник",
    "владею автомобилем",
)
PERSON_NAME_PATTERN = re.compile(
    r"^[A-ZА-ЯЁ][a-zа-яё-]+(?:['-][A-ZА-ЯЁ]?[a-zа-яё-]+)*"
    r"(?:\s+(?:[A-ZА-ЯЁ]\. |[A-ZА-ЯЁ]\.|[A-ZА-ЯЁ][a-zа-яё-]+)){1,2}$",
    re.IGNORECASE,
)
NON_PERSON_SELLER_WORDS = {
    "авто", "автомобили", "автосалон", "дилер", "центр", "компания",
    "моторс", "cars", "auto", "dealer", "официальный", "продажа",
}
AVITO_GIVEN_NAMES = {
    "александр", "алексей", "андрей", "артем", "артём", "сергей", "дмитрий",
    "иван", "михаил", "николай", "павел", "роман", "евгений", "владимир",
    "максим", "олег", "денис", "антон", "игорь", "константин", "кирилл",
    "никита", "егор", "арсений", "тимур", "виктор", "юрий", "руслан",
    "станислав", "василий", "федор", "фёдор", "ярослав", "мария", "анна",
    "елена", "наталья", "ольга", "татьяна", "ирина", "светлана", "екатерина",
    "дарья", "настя", "анастасия", "юлия", "валерия", "алиса", "полина",
}
TOTAL_LOSS_PATTERNS = (
    re.compile(r"\bтотал(?:ьн\w*)?\b", re.IGNORECASE),
    re.compile(r"\b(?:под восстановление|восстановлению не подлежит|на запчасти)\b", re.IGNORECASE),
    re.compile(r"\b(?:сильно|серьезно|сильно) бит\w*\b", re.IGNORECASE),
    re.compile(r"\b(?:серьезн\w*|сильн\w*) дтп\b", re.IGNORECASE),
    re.compile(r"\b(?:не на ходу|геометрия кузова нарушена|сработали подушки)\b", re.IGNORECASE),
)


def rejection_reasons(ad):
    reasons = []
    seller_type = str(ad.get("seller_type") or "").casefold()
    seller_text = " ".join(
        str(ad.get(key) or "")
        for key in ("seller", "seller_name", "seller_type")
    ).casefold().replace("ё", "е")
    description = " ".join(
        str(ad.get(key) or "") for key in ("description", "info")
    ).casefold().replace("ё", "е")

    if ad.get("seller_verified_company") or seller_type in {"company", "dealer", "business"}:
        reasons.append("компания или автосалон")
    elif LEGAL_ENTITY_PATTERN.search(seller_text) or any(
        marker in seller_text for marker in COMPANY_SELLER_MARKERS
    ):
        reasons.append("компания или автосалон")
    elif any(pattern.search(description) for pattern in COMPANY_DESCRIPTION_PATTERNS):
        reasons.append("объявление автосалона или компании")

    searchable_text = NEGATED_DAMAGE_PATTERN.sub("", " ".join(
        str(ad.get(key) or "") for key in ("title", "description", "info")
    ))
    if any(pattern.search(searchable_text) for pattern in TOTAL_LOSS_PATTERNS):
        reasons.append("тотал или тяжелые повреждения")
    if not has_private_seller_signal(ad):
        reasons.append("не подтвержден частный продавец")

    return reasons


def has_private_seller_signal(ad):
    seller_type = str(ad.get("seller_type") or "").casefold().replace("ё", "е")
    if seller_type in {"private", "person", "owner", "частное лицо", "частник", "владелец"}:
        return True

    seller_text = " ".join(
        str(ad.get(key) or "") for key in ("seller", "seller_name", "name", "seller_type")
    ).casefold().replace("ё", "е")
    description = " ".join(
        str(ad.get(key) or "") for key in ("description", "info")
    ).casefold().replace("ё", "е")

    if any(marker in seller_text for marker in PRIVATE_SELLER_MARKERS):
        return True
    if any(marker in description for marker in PRIVATE_SELLER_MARKERS):
        return True

    display_name = str(ad.get("seller_name") or ad.get("name") or "").strip()
    if ad.get("site") == "avito" and display_name.casefold() in AVITO_GIVEN_NAMES:
        return True
    if LEGAL_ENTITY_PATTERN.search(display_name) or any(
        marker in display_name.casefold() for marker in COMPANY_SELLER_MARKERS
    ):
        return False
    return bool(
        PERSON_NAME_PATTERN.fullmatch(display_name)
        and not any(word.casefold() in NON_PERSON_SELLER_WORDS for word in display_name.split())
    )


def is_acceptable_private_car(ad):
    return not rejection_reasons(ad)