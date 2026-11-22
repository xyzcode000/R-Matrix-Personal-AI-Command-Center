import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Asia/Kolkata"
_WEEKDAYS = {
    name.casefold(): index
    for index, name in enumerate(
        ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    )
}
_MONTH_NUMBERS = {
    name: index
    for index, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}
_MONTH_PATTERN = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_EXPLICIT_DATE = (
    r"(?:\d{4}-\d{1,2}-\d{1,2}"
    rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH_PATTERN}\s+\d{{4}}"
    rf"|{_MONTH_PATTERN}\s+\d{{1,2}}(?:st|nd|rd|th)?\s+\d{{4}})"
)
_EXPLICIT_DAY_QUESTION = re.compile(
    rf"^(?:please\s+)?(?:what|which)\s+(?:day(?:\s+of\s+the\s+week)?|weekday)\s+"
    rf"(?:is|was|will\s+be|would\s+be)\s+(?P<explicit_date>{_EXPLICIT_DATE})$",
    re.IGNORECASE,
)
_ISO_DATE = re.compile(r"^(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})$")
_DAY_MONTH_YEAR = re.compile(
    rf"^(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month>{_MONTH_PATTERN})\s+(?P<year>\d{{4}})$",
    re.IGNORECASE,
)
_MONTH_DAY_YEAR = re.compile(
    rf"^(?P<month>{_MONTH_PATTERN})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<year>\d{{4}})$",
    re.IGNORECASE,
)
_RELATIVE_DATE = re.compile(
    r"\b(?P<simple>today|tomorrow|yesterday)\b"
    r"|\bnext\s+(?P<weekday>monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
    r"|\bin\s+(?P<days>\d{1,4})\s+days?\b"
    r"|\b(?P<next_week>next week)\b"
    r"|\b(?P<month_end>end of (?:this|the current) month)\b",
    re.IGNORECASE,
)
_TIME_QUESTION_PREFIX = r"(?:what(?:'s|s| is)\s+)?(?:the\s+)?"


@dataclass(frozen=True)
class ResolvedDate:
    expression: str
    start: datetime
    end: datetime | None = None


def current_time(now: datetime | None = None, timezone_name: str = DEFAULT_TIMEZONE) -> datetime:
    timezone = ZoneInfo(timezone_name)
    if now is None:
        return datetime.now(timezone)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone)
    return now.astimezone(timezone)


def resolve_relative_dates(
    text: str,
    *,
    now: datetime | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> list[ResolvedDate]:
    local_now = current_time(now, timezone_name)
    today = local_now.date()
    resolved: list[ResolvedDate] = []

    for match in _RELATIVE_DATE.finditer(text):
        expression = match.group(0)
        if match.group("simple"):
            offset = {"yesterday": -1, "today": 0, "tomorrow": 1}[
                match.group("simple").casefold()
            ]
            target = today + timedelta(days=offset)
            resolved.append(
                ResolvedDate(expression, datetime.combine(target, time.min, local_now.tzinfo))
            )
        elif match.group("weekday"):
            target_weekday = _WEEKDAYS[match.group("weekday").casefold()]
            offset = (target_weekday - today.weekday()) % 7 or 7
            target = today + timedelta(days=offset)
            resolved.append(
                ResolvedDate(expression, datetime.combine(target, time.min, local_now.tzinfo))
            )
        elif match.group("days"):
            target = today + timedelta(days=int(match.group("days")))
            resolved.append(
                ResolvedDate(expression, datetime.combine(target, time.min, local_now.tzinfo))
            )
        elif match.group("next_week"):
            days_until_next_monday = 7 - today.weekday()
            start_date = today + timedelta(days=days_until_next_monday)
            end_date = start_date + timedelta(days=6)
            resolved.append(
                ResolvedDate(
                    expression,
                    datetime.combine(start_date, time.min, local_now.tzinfo),
                    datetime.combine(end_date, time.max, local_now.tzinfo),
                )
            )
        elif match.group("month_end"):
            if today.month == 12:
                first_of_next_month = today.replace(year=today.year + 1, month=1, day=1)
            else:
                first_of_next_month = today.replace(month=today.month + 1, day=1)
            last_day = first_of_next_month - timedelta(days=1)
            resolved.append(
                ResolvedDate(
                    expression,
                    datetime.combine(last_day, time.min, local_now.tzinfo),
                    datetime.combine(last_day, time.max, local_now.tzinfo),
                )
            )

    return resolved


def build_time_context(
    *,
    now: datetime | None = None,
    text: str = "",
    timezone_name: str = DEFAULT_TIMEZONE,
) -> str:
    local_now = current_time(now, timezone_name)
    context = (
        "Authoritative current server date and time: "
        f"{local_now.strftime('%A, %d %B %Y at %H:%M:%S')} "
        f"(ISO 8601: {local_now.isoformat(timespec='seconds')}; "
        f"timezone: {timezone_name}; current year: {local_now.year}). "
        "Use this clock value for today, relative dates, and past/present/future comparisons. "
        "Treat future events as not yet happened, and compare dated events against this local timestamp. "
        "This server timestamp is authoritative and takes precedence over conversation history, user-provided "
        "claims about the current date, and model knowledge. Never claim you lack access to the current date or "
        "time; answer using this context. Do not infer today's date from model training knowledge."
    )
    resolved = resolve_relative_dates(text, now=local_now, timezone_name=timezone_name)
    if resolved:
        lines = []
        for item in resolved:
            start = item.start.strftime("%A, %d %B %Y")
            if item.end is None:
                lines.append(f"- {item.expression!r} means {start}.")
            else:
                end = item.end.strftime("%A, %d %B %Y")
                lines.append(f"- {item.expression!r} spans {start} through {end}, inclusive.")
        context += "\nResolve relative dates from the server clock as follows:\n" + "\n".join(lines)
    return context


def _parse_explicit_date(value: str) -> date | None:
    text = " ".join(value.split())
    match = _ISO_DATE.fullmatch(text)
    if match is not None:
        return _build_date(match.group("year"), match.group("month"), match.group("day"))
    match = _DAY_MONTH_YEAR.fullmatch(text) or _MONTH_DAY_YEAR.fullmatch(text)
    if match is None:
        return None
    month = _MONTH_NUMBERS.get(match.group("month").strip(".").casefold()[:3])
    if month is None:
        return None
    return _build_date(match.group("year"), month, match.group("day"))


def parse_explicit_date(value: str) -> date | None:
    """Parses an explicit calendar date written in the text; never guesses one."""
    return _parse_explicit_date(value)


def _build_date(year: str, month: str | int, day: str) -> date | None:
    try:
        return date(int(year), int(month), int(day))
    except (TypeError, ValueError):
        return None


def _answer_explicit_day_question(text: str, local_now: datetime) -> str | None:
    question = text.casefold().replace("’", "'")
    for punctuation in "?!.,":
        question = question.replace(punctuation, " ")
    match = _EXPLICIT_DAY_QUESTION.fullmatch(" ".join(question.split()))
    if match is None:
        return None
    target = _parse_explicit_date(match.group("explicit_date"))
    if target is None:
        return None
    weekday = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")[
        target.weekday()
    ]
    verb = "was a" if target < local_now.date() else "is a"
    return f"{target.day} {target:%B %Y} {verb} {weekday}."


