class SourceUnavailableError(RuntimeError):
	"""Raised when a marketplace blocks a search request."""


def ensure_source_available(source, status_code, body):
	if status_code is not None and status_code >= 400:
		raise SourceUnavailableError(f"[{source}] HTTP {status_code}; доступ к выдаче отклонён")

	body_lower = body.casefold()
	block_markers = (
		"доступ с вашего ip",
		"ошибка доступа с ip",
		"сайт заблокирован",
		"подозрительный трафик",
		"проверка безопасности",
		"captcha",
		"капча",
	)
	if any(marker in body_lower for marker in block_markers):
		raise SourceUnavailableError(f"[{source}] сайт вернул страницу блокировки вместо объявлений")
