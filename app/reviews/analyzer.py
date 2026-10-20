import asyncio
import json
import logging
import os
import time
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone
    UTC = timezone.utc  # noqa: UP017

from app.config import Settings
from app.storage.models import ReviewAiAnalysis
from app.storage.repositories import ReviewRepository

logger = logging.getLogger(__name__)

SYSTEM_INSTRUCTION = (
    "Ты — профессиональный ИИ-аналитик отзывов клиентов для сети учебных отделений "
    "(школ/курсов/центров).\n"
    "Твоя задача — объективно проанализировать смысл ВСЕГО текста отзыва целиком, "
    "а не только первые строки.\n\n"
    "Особенно тщательно выявляй:\n"
    "1. Скрытый негатив (has_hidden_negative):\n"
    "   - Ставь has_hidden_negative=true ТОЛЬКО когда под видом похвалы, вежливости или высокой\n"
    "     оценки (например, 5 звёзд) замаскирована серьёзная жалоба, возмущение, обман или\n"
    "     конфликт во второй половине текста.\n"
    "   - НЕ путай скрытый негатив с обычным смешанным отзывом! Если клиент спокойно и\n"
    "     конструктивно перечисляет плюсы и небольшие минусы/пожелания (например, «хорошие\n"
    "     кураторы, но в аудитории душно»), это обычный смешанный отзыв (sentiment=\"mixed\",\n"
    "     criticism_found=true, has_hidden_negative=false, severity=\"low\" или \"medium\").\n"
    "2. Сарказм, насмешки и иронию («Спасибо за испорченный праздник»).\n"
    "3. Противоречие оценки и текста (stars_text_conflict):\n"
    "   - Оценка 5 звёзд, но в тексте содержатся жалобы или критика;\n"
    "   - Оценка 1-2 звезды, но текст исключительно положительный.\n"
    "4. Конструктивную критику и замечания (грязь, духота, задержки, проблемы с расписанием,\n"
    "   парковкой, оборудованием).\n"
    "5. Серьёзные обвинения и конфликты (хамство сотрудников, обман, вымогательство,\n"
    "   угрозы обращения в администрацию, департамент образования, полицию, суд,\n"
    "   Роспотребнадзор).\n\n"
    "Важные правила:\n"
    "- НЕ считай 5 звёзд автоматически позитивным отзывом.\n"
    "- НЕ считай 1 звезду автоматически негативным текстом.\n"
    "- Оценка (звёзды) и текст анализируются отдельно и сравниваются между собой.\n"
    "- summary должна быть объективной краткой выжимкой на русском языке в 1-2 предложениях.\n"
)


class GeminiAnalysisResult(BaseModel):
    summary: str = Field(
        description="Краткая объективная выжимка сути отзыва в 1-2 предложениях на русском языке."
    )
    sentiment: Literal["positive", "mixed", "negative"] = Field(
        description=(
            "Общая тональность отзыва: positive (исключительно положительный), "
            "mixed (смешанный/с замечаниями), negative (негативный/жалоба)."
        )
    )
    criticism_found: bool = Field(
        description=(
            "true, если в отзыве есть хотя бы минимальная критика, претензии, "
            "замечания или жалобы; иначе false."
        )
    )
    has_hidden_negative: bool = Field(
        description=(
            "true, если выявлен скрытый негатив, сарказм, ирония или хвалебное начало со "
            "скрытой/явной критикой далее; иначе false."
        )
    )
    stars_text_conflict: bool = Field(
        description=(
            "true, если выставленная оценка в звёздах явно не соответствует тональности текста "
            "(например, 5 звезд при критике или 1 звезда при восторге); иначе false."
        )
    )
    severity: Literal["none", "low", "medium", "high"] = Field(
        description=(
            "Серьёзность проблемы: none (нет замечаний), low (мелкие пожелания), "
            "medium (ощутимые неудобства), high (грубые нарушения, хамство, скандал, "
            "угрозы надзорных органов)."
        )
    )
    requires_attention: bool = Field(
        description=(
            "true, если отзыв требует реакции руководства или исправления ситуации; "
            "иначе false."
        )
    )


