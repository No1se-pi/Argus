"""
Gemini Review Analyzer Test Harness
CLI utility to evaluate model accuracy and traffic light verdicts on real/synthetic review datasets.
ZERO LEAK PROTOCOL: Never prints or logs the GEMINI_API_KEY.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Ensure UTF-8 console output on Windows
if sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from app.reviews.analyzer import (  # noqa: E402
    GeminiReviewClient,
    compute_deterministic_verdict,
)

SMOKE_TEST_CASES = [
    {
        "id": "Test A (Очевидно позитивный)",
        "rating": 5,
        "text": (
            "Отличная школа! Преподаватели настоящие профессионалы, "
            "ребенок ходит с огромным удовольствием. Заметен прогресс в учебе."
        ),
        "expected_verdict": "GREEN",
        "expected_flags": {
            "criticism_found": False,
            "has_hidden_negative": False,
            "stars_text_conflict": False,
            "sentiment": "positive",
        },
    },
    {
        "id": "Test B (Смешанный)",
        "rating": 4,
        "text": (
            "В целом хороший учебный центр, сильная программа подготовки и внимательные "
            "кураторы. Но иногда бывают накладки с расписанием и в аудиториях душно."
        ),
        "expected_verdict": "YELLOW",
        "expected_flags": {
            "criticism_found": True,
            "has_hidden_negative": False,
        },
    },
    {
        "id": "Test C (Главный кейс: 5★ со скрытым негативом)",
        "rating": 5,
        "text": (
            "Замечательное место, очень красивый холл и вежливый администратор на входе. Но во "
            "второй половине курса начался полный кошмар: преподаватель постоянно опаздывал, "
            "на вопросы не отвечал, нахамил при всей группе, а руководство отказалось возвращать "
            "деньги за пропущенные по их вине занятия! Будем обращаться в департамент образования "
            "и Роспотребнадзор."
        ),
        "expected_verdict": "RED",
        "expected_flags": {
            "hidden_negative": True,
            "criticism_found": True,
            "stars_text_conflict": True,
        },
    },
]


def load_api_key() -> str | None:
    # Check secrets/gemini.env first (local secret file)
    secret_path = PROJECT_ROOT / "secrets" / "gemini.env"
    if secret_path.exists():
        vals = dotenv_values(str(secret_path))
        key = vals.get("GEMINI_API_KEY")
        if key:
            k = key.strip().strip("'").strip('"')
            os.environ["GEMINI_API_KEY"] = k
            os.environ["GOOGLE_API_KEY"] = k
            return k

    # Check OS env
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        os.environ["GOOGLE_API_KEY"] = key
        return key

    # Check .env
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        vals = dotenv_values(str(env_path))
        key = vals.get("GEMINI_API_KEY")
        if key:
            k = key.strip().strip("'").strip('"')
            os.environ["GEMINI_API_KEY"] = k
            os.environ["GOOGLE_API_KEY"] = k
            return k

    return None


async def run_benchmark(
    cases: list[dict[str, Any]],
    model: str = "gemini-3.5-flash-lite",
    max_rpm: int = 10,
) -> bool:
    api_key = load_api_key()
    if not api_key:
        print(
            "ERROR: GEMINI_API_KEY not found in environment or secrets/gemini.env",
            file=sys.stderr,
        )
        return False

    client = GeminiReviewClient(
        api_key=api_key,
        model=model,
        timeout_seconds=20.0,
        max_rpm=max_rpm,
        max_retries=3,
    )

    print("\n=======================================================")
    print(f"Running Gemini Review Quality Harness with model: {model}")
    print(f"Total test cases: {len(cases)}")
    print(f"Rate limiter: <= {max_rpm} RPM")
    print("=======================================================\n")

    passed_count = 0
    total_count = len(cases)

    for idx, case in enumerate(cases, 1):
        case_id = case.get("id", f"Case #{idx}")
        rating = case["rating"]
        text = case["text"]
        expected_verdict = case.get("expected_verdict")

        print(f"[{idx}/{total_count}] Evaluating: {case_id}")
        print(f"  Input Rating: {rating} stars")
        print(f"  Review Text: {text[:80]}...")

        try:
            res = await client.analyze_review(rating=rating, text=text)
            verdict = compute_deterministic_verdict(res)

            print(f"  Summary: {res.summary}")
            print("  Gemini Flags:")
            print(f"    - sentiment: {res.sentiment}")
            print(f"    - criticism_found: {res.criticism_found}")
            print(f"    - has_hidden_negative: {res.has_hidden_negative}")
            print(f"    - stars_text_conflict: {res.stars_text_conflict}")
            print(f"    - severity: {res.severity}")
            print(f"    - requires_attention: {res.requires_attention}")
            print(f"  Computed Verdict: {verdict}")

            # Check verdict match
            is_verdict_ok = (expected_verdict is None) or (verdict == expected_verdict)
            if is_verdict_ok:
                print(f"  Result: PASS (Expected: {expected_verdict}, Got: {verdict})\n")
                passed_count += 1
            else:
                print(f"  Result: FAIL (Expected: {expected_verdict}, Got: {verdict})\n")

        except Exception as exc:
            print(f"  Result: ERROR ({type(exc).__name__}: {exc})\n")

    pct = (passed_count / total_count) * 100
    print("=======================================================")
    print(f"Benchmark Complete: {passed_count}/{total_count} PASSED ({pct:.1f}%)")
    print("=======================================================\n")

    return passed_count == total_count


def main() -> None:
    parser = argparse.ArgumentParser(description="Gemini Review Analysis Test Harness")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Run 3 standard smoke test cases (A, B, C)",
    )
    parser.add_argument("--model", type=str, default="gemini-3.5-flash-lite", help="Model name")
    parser.add_argument("--rpm", type=int, default=10, help="Max requests per minute")

    args = parser.parse_args()

    cases = SMOKE_TEST_CASES
    success = asyncio.run(run_benchmark(cases, model=args.model, max_rpm=args.rpm))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
