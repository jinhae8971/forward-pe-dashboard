"""FOMC 회의 일정 — 연준 공식 페이지 파싱, 실패 시 내장 표로 폴백.

정책금리 변경은 회의 종료 다음 영업일부터 유효(Implementation Note 기준)하므로
'회의 종료일(day 2)'만 보관한다.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import List

import requests

FED_CAL_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

# 연준 공식 발표 기준 회의 종료일 (파싱 실패 시 폴백)
# 갱신 시 FALLBACK_ASOF 도 함께 올린다.
FALLBACK_ASOF = "2026-08-15"
FALLBACK_MEETINGS: List[str] = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
    "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09",
    "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08",
]

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def _strip_tags(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def parse_fed_calendar(html: str) -> List[dt.date]:
    """연준 FOMC 캘린더 HTML에서 회의 종료일을 추출한다."""
    text = _strip_tags(html)
    out: List[dt.date] = []
    for m in re.finditer(r"(\d{4})\s+FOMC\s+Meetings", text):
        year = int(m.group(1))
        seg = text[m.end(): m.end() + 1800]
        # 다음 연도 헤더가 나오면 거기서 자른다
        nxt = re.search(r"\d{4}\s+FOMC\s+Meetings", seg)
        if nxt:
            seg = seg[: nxt.start()]
        found = []
        for mm in re.finditer(
            r"(January|February|March|April|May|June|July|August|September|October|November|December)"
            r"\s+(\d{1,2})\s*[-–/]\s*(\d{1,2})", seg):
            month = _MONTHS[mm.group(1).lower()]
            d1, d2 = int(mm.group(2)), int(mm.group(3))
            # 월말 걸침(예: April 28-29) 처리: d2 < d1 이면 다음 달 1일
            if d2 >= d1:
                try:
                    found.append(dt.date(year, month, d2))
                except ValueError:
                    continue
            else:
                nm = month + 1
                ny = year + (1 if nm > 12 else 0)
                nm = 1 if nm > 12 else nm
                try:
                    found.append(dt.date(ny, nm, d2))
                except ValueError:
                    continue
        # FOMC 정례회의는 연 8회 — 그 이상 잡히면 앞 8건만 신뢰
        out.extend(sorted(set(found))[:8])
    return sorted(set(out))


def load_meetings(timeout: int = 20) -> tuple[List[dt.date], str]:
    """(회의일 리스트, 출처) 반환. 네트워크 실패·검증 실패 시 내장 표 사용."""
    try:
        r = requests.get(
            FED_CAL_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; FedWatchRadar/1.0)"},
            timeout=timeout,
        )
        r.raise_for_status()
        parsed = parse_fed_calendar(r.text)
        # 건전성 검증: 최소 8건 이상 + 올해 회의 포함
        this_year = dt.date.today().year
        if len(parsed) >= 8 and any(d.year == this_year for d in parsed):
            return parsed, "federalreserve.gov"
    except Exception as exc:  # noqa: BLE001
        print(f"[fomc] 공식 캘린더 조회 실패 → 내장 표 사용: {exc}")
    return [dt.date.fromisoformat(s) for s in FALLBACK_MEETINGS], f"fallback({FALLBACK_ASOF})"


def upcoming(meetings: List[dt.date], asof: dt.date, n: int = 4) -> List[dt.date]:
    """asof 이후(당일 포함) 회의 n건."""
    return [d for d in sorted(meetings) if d >= asof][:n]


def has_meeting_in(meetings: List[dt.date], year: int, month: int) -> dt.date | None:
    for d in meetings:
        if d.year == year and d.month == month:
            return d
    return None