def compute_deterministic_verdict(
    result: GeminiAnalysisResult | dict[str, Any],
) -> str:
    """
    Computes a deterministic traffic light verdict (RED, YELLOW, GREEN, UNKNOWN)
    strictly in Python code based on structured facts.
    """
    if isinstance(result, dict):
        try:
            res = GeminiAnalysisResult.model_validate(result)
        except ValidationError:
            return "UNKNOWN"
    else:
        res = result

    # 🔴 RED:
    # - severity == high;
    # - или has_hidden_negative == true;
    # - или stars_text_conflict == true вместе с существенной критикой (criticism_found);
    # - или requires_attention == true при серьёзной жалобе (severity == "high").
    if (
        res.severity == "high"
        or res.has_hidden_negative
        or (res.stars_text_conflict and res.criticism_found)
        or (res.requires_attention and res.severity == "high")
    ):
        return "RED"

    # 🟡 YELLOW:
    # - criticism_found == true;
    # - mixed sentiment;
    # - severity in ("low", "medium");
    # - stars_text_conflict (anomaly without high severity);
    # - есть замечания, но нет критической проблемы.
    if (
        res.criticism_found
        or res.sentiment == "mixed"
        or res.severity in ("low", "medium")
        or res.stars_text_conflict
    ):
        return "YELLOW"

    # 🟢 GREEN:
    # - criticism=false;
    # - hidden_negative=false;
    # - stars_text_conflict=false;
    # - sentiment positive;
    # - severity none.
    if (
        not res.criticism_found
        and not res.has_hidden_negative
        and not res.stars_text_conflict
        and res.sentiment == "positive"
        and res.severity == "none"
    ):
        return "GREEN"

    return "UNKNOWN"


class GeminiRateLimiter:
    """
    Token-bucket / timestamp throttle ensuring that API calls never exceed max_rpm.
    Default max_rpm=10 guarantees >= 6.0 seconds between consecutive calls.
    """

    def __init__(self, max_rpm: int = 10) -> None:
        self.max_rpm = max(1, max_rpm)
        self.min_interval = 60.0 / self.max_rpm
        self._last_call_time: float = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            elapsed = now - self._last_call_time
            wait_time = self.min_interval - elapsed
            if wait_time > 0:
                await asyncio.sleep(wait_time)
            self._last_call_time = loop.time()


class GeminiQuotaPausedError(Exception):
    """Raised when daily Gemini API quota is exhausted (RESOURCE_EXHAUSTED / 429 quota)."""
    pass


