# app/client_time.py

import re
from typing import Optional

from app.utils import normalize


def parse_manual_time_text(text: str) -> Optional[str]:
    s = normalize(text or "").strip()
    if not s:
        return None

    s = s.replace(".", ":")
    s = re.sub(r"\s+", " ", s)

    # 20:15 / 8:15 / 08:15 pm
    m = re.match(r"^(\d{1,2}):(\d{2})(?:\s*(am|pm))?$", s)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2))
        suffix = m.group(3)

        if minute < 0 or minute > 59:
            return None

        if suffix == "am":
            if hour == 12:
                hour = 0
            elif hour < 1 or hour > 12:
                return None
        elif suffix == "pm":
            if hour == 12:
                hour = 12
            elif 1 <= hour <= 11:
                hour += 12
            else:
                return None
        else:
            if hour < 0 or hour > 23:
                return None

        return f"{hour:02d}:{minute:02d}"

    # 2015
    m = re.match(r"^(\d{2})(\d{2})$", s)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
        return None

    # 8 pm / 8am / 20h / 8 h / 20
    m = re.match(r"^(\d{1,2})(?:\s*(am|pm|h))?$", s)
    if m:
        hour = int(m.group(1))
        suffix = m.group(2)

        if suffix == "am":
            if hour == 12:
                hour = 0
            elif not (1 <= hour <= 12):
                return None
        elif suffix == "pm":
            if hour == 12:
                hour = 12
            elif 1 <= hour <= 11:
                hour += 12
            else:
                return None
        else:
            if not (0 <= hour <= 23):
                return None

        return f"{hour:02d}:00"

    return None
