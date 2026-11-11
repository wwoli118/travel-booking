# 호텔 검색 설계 (초안)

> 상태: **H1(검색·비교·CSV), H2(예약 전 확인) 구현 완료.** H3(Booking.com·Agoda)는 파트너 승인 후.
> 조사 기준일: 2026-11-11. API 조건은 바뀔 수 있으니 구현 직전에 공식 문서로 다시 확인합니다 (CLAUDE.md).

## 1. 범위

| 포함 | 제외 |
|---|---|
| 위치·날짜·객실·인원으로 호텔 검색 | **예약 생성** (아래 2장 이유) |
| 1박 가격·총액(통화 포함)으로 비교, CSV 저장 | 결제, 카드 정보 처리 |
| 선택한 요금의 가격·취소 규정 재확인 + 예약 전 요약 + yes 확인 | 예약 사이트 화면 크롤링·자동 조작 |
| 사람이 예약한 뒤 안내 이메일·회계 내역으로 연결 | |

흐름 (사람이 하는 단계는 ★):

```
검색 → 비교(CSV) → 요금 재확인(견적) → 요약 + yes → 준비 내용 저장
  → ★ 사람이 예약·결제 → 예약 정보 파일 작성 → 안내 이메일 초안 / 회계 내역 추가 (이미 구현됨)
```

## 2. API 조사 결과

| API | 지금 쓸 수 있나 | 예약 시 결제 | 판단 |
|---|---|---|---|
| **Duffel Stays** | 테스트 토큰으로 테스트 가능 (항공과 같은 계정·키) | 예약 생성 시 **Duffel 잔액에서 자동 차감** | **1순위.** 견적(quote)까지만 사용 |
| Booking.com Demand API | 아니오. Managed Affiliate Partner 계약 + 담당 매니저 필요, 숙소 검색 API는 파일럿 파트너 한정 | (계약 조건에 따름) | 승인 후 어댑터 추가 |
| Agoda 파트너 API | 아니오. 제휴 파트너 계약, IP 화이트리스트 필요 | (계약 조건에 따름) | 승인 후 어댑터 추가 |
| Amadeus Self-Service | 아니오. 2026-07-17 종료, 엔터프라이즈 계약만 | — | 제외 |

### Duffel Stays 세부 (문서 요약)
- 흐름: `POST /stays/search` → 결과별 요금 조회(`fetch_all_rates`) → `POST /stays/quotes`(가용성·총액 확정) → `POST /stays/bookings`(예약)
- **`/stays/bookings`는 호출 즉시 Duffel 잔액에서 결제**되므로 CLAUDE.md 원칙 1에 따라 **호출하지 않습니다.**
  `src/duffel.py`의 차단 목록에 `/stays/bookings`와 결제 지시(payment instruction) 경로를 추가합니다.
- 검색 조건: 위도·경도 + 반경(km) 또는 숙소 ID 목록, 체크인·체크아웃, 객실 수, 투숙객 목록.
  체크인은 330일 이내, 최대 99박. `free_cancellation_only` 필터 있음.
- 결과: 요금별 총액·통화, `cancellation_timeline`(시점별 환불액·통화), 결과 만료 시각.
- 요금 유형: 선결제, 현장 결제, 보증금(deposit) 등이 있습니다. 카드 보증만 필요한 요금은 Duffel 잔액으로 예약할 수 없습니다.
- **테스트 모드 한계:** 테스트 모드에서는 특정 좌표(헨더슨섬)의 가상 숙소 "Duffel Test Hotel"만 검색됩니다.
  **마닐라 같은 실제 도시의 실제 가격은 테스트 키로 볼 수 없습니다.** 실제 가격 조회는 운영 키가 필요하고, 이는 원칙 4에 따라 별도 승인 사항입니다.

### 구현하면서 확인한 것
- 요청·응답 필드는 Duffel 공식 SDK `@duffel/api` 4.31.0의 Stays 타입 정의로 확인했습니다
  (`POST /stays/search`, `POST /stays/search_results/{id}/actions/fetch_all_rates`, `POST /stays/quotes`, `POST /stays/bookings`).