class GeminiReviewClient:
    """
    Safe wrapper around Google GenAI SDK.
    Guarantees zero leakage of API keys in logs and exception messages.
    """

    def __init__(
        self,
        *,
        api_key: str | Any | None,
        model: str = "gemini-3.5-flash-lite",
        timeout_seconds: float = 35.0,
        max_rpm: int = 10,
        max_retries: int = 3,
    ) -> None:
        if hasattr(api_key, "get_secret_value"):
            self._api_key = api_key.get_secret_value()
        else:
            self._api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(1, max_retries)
        self.rate_limiter = GeminiRateLimiter(max_rpm=max_rpm)
        self._client: Any = None
        self._client_init_failed = False

    def _get_client(self) -> Any:
        if self._client is None and not self._client_init_failed:
            if not self._api_key:
                logger.warning("GeminiReviewClient initialized without API key")
                self._client_init_failed = True
                return None
            try:
                from google import genai
                from google.genai import types

                ambient_google_key = os.environ.pop("GOOGLE_API_KEY", None)
                try:
                    http_opts = types.HttpOptions(timeout=int(self.timeout_seconds * 1000))
                    self._client = genai.Client(api_key=self._api_key, http_options=http_opts)
                finally:
                    if ambient_google_key is not None:
                        os.environ["GOOGLE_API_KEY"] = ambient_google_key
            except Exception as exc:
                self._client_init_failed = True
                logger.error("Failed to initialize Google GenAI Client: %s", type(exc).__name__)
        return self._client

    async def _reset_client_if_needed(self) -> None:
        if (
            self._client is not None
            and not hasattr(self._client, "_mock_return_value")
            and not hasattr(self._client, "mock_calls")
        ):
            client = self._client
            self._client = None
            try:
                if hasattr(client, "_api_client") and hasattr(client._api_client, "aclose"):
                    await client._api_client.aclose()
            except Exception:
                pass


    async def analyze_review(
        self,
        *,
        rating: int,
        text: str,
    ) -> GeminiAnalysisResult:
        """
        Sends the review to Gemini API with structured JSON output schema.
        Handles bounded retries with exponential backoff for 429/5xx and network errors.
        """
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=GeminiAnalysisResult,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        prompt = (
            f"Оценка пользователя: {rating} из 5 звёзд.\n"
            f"Текст отзыва:\n{text if text.strip() else '(без текста)'}"
        )

        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            await self.rate_limiter.acquire()
            client = self._get_client()
            if client is None:
                raise RuntimeError(
                    "Gemini client is not available (missing API key or initialization failed)"
                )
            try:
                coro = client.aio.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=config,
                )
                response = await asyncio.wait_for(coro, timeout=self.timeout_seconds)

                if not response.text:
                    raise ValueError("Gemini returned empty response text")

                data = json.loads(response.text)
                return GeminiAnalysisResult.model_validate(data)

            except (TimeoutError, asyncio.TimeoutError):
                await self._reset_client_if_needed()
                last_error = TimeoutError(
                    f"Gemini API timeout after {self.timeout_seconds}s "
                    f"(attempt {attempt}/{self.max_retries})"
                )
                logger.warning(
                    "Gemini API call timed out (attempt %d/%d)", attempt, self.max_retries
                )
            except Exception as exc:
                exc_type = type(exc).__name__
                exc_str = str(exc).lower()

                # Check for quota exhaustion vs temporary rate limit
                if "resource_exhausted" in exc_str or "quota exceeded" in exc_str:
                    logger.error("Gemini API daily quota exhausted: %s", exc_type)
                    raise GeminiQuotaPausedError("Daily quota exhausted") from exc

                # Check if JSON validation error (no retry needed)
                if isinstance(exc, (json.JSONDecodeError, ValidationError)):
                    logger.warning("Gemini returned invalid structured output: %s", exc_type)
                    raise

                await self._reset_client_if_needed()
                last_error = exc
                logger.warning(
                    "Gemini API call failed with %s (attempt %d/%d)",
                    exc_type,
                    attempt,
                    self.max_retries,
                )

            if attempt < self.max_retries:
                backoff_seconds = min(2.0 ** attempt, 10.0)
                await asyncio.sleep(backoff_seconds)

        if last_error is not None:
            raise last_error
        raise RuntimeError("Gemini API call failed after retries")


