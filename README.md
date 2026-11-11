# 여행 예약 자동화 (회사 출장용)

원칙은 [CLAUDE.md](CLAUDE.md)를 따릅니다. 결제·주문 생성은 하지 않으며, 현재는 **Duffel 테스트(샌드박스) 키로 편도 항공권 검색**만 합니다.

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

## 테스트

```bash
python -m pytest -q
```

API를 호출하지 않고 `tests/fixtures/`의 샘플 응답으로 검증합니다.

## 구조

```
src/config.py    .env 로드, 테스트 키 강제
src/models.py    검색 조건·항공편 모델 (pydantic, 금액은 Decimal)
src/flights.py   Duffel 검색 호출 + CLI
src/compare.py   가격순 정렬
src/export.py    CSV 저장
```