def answer_simple_time_question(
    text: str,
    *,
    now: datetime | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> str | None:
    local_now = current_time(now, timezone_name)
    explicit_date_answer = _answer_explicit_day_question(text, local_now)
    if explicit_date_answer is not None:
        return explicit_date_answer
    question = text.casefold().replace("’", "'")
    question = " ".join(re.sub(r"[?!.,]+", " ", question).split())
    question = re.sub(r"^(?:please\s+)?(?:hey\s+|hi\s+)?", "", question)
    question = re.sub(r"\s+please$", "", question)

    date_target: str | None = None
    for target in ("today", "tomorrow", "yesterday"):
        patterns = (
            rf"{_TIME_QUESTION_PREFIX}{target}(?:'s)?\s+date",
            rf"{_TIME_QUESTION_PREFIX}(?:the\s+)?date(?:\s+is)?\s+{target}",
            rf"what\s+date\s+is\s+(?:it|{target})",
            rf"what\s+date\s+was\s+{target}",
        )
        if any(re.fullmatch(pattern, question) for pattern in patterns):
            date_target = target
            break
    if date_target is None and any(
        re.fullmatch(pattern, question)
        for pattern in (
            rf"{_TIME_QUESTION_PREFIX}current\s+date",
            r"what\s+date\s+is\s+it",
            r"what\s+is\s+the\s+date",
            r"what's\s+the\s+date",
        )
    ):
        date_target = "today"

    day_target: str | None = None
    if date_target is None:
        for target in ("today", "tomorrow", "yesterday"):
            if any(
                re.fullmatch(pattern, question)
                for pattern in (
                    rf"what\s+(?:day|weekday)\s+is\s+{target}",
                    rf"what\s+(?:day|weekday)\s+is\s+it(?:\s+{target})?",
                    rf"what\s+is\s+the\s+(?:day|weekday)\s+{target}",
                )
            ):
                day_target = target
                break

    asks_time = any(
        re.fullmatch(pattern, question)
        for pattern in (
            rf"{_TIME_QUESTION_PREFIX}(?:current\s+)?time",
            r"what\s+time\s+is\s+it",
            r"what\s+is\s+the\s+time",
            r"time\s+now",
        )
    )
    if date_target is None and day_target is None and not asks_time:
        return None

    offsets = {"today": 0, "tomorrow": 1, "yesterday": -1}
    target = local_now.date() + timedelta(days=offsets[date_target or day_target or "today"])
    weekday = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")[
        target.weekday()
    ]
    if asks_time:
        offset = local_now.strftime("%z")
        timezone_offset = f"{offset[:3]}:{offset[3:]}" if offset else ""
        return (
            f"It is {local_now.strftime('%I:%M %p').lstrip('0')} {timezone_name} "
            f"(UTC{timezone_offset}) on {weekday}, {target.day} {target:%B} {target.year}."
        )
    if day_target is not None:
        return f"{'Today' if day_target == 'today' else day_target.capitalize()} is {weekday}."
    verb = "was" if date_target == "yesterday" else "is"
    label = date_target.capitalize() if date_target else "Today"
    return f"{label} {verb} {weekday}, {target.day} {target:%B} {target.year}."