class GeminiReviewWorker:
    """
    Lightweight background worker processing pending reviews sequentially
    via an internal asyncio.Queue at a safe rate limit.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        repository: ReviewRepository,
        alerts: Any,
        client: GeminiReviewClient | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.alerts = alerts
        self._client = client
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._stopped = False
        self.quota_cooldown_seconds: float = getattr(
            settings, "gemini_quota_cooldown_seconds", 3600.0
        )
        self._quota_paused: bool = False
        self._quota_paused_at: float | None = None

    def is_quota_paused(self) -> bool:
        if not self._quota_paused:
            return False
        if self._quota_paused_at is not None:
            try:
                loop = asyncio.get_running_loop()
                now = loop.time()
            except RuntimeError:
                now = time.time()
            if now - self._quota_paused_at >= self.quota_cooldown_seconds:
                logger.info("Gemini quota pause cooldown expired, re-enabling requests")
                self._quota_paused = False
                self._quota_paused_at = None
                return False
        return True

    def pause_quota(self, cooldown_seconds: float | None = None) -> None:
        self._quota_paused = True
        try:
            loop = asyncio.get_running_loop()
            self._quota_paused_at = loop.time()
        except RuntimeError:
            self._quota_paused_at = time.time()
        if cooldown_seconds is not None:
            self.quota_cooldown_seconds = cooldown_seconds
        logger.warning(
            "Gemini quota paused for %s seconds", self.quota_cooldown_seconds
        )

    def get_client(self) -> GeminiReviewClient:
        if self._client is None:
            api_key = (
                self.settings.gemini_api_key.get_secret_value()
                if self.settings.gemini_api_key
                else None
            )
            self._client = GeminiReviewClient(
                api_key=api_key,
                model=self.settings.gemini_model,
                timeout_seconds=self.settings.gemini_timeout_seconds,
                max_rpm=self.settings.gemini_max_rpm,
                max_retries=self.settings.gemini_max_retries,
            )
        return self._client

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopped = False
        self._quota_paused = False
        self._quota_paused_at = None
        # Recover pending analyses from SQLite on startup/restart
        pending_reviews = await self.repository.get_pending_ai_reviews(limit=100)
        for rev in pending_reviews:
            if rev.id is not None:
                self.queue.put_nowait(rev.id)
        if pending_reviews:
            logger.info(
                "Enqueued %d pending reviews for AI analysis on worker startup",
                len(pending_reviews),
            )

        self._task = asyncio.create_task(self._worker_loop(), name="gemini_review_worker")

    async def stop(self) -> None:
        self._stopped = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    def enqueue(self, review_id: int) -> None:
        if not self._stopped:
            self.queue.put_nowait(review_id)

    async def _worker_loop(self) -> None:
        while not self._stopped:
            try:
                review_id = await self.queue.get()
            except asyncio.CancelledError:
                break

            try:
                await self.process_review(review_id)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(
                    "Unexpected error in Gemini worker loop for review %s: %s",
                    review_id,
                    exc,
                )
            finally:
                self.queue.task_done()

    async def process_review(self, review_id: int) -> ReviewAiAnalysis | None:
        # 1. Fetch review from SQLite
        review = await self.repository.get_review_by_id(review_id)
        if review is None:
            logger.warning("Worker could not find review id %s in SQLite", review_id)
            return None

        # 2. Check if already successfully analyzed
        existing = await self.repository.get_ai_analysis(review_id)
        if existing and existing.status == "SUCCESS":
            logger.info("Review %s already analyzed successfully, skipping", review_id)
            return existing

        # 3. Check if AI feature is enabled
        runtime_ai_val = await self.repository.database.require_connection().execute(
            "SELECT value FROM runtime_settings WHERE key = 'enable_gemini_review_analysis' LIMIT 1"
        )
        async with runtime_ai_val as cur:
            row = await cur.fetchone()
        runtime_enabled = (
            row["value"].strip().lower() in {"1", "true", "yes", "on"} if row else None
        )
        is_enabled = (
            runtime_enabled
            if runtime_enabled is not None
            else self.settings.enable_gemini_review_analysis
        )

        if not is_enabled:
            # AI is disabled -> mark SKIPPED, verdict UNKNOWN
            analysis = ReviewAiAnalysis(
                id=existing.id if existing else None,
                review_id=review_id,
                status="SKIPPED",
                verdict="UNKNOWN",
                summary=None,
                sentiment=None,
                severity=None,
                criticism_found=False,
                has_hidden_negative=False,
                stars_text_conflict=False,
                requires_attention=False,
                model=self.settings.gemini_model,
                error_message="AI analysis disabled",
                retry_count=0,
            )
            saved = await self.repository.save_ai_analysis(analysis)
            if self.alerts:
                await self._safe_deliver(review_id, saved)
            return saved

        # 4. Check if quota paused
        if self.is_quota_paused():
            analysis = ReviewAiAnalysis(
                id=existing.id if existing else None,
                review_id=review_id,
                status="QUOTA_PAUSED",
                verdict="UNKNOWN",
                model=self.settings.gemini_model,
                error_message="Gemini quota paused",
                retry_count=existing.retry_count if existing else 0,
            )
            saved = await self.repository.save_ai_analysis(analysis)
            if self.alerts:
                await self._safe_deliver(review_id, saved)
            return saved

        # 5. Execute Gemini call
        client = self.get_client()
        retry_count = existing.retry_count if existing else 0

        try:
            res = await client.analyze_review(rating=review.rating, text=review.text)
            verdict = compute_deterministic_verdict(res)
            analysis = ReviewAiAnalysis(
                id=existing.id if existing else None,
                review_id=review_id,
                status="SUCCESS",
                verdict=verdict,
                summary=res.summary,
                sentiment=res.sentiment,
                severity=res.severity,
                criticism_found=res.criticism_found,
                has_hidden_negative=res.has_hidden_negative,
                stars_text_conflict=res.stars_text_conflict,
                requires_attention=res.requires_attention,
                model=client.model,
                error_message=None,
                retry_count=retry_count,
            )
            saved = await self.repository.save_ai_analysis(analysis)
            logger.info(
                "Review %s analyzed by Gemini: verdict=%s sentiment=%s severity=%s",
                review_id,
                verdict,
                res.sentiment,
                res.severity,
            )

        except GeminiQuotaPausedError:
            self.pause_quota()
            analysis = ReviewAiAnalysis(
                id=existing.id if existing else None,
                review_id=review_id,
                status="QUOTA_PAUSED",
                verdict="UNKNOWN",
                model=client.model,
                error_message="Daily quota exhausted",
                retry_count=retry_count + 1,
            )
            saved = await self.repository.save_ai_analysis(analysis)

        except Exception as exc:
            err_type = type(exc).__name__
            analysis = ReviewAiAnalysis(
                id=existing.id if existing else None,
                review_id=review_id,
                status="AI_ERROR",
                verdict="UNKNOWN",
                model=client.model,
                error_message=f"Analysis failed: {err_type}",
                retry_count=retry_count + 1,
            )
            saved = await self.repository.save_ai_analysis(analysis)
            logger.error("Failed to analyze review %s with Gemini: %s", review_id, err_type)

        # Trigger safe delivery to Telegram if not already delivered
        if self.alerts:
            await self._safe_deliver(review_id, saved)

        return saved

    async def _safe_deliver(
        self,
        review_id: int,
        ai_analysis: ReviewAiAnalysis | None = None,
    ) -> bool:
        if not self.alerts:
            return False

        fresh_review = await self.repository.get_review_by_id(review_id)
        if fresh_review is None or fresh_review.is_sent_to_telegram:
            return False

        source = (
            await self.repository.get_source_by_id(fresh_review.source_id)
            if fresh_review.source_id
            else None
        )
        if source is None:
            sources = await self.repository.list_sources()
            for s in sources:
                if (
                    s.branch_name == fresh_review.branch_name
                    and s.platform == fresh_review.platform
                ):
                    source = s
                    break

        if source is None:
            logger.warning(
                "Could not find source for review %s in worker delivery",
                review_id,
            )
            return False

        try:
            return await self.alerts.send_review_alert(
                review=fresh_review,
                source=source,
                repo=self.repository,
                ai_analysis=ai_analysis,
            )
        except Exception as deliv_exc:
            logger.error(
                "Failed to deliver review %s to Telegram from worker: %s",
                review_id,
                deliv_exc,
            )
            return False
