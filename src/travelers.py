"""출장자(탑승객) 정보. 예약자(회사)와 탑승객이 다를 수 있으므로 좌석마다 따로 받는다.

data/travelers.json 형식 (개인정보이므로 git에 올리지 않는다):
{
  "trip": {"purpose": "마닐라 거래처 미팅", "cost_center": "영업1팀"},   # 선택
  "travelers": [
    {"title": "mr", "given_name": "GILDONG", "family_name": "HONG", "gender": "m",
     "born_on": "1985-03-02", "email": "hong@example.com", "phone_number": "+821012345678",
     "passport": {"number": "M12345678", "expires_on": "2031-05-01", "issuing_country": "KR"}}   # passport는 선택
  ]
}
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator

PASSPORT_NAME_RE = re.compile(r"^[A-Z]+(?:[ \-][A-Z]+)*$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")
PASSPORT_NO_RE = re.compile(r"^[A-Z0-9]{5,20}$")
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
ADULT_AGE = 18  # Duffel 기준 성인(adult) 나이
PASSPORT_RECOMMENDED_VALIDITY_DAYS = 183  # 많은 국가가 입국 시 6개월 이상 잔여 유효기간을 요구


def normalize_passport_name(v: str) -> str:
    """여권 영문 표기로 정리·검증한다 (대문자, 공백 하나, 하이픈 허용)."""
    v = " ".join(str(v).split()).upper()
    if not PASSPORT_NAME_RE.match(v):
        raise ValueError(f"여권 영문 표기(A–Z, 공백, 하이픈)만 입력하세요: {v!r}")
    return v


class TravelerFileError(Exception):
    """travelers.json을 읽거나 검증하지 못했을 때. 원인을 담는다."""


def age_on(born_on: date, on: date) -> int:
    """on 날짜 기준 만 나이."""
    return on.year - born_on.year - ((on.month, on.day) < (born_on.month, born_on.day))


class Passport(BaseModel):
    """여권 정보 (선택). 항공사가 요구하는 운임이면 필수로 검사한다."""

    number: str
    expires_on: date
    issuing_country: str  # ISO 3166-1 alpha-2 (예: KR)

    @field_validator("number", mode="before")
    @classmethod
    def _number(cls, v: str) -> str:
        v = re.sub(r"\s", "", str(v)).upper()
        if not PASSPORT_NO_RE.match(v):
            raise ValueError(f"여권번호는 영문 대문자·숫자 5~20자여야 합니다: {v!r}")
        return v

    @field_validator("issuing_country", mode="before")
    @classmethod
    def _country(cls, v: str) -> str:
        v = str(v).strip().upper()
        if not COUNTRY_RE.match(v):
            raise ValueError(f"발급국은 2글자 국가 코드로 입력하세요 (예: KR): {v!r}")
        return v

    @property
    def masked_number(self) -> str:
        return self.number[:3] + "*" * (len(self.number) - 3)


class Traveler(BaseModel):
    """탑승객 한 명. 이름은 여권 영문 표기 그대로."""

    title: str
    given_name: str = Field(max_length=50)
    family_name: str = Field(max_length=50)
    gender: str
    born_on: date
    email: str
    phone_number: str
    passport: Passport | None = None

    @field_validator("given_name", "family_name", mode="before")
    @classmethod
    def _passport_name(cls, v: str) -> str:
        return normalize_passport_name(v)

    @field_validator("title", mode="before")
    @classmethod
    def _title(cls, v: str) -> str:
        v = str(v).strip().lower().rstrip(".")
        if v not in {"mr", "ms", "mrs", "miss", "dr"}:
            raise ValueError(f"호칭은 mr, ms, mrs, miss, dr 중 하나여야 합니다: {v!r}")
        return v

    @field_validator("gender", mode="before")
    @classmethod
    def _gender(cls, v: str) -> str:
        v = str(v).strip().lower()
        if v not in {"m", "f"}:
            raise ValueError(f"성별은 여권 표기대로 m 또는 f로 입력하세요: {v!r}")
        return v

    @field_validator("born_on")
    @classmethod
    def _born_in_past(cls, v: date) -> date:
        if v >= date.today():
            raise ValueError(f"생년월일이 오늘 이후입니다: {v.isoformat()}")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip()
        if not EMAIL_RE.match(v):
            raise ValueError(f"이메일 형식이 아닙니다: {v!r}")
        return v

    @field_validator("phone_number", mode="before")
    @classmethod
    def _phone(cls, v: str) -> str:
        v = re.sub(r"[\s\-()]", "", str(v))
        if not E164_RE.match(v):
            raise ValueError(
                f"전화번호는 국가번호 포함 국제 형식으로 입력하세요 (예: +821012345678): {v!r}"
            )
        return v

    @property
    def passport_name(self) -> str:
        """여권 표기 순서: 성 이름 (예: HONG GILDONG)."""
        return f"{self.family_name} {self.given_name}"


class TripInfo(BaseModel):
    """출장 관리용 선택 항목."""

    purpose: str | None = None
    cost_center: str | None = None


class TravelerFile(BaseModel):
    trip: TripInfo = TripInfo()
    travelers: list[Traveler] = Field(min_length=1)


def load_travelers(path: Path) -> TravelerFile:
    if not path.exists():
        raise TravelerFileError(
            f"탑승객 파일이 없습니다: {path}. travelers.example.json을 복사해서 작성하세요."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise TravelerFileError(f"{path} JSON 형식 오류 ({e.lineno}행 {e.colno}열): {e.msg}") from e
    try:
        return TravelerFile.model_validate(raw)
    except ValidationError as e:
        lines = []
        for err in e.errors():
            where = ".".join(str(x) for x in err["loc"])
            lines.append(f"  - {where}: {err['msg'].removeprefix('Value error, ')}")
        raise TravelerFileError(f"{path} 내용 오류:\n" + "\n".join(lines)) from e


def check_travelers_for_flight(
    travelers: list[Traveler], seats: int, departure: date
) -> None:
    """좌석 수와 탑승객 수가 맞는지, 모두 출발일 기준 성인인지 확인한다."""
    if len(travelers) != seats:
        raise TravelerFileError(
            f"좌석 수({seats}석)와 탑승객 수({len(travelers)}명)가 다릅니다. "
            "검색 인원과 travelers.json을 맞춰주세요."
        )
    minors = [t.passport_name for t in travelers if age_on(t.born_on, departure) < ADULT_AGE]
    if minors:
        raise TravelerFileError(
            f"출발일({departure.isoformat()}) 기준 만 {ADULT_AGE}세 미만이 있습니다: {', '.join(minors)}. "
            "현재는 성인 운임만 지원합니다."
        )
    names = [t.passport_name for t in travelers]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise TravelerFileError(f"같은 이름의 탑승객이 중복되어 있습니다: {', '.join(dupes)}")


def check_passports(travelers: list[Traveler], departure: date, required: bool) -> list[str]:
    """여권 검사. 막아야 할 문제는 TravelerFileError, 주의할 점은 경고 문구 목록으로 돌려준다."""
    errors: list[str] = []
    warnings: list[str] = []
    for t in travelers:
        pp = t.passport
        if pp is None:
            if required:
                errors.append(f"{t.passport_name}: 이 운임은 여권 정보가 필요합니다.")
            continue
        if pp.expires_on <= departure:
            errors.append(f"{t.passport_name}: 여권이 출발일({departure.isoformat()}) 전에 만료됩니다 ({pp.expires_on.isoformat()}).")
        elif (pp.expires_on - departure).days < PASSPORT_RECOMMENDED_VALIDITY_DAYS:
            warnings.append(
                f"{t.passport_name}: 여권 잔여 유효기간이 출발일 기준 6개월 미만입니다 ({pp.expires_on.isoformat()}). "
                "목적지 입국 규정을 확인하세요."
            )
    if errors:
        raise TravelerFileError("\n".join(errors))
    return warnings
