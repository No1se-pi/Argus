import json
import re
from dataclasses import dataclass

from app.services.llm.ollama_client import OllamaClient
from app.services.llm.prompts import SYSTEM_PROMPT, classification_prompt

SENTIMENTS = {"positive", "neutral", "negative"}
INTENTS = {"none", "joke_or_quote", "vague", "intent", "planned", "imminent"}
CATEGORIES = {
    "violence", "threat", "fight", "weapon", "prohibited_item", "alcohol", "drugs",
    "vandalism", "theft", "harassment", "bullying", "doxxing", "personal_data",
    "event_safety", "self_harm", "suspicious_link", "coordinated_action", "other",
}


@dataclass(frozen=True)
class Classification:
    risk: bool
    severity: int
    confidence: float
    categories: list[str]
    sentiment: str
    topic: str
    reason: str
    intent_level: str


class MessageClassifier:
    def __init__(self, client: OllamaClient) -> None:
        self.client = client

    async def classify(self, text: str, reply_text: str | None = None) -> Classification:
        raw = await self.client.generate(
            system=SYSTEM_PROMPT, prompt=classification_prompt(text, reply_text)
        )
        return self.parse(raw)

    @staticmethod
    def parse(raw: str) -> Classification:
        candidate = raw.strip()
        if candidate.startswith("```"):
            candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.I)
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("Invalid LLM JSON") from exc
        required = {"risk", "severity", "confidence", "categories", "sentiment", "topic", "reason", "intent_level"}
        if not isinstance(data, dict) or not required.issubset(data):
            raise ValueError("Incomplete LLM classification")
        if type(data["risk"]) is not bool:
            raise ValueError("risk must be boolean")
        severity = int(data["severity"])
        confidence = float(data["confidence"])
        categories = data["categories"]
        if severity not in range(4) or not 0 <= confidence <= 1:
            raise ValueError("Classification values out of range")
        if not isinstance(categories, list) or any(item not in CATEGORIES for item in categories):
            raise ValueError("Unknown risk category")
        sentiment = str(data["sentiment"])
        intent = str(data["intent_level"])
        if sentiment not in SENTIMENTS or intent not in INTENTS:
            raise ValueError("Unknown sentiment or intent")
        risk = data["risk"]
        # Small local models occasionally emit a self-contradictory boolean/severity while
        # correctly identifying concrete intent and risk categories. Normalize only that
        # semantic contradiction; keywords never make the decision here.
        dangerous = set(categories) - {"other", "personal_data", "suspicious_link"}
        if not risk and dangerous and intent in {"planned", "imminent"} and confidence >= 0.5:
            risk = True
            severity = max(severity, 2 if intent == "planned" else 3)
        if not risk:
            severity = 0
        return Classification(risk, severity, confidence, categories, sentiment,
                              str(data["topic"])[:100], str(data["reason"])[:1000], intent)