- **검색 결과에는 숙소별 최저 요금(총액·통화, 현장 추가 요금)만 있습니다.** 취소 규정(`cancellation_timeline`),
  식사(`board_type`), 결제 유형(`payment_type`: pay_now / deposit / guarantee)은 요금 조회(`fetch_all_rates`)에서 나옵니다.
  그래서 3.3의 취소 규정·식사 항목은 H2(예약 전 요약)로 옮겼고, H1은 `--free-cancel` 필터로 무료 취소 요금만 검색합니다.
- 검색 반경 1~20km 제한은 이 도구의 자체 제한입니다 (Duffel 상한은 확인하지 못함).

### 확인하지 못한 것
- duffel.com 문서 페이지는 이 작업 환경에서 열 수 없었습니다 (SDK 타입 정의로 대신 확인).
- Duffel Stays 운영 사용에 별도 승인·활성화가 필요한지 여부.
- Booking.com·Agoda 제휴 프로그램이 "회사 내부 출장 예약 도구"를 대상으로 하는지 (제휴 프로그램은 보통 고객에게 숙소를 판매·연결하는 사업자 대상). 두 회사에 문의가 필요합니다.

## 3. 설계

### 3.1 공급자(provider) 구조
API마다 요청 형식이 달라서, 공통 모델과 공급자별 어댑터로 나눕니다. Booking.com·Agoda는 승인 후 어댑터만 추가하면 됩니다.

```python
class HotelProvider(Protocol):
    name: str
    def search(self, query: HotelSearchQuery) -> list[HotelOffer]: ...
    def recheck(self, offer: HotelOffer) -> HotelQuote: ...   # 가격·가용성 재확인 (결제·예약 없음)

DuffelStaysProvider      # 1단계 구현
BookingDemandProvider    # 승인 후 (지금은 "승인 전" 오류)
AgodaProvider            # 승인 후 (지금은 "승인 전" 오류)
```

### 3.2 검색 조건 `HotelSearchQuery`
| 항목 | 검증 |
|---|---|
| 위치 | 등록 지명(아래) 또는 `--lat --lng`, 반경 1–20km (기본 3km) |
| 체크인·체크아웃 | 오늘 이후, 체크아웃 > 체크인, 최대 99박, 체크인 330일 이내 |
| 객실 수 | 1–9 |
| 객실별 성인 수 | 객실마다 1명 이상 (기본: 1실 1인) |
| 무료 취소만 | 선택 (`--free-cancel`) |

**위치 지정:** Duffel은 좌표로 검색하므로, 자주 가는 출장지를 `src/locations.json`에 등록해 이름으로 씁니다
(예: `manila-makati`, `manila-bgc`, `cebu-it-park`). 등록되지 않은 곳은 `--lat --lng`로 지정합니다.
지명을 좌표로 바꾸는 외부 API는 키·약관 문제가 하나 더 생기므로 쓰지 않습니다.

### 3.3 결과 `HotelOffer`
공급자, 숙소 ID, 숙소명, 주소, 등급, 객실 유형, 식사 포함 여부, **총액·통화**, **1박 평균(총액 ÷ 박수, 통화 포함)**,
결제 방식(선결제 / 현장 결제 / 보증금), **취소 규정 요약**(예: "12/18 23:59까지 무료 취소", "환불 불가",
"12/18 이후 취소 시 2,000 PHP 환불"), 결과 만료 시각.

### 3.4 비교·저장
- 정렬: 총액 오름차순 → 같으면 무료 취소 가능한 요금 우선.
- 통화가 섞이면 항공과 같이 정렬하지 않고 오류 (환율 환산 없음). `compare.py`의 정렬을 항공·호텔 공용으로 일반화합니다.
- 화면 표 + `data/hotels_<위치>_<체크인>_<조회시각>.csv` 저장.

### 3.5 예약 전 요약 (원칙 5)
선택한 요금을 다시 조회(Duffel은 quote)해 가격 변동을 확인하고 아래를 보여준 뒤 `yes`를 받습니다.

