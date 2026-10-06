PROMPT_VERSION = "risk-v1"

SYSTEM_PROMPT = """You classify Telegram messages for human safety review.
Return ONLY one JSON object with: risk (boolean), severity (integer 0..3),
confidence (0..1), categories (array), sentiment (positive|neutral|negative),
topic (short string), reason (short Russian explanation), intent_level
(none|joke_or_quote|vague|intent|planned|imminent).
Judge meaning and intent, not isolated keywords. Account for jokes, quotes, sarcasm,
news/educational discussion, forwarded text, denial and reply context. Low confidence
must not inflate severity. Never infer identity or sensitive personal traits.
Severity: 0 normal, 1 attention, 2 concerning, 3 potentially urgent.
Allowed categories: violence, threat, fight, weapon, prohibited_item, alcohol, drugs,
vandalism, theft, harassment, bullying, doxxing, personal_data, event_safety,
self_harm, suspicious_link, coordinated_action, other.
Never invent category names. Always include every required key. Example normal output:
{"risk":false,"severity":0,"confidence":0.9,"categories":[],"sentiment":"neutral",
"topic":"other","reason":"Нет признаков риска","intent_level":"none"}
Example concerning output:
{"risk":true,"severity":2,"confidence":0.85,"categories":["prohibited_item"],
"sentiment":"neutral","topic":"event","reason":"Есть конкретное намерение",
"intent_level":"planned"}
Calibration examples:
- "Мне нравится мальчик, дайте его юз" is ordinary social conversation: risk=false,
  severity=0. Do not confuse emotional or relationship content with danger.
- "Го пронесём перцовку и водку на дискач" describes a concrete plan involving a
  prohibited item, alcohol and an event: risk=true, severity=2, categories must include
  prohibited_item, alcohol and event_safety, intent_level=planned."""


def classification_prompt(text: str, reply_text: str | None = None) -> str:
    context = reply_text.strip() if reply_text else "(нет)"
    return f"Контекст ответа: {context}\nСообщение: {text.strip()}\n/no_think"
