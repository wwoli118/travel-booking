# 여행 예약 자동화 (회사 출장용)

원칙은 [CLAUDE.md](CLAUDE.md)를 따릅니다. 결제·주문 생성은 하지 않으며, 현재는 **Duffel 테스트(샌드박스) 키로 편도 항공권 검색과 예약 전 확인 요약**까지 합니다.

## 설치

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 그리고 DUFFEL_API_KEY=duffel_test_... 입력
```

`duffel_test_`로 시작하지 않는 키는 API를 호출하기 전에 거부됩니다.

## 항공권 검색 (편도, 가격순)

```bash
python -m src.flights --from ICN --to MNL --date 2026-12-20 --adults 2
```

| 옵션 | 설명 |
|---|---|
| `--from`, `--to` | 공항 IATA 코드 (3글자) |
| `--date` | 출발일 `YYYY-MM-DD` (오늘 이후) |
| `--adults` | 성인 인원 1~9 (기본 1) |
| `--cabin` | `economy`(기본) / `premium_economy` / `business` / `first` |
| `--limit` | 화면 표시 건수 (기본 20, CSV에는 전체 저장) |

결과: 항공사, 편명, 출발/도착 현지 시각, 경유 수, 총액+통화를 가격순으로 출력하고
`data/flights_<출발>-<도착>_<출발일>_<조회시각>.csv`에 저장합니다 (엑셀 호환 UTF-8 BOM).

- 같은 가격이면 출발 시각이 이른 순으로 정렬합니다.
- 결과에 통화가 섞여 있으면 환율 없이 비교할 수 없으므로 정렬하지 않고 오류를 냅니다.
- 해석할 수 없는 운임은 버리지 않고 제외 사유를 경고로 출력합니다.
- 샌드박스 결과는 실제 운항·가격이 아닌 테스트 데이터입니다.

## 예약 전 확인 요약

검색 결과에서 고른 항공편을 Duffel에 다시 조회해서, 탑승객 정보와 함께 요약을 보여주고 `yes/no` 확인을 받습니다.
**주문 생성과 결제는 하지 않습니다.**

1. 탑승객 파일 작성 (개인정보이므로 `data/` 아래에 두며 git에 올라가지 않습니다)

   ```bash
   cp travelers.example.json data/travelers.json
   ```

   - 이름: 여권 영문 표기 그대로 (A–Z, 공백, 하이픈). 소문자로 써도 대문자로 바뀝니다.
   - 호칭 `mr/ms/mrs/miss/dr`, 성별 `m/f`, 생년월일 `YYYY-MM-DD`
   - 전화번호: 국가번호 포함 (`+821012345678`)
   - `trip.purpose`(출장 목적), `trip.cost_center`(비용 부서)는 선택 항목입니다.
   - 탑승객 수는 검색 인원과 같아야 하고, 출발일 기준 만 18세 이상이어야 합니다.

2. 실행

   ```bash
   python -m src.summary --csv data/flights_ICN-MNL_2026-12-20_<조회시각>.csv --rank 1
   ```

   `--offer off_...`로 운임 ID를 직접 넣을 수도 있습니다. `--travelers`로 다른 탑승객 파일을 지정할 수 있습니다.

요약 항목: 항공편·경유지, 구간과 현지 시각, 좌석 수와 등급, 탑승객 영문 이름, 총액(검색 시 가격과 비교),
출발 전 환불·변경 규정, 수하물, 운임 유효 시각, 출장 목적/비용 부서.

- 가격이 오르거나 통화가 바뀌면 경고를 표시합니다.
- 운임 유효 시간이 지났으면 확인을 받지 않고 중단합니다. 다시 검색하세요.
- `yes`일 때만 `data/drafts/draft_<구간>_<출발일>_<확인시각>.json`에 예약 준비 내용을 저장합니다.
  탑승객은 Duffel 승객 ID와 순서대로 짝지어 저장되어 다음 단계(주문 준비)에서 씁니다.

## 테스트

```bash
python -m pytest -q
```

API를 호출하지 않고 `tests/fixtures/`의 샘플 응답으로 검증합니다.

## 구조

```
src/config.py    .env 로드, 테스트 키 강제
src/models.py    검색 조건·항공편 모델 (pydantic, 금액은 Decimal)
src/duffel.py    Duffel API 공용 호출 (조회 전용, 테스트 키 재확인, 오류 원인 출력)
src/flights.py   항공권 검색 + CLI
src/compare.py   가격순 정렬
src/export.py    검색 결과 CSV 저장
src/offers.py    운임 재조회 (가격·규정·만료·수하물), 검색 CSV에서 선택
src/travelers.py 탑승객 정보 검증 (여권 영문 이름, 나이, 연락처)
src/summary.py   예약 전 확인 요약 + yes/no + 준비 내용 저장
```