```
숙소      Example Hotel Makati (★4) — 주소
일정      2026-12-20(일) ~ 2026-12-23(수), 3박
객실      2실 / Deluxe King, 조식 포함
투숙객    1번 객실: KIM YOUNGHEE / 2번 객실: PARK MINSU   (여권 영문 표기)
총액      15,800.00 PHP (1박 평균 5,266.67 PHP)   (검색 시 15,800.00 PHP, 변동 없음)
결제      선결제 (예약 시 결제)
취소      2026-12-18 23:59 (현지)까지 무료 취소, 이후 환불 불가
※ 이 도구는 예약·결제를 하지 않습니다.
```

투숙객은 `data/travelers.json`을 재사용하고 객실 배정을 받습니다. `yes`면 `data/drafts/hotel_draft_*.json`에 저장하고,
**`hotel_booking.json` 초안(예약번호만 빈칸)**도 같이 만들어 사람이 예약 후 번호만 채우면 안내 이메일·회계 내역으로 바로 이어지게 합니다.

### 3.6 실제 예약 (사람)
- Duffel Stays: Duffel 대시보드에서 해당 숙소·요금으로 사람이 예약·결제.
- Booking.com·Agoda: 승인된 계약 조건에 맞춰 결정 (보통 해당 사이트로 이동해 사람이 예약).

### 3.7 안전장치
- `src/duffel.py` 차단 목록에 `/stays/bookings`, 결제 지시 경로 추가 + 테스트.
- 테스트 키 외에는 거부 (기존과 동일). 운영 키 사용은 별도 승인 후.
- 카드 정보 입력 항목 없음.

## 4. 파일 계획

```
src/hotels.py                  검색 CLI, HotelSearchQuery/HotelOffer, 표·CSV
src/hotel_providers/base.py    HotelProvider 인터페이스, 공통 오류
src/hotel_providers/duffel_stays.py
src/hotel_summary.py           재확인 + 요약 + yes + 준비 내용·hotel_booking.json 초안 저장
src/locations.json             출장지 좌표 목록
src/compare.py                 정렬 일반화 (항공·호텔 공용)
tests/test_hotels*.py, tests/fixtures/duffel_stays_*.json
```

## 5. 구현 순서
1. **H1 검색**: Duffel Stays 검색 + 비교 + CSV (테스트 호텔로 검증)
2. **H2 예약 전 요약**: 요금 재확인(quote) + 요약 + yes + 준비 내용·`hotel_booking.json` 초안
3. **H3 공급자 추가**: Booking.com / Agoda 승인 후

## 6. 결정이 필요한 것
1. Duffel Stays를 1순위로 진행할지 (테스트는 가상 호텔만 가능하다는 한계 포함)
2. 자주 가는 출장지 목록 (예: 마닐라 마카티·BGC, 세부 등) — `locations.json` 초기값
3. 기본 객실 배정: 1인 1실로 둘지
4. Booking.com·Agoda 제휴 문의 여부 (문의 시 "회사 출장용 내부 도구" 대상인지 확인)
5. Duffel Stays의 견적(quote) 생성까지 허용할지 — 결제·예약은 아니지만 공급자에 가용성 확인 요청을 보냅니다

## 출처
- Booking.com Demand API: [Prerequisites](https://developers.booking.com/demand/docs/getting-started/prerequisites), [Demand API 3.1](https://developers.booking.com/demand/docs/open-api/demand-api), [Accommodations 3.2 Beta](https://developers.booking.com/demand/docs/open-api/3.2-beta/demand-api/accommodations)
- Agoda: [Getting Started](https://developer.pps.agoda.com/docs), [JSON Search API](https://developer.agoda.com/demand/docs/json-search-api), [FAQ](https://developer.pps.agoda.com/docs/faq)
- Duffel Stays: [Getting Started with Stays](https://duffel.com/docs/guides/getting-started-with-stays), [Searching for Stays](https://duffel.com/docs/guides/searching-for-stays), [Test Hotels](https://duffel.com/docs/guides/test-hotels), [Bookings API](https://duffel.com/docs/api/v2/bookings), [Search API](https://duffel.com/docs/api/v2/search)
- Amadeus: [Statement regarding Amadeus for Developers portal](https://amadeus.com/en/industry-messaging/statement-regarding-amadeus-for-developers-portal), [PhocusWire](https://www.phocuswire.com/amadeus-shut-down-self-service-apis-portal-developers)
