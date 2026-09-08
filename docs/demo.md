# Hidden View Finder 추천 데모

사용자 조건을 입력하면 **관측 대상 → 서 있을 위치 → 필수 조건 검사 → Top K → 설명과 이미지** 순서로 실행합니다. 화면에는 확정 추천, 필수 조건의 확인이 필요한 후보, 제외 사유를 구분해서 표시합니다.

## 바로 실행하기

저장소 루트에서 Python 3.11 이상으로 실행합니다. 기본 가상 시나리오는 Linux의 시간대 DB가 있는 환경에서 패키지 설치, API 키, 서울 데이터가 필요하지 않습니다. Windows 등 IANA 시간대 DB가 없다면 먼저 `python -m pip install tzdata`를 실행하세요([Python 시간대 자료 설명](https://docs.python.org/3/library/zoneinfo.html#data-sources)). 검증 환경은 Linux입니다.

```bash
python3 scripts/demo/run.py
```

브라우저에서 **http://127.0.0.1:8000**을 엽니다. 포트를 바꾸려면 `--port 8001`을 사용합니다. 종료는 터미널의 `Ctrl+C`입니다.

기본 화면은 **2026-09-08 16:00–19:30, Asia/Seoul**로 고정된 기능 검증 사례입니다. 실행한 날의 날씨나 현재 방문 추천이 아닙니다. 기본 출발점, 장소명, 경로, 장면, 운영 시간, 혼잡도, 날씨와 가시성 상태는 모두 명시적인 가상 입력입니다. 지도 좌표와 경위도가 있더라도 실제 그 위치의 방문 정보로 사용하지 않습니다.

조건을 바꾸면서 다음 동작을 확인할 수 있습니다.

- 자연·강 선호에서 도시·스카이라인 선호로 바꾸면 취향 점수와 순위가 바뀝니다.
- 이동 시간, 보행 거리, 방문 가능 시간, 계단 제한을 줄이면 일부 후보가 제외됩니다.
- 비슷한 위치·방향·관측 대상을 가진 후보는 대표 하나로 묶입니다.
- 근거가 없는 평가 항목은 점수에 50점을 넣지 않고 근거 비중에서 빠집니다.
- 조건을 충족한 장소가 K개보다 적으면 부족한 개수와 이유를 표시합니다.

이 화면은 로컬 데모 서버입니다. 기본 바인딩은 `127.0.0.1`이고 외부 CDN, 지도 타일 또는 브라우저에서 직접 호출하는 외부 API가 없습니다. 가상 모드의 배경은 좌표 관계도이며, 서울 모드에서는 취득한 OSM 선형 지오메트리를 표시합니다. 인증·운영 배포 구성은 포함하지 않습니다.

## 실제 서울 모드

서울 모드는 기존 5 m 가시성 표면과 실제 보행 그래프를 연결합니다. [가시성 엔진 안내](engine.md)에 따라 GDAL과 호환되는 `.venv` 및 서울 중앙 지역 자료를 먼저 준비해야 합니다. GDAL을 다른 배포판으로 중복 설치하지 마세요.

기본적으로 필요한 로컬 파일은 다음과 같습니다.

```text
data/seoul/processed/central-gba-maximum/manifest.json
data/demo/context.json
```

첫 번째 manifest가 있으면 보행 맥락만 추가 취득합니다.

```bash
.venv/bin/python scripts/demo/acquire_context.py
.venv/bin/python scripts/demo/run.py
```

`acquire_context.py`는 남산·명동 약 3 × 3 km 범위의 OSM 경로, 장애물 노드, 공원·숲 폴리곤 및 대상점 근거를 취득하고 캐시합니다. 약 3 MB의 원본을 보존하며 다운로드 합계 한도는 5 MiB입니다. 출처, 시점, 라이선스와 재현 절차는 [추천 데모 데이터 설명](demo-sources.md)에 있습니다. 원본·가공 데이터는 Git에 포함되지 않습니다.

다른 준비 manifest는 실행 옵션으로 지정할 수 있습니다. 현재 추천 공급자의 대상과 검색 반경은 남산용으로 고정되어 있으므로, 다른 지역 manifest를 연결하는 것만으로 새 지역 추천이 구현되지는 않습니다.

```bash
.venv/bin/python scripts/demo/run.py --manifest /absolute/path/to/manifest.json
```

실제 서울 모드의 현재 계산 범위는 다음과 같습니다.

| 항목 | 구현과 의미 |
|---|---|
| 관측 대상 | N서울타워 기둥의 지도 중심 + DTM 위 236.7 m의 **근사 상단 대표점** |
| 가시성 계산 | 반경 1,800 m, 5 m 격자, 기본 눈높이 1.7 m, 곡률 계수 6/7; 요청당 한 번의 native target-centered viewshed |
| 후보 발견 | 실제 OSM `footway`, `path`, `pedestrian`, `steps` 노드를 가시성 결과와 교차; 100 m 공간 구획과 상태별 대표를 만들고 최대 120개 노드 평가 |
| 가시성 상태 | `visible`, `blocked`, `excluded`, `unknown` 유지; 건물 점유 셀은 관측 지점에서 제외되지만 건물은 차폐물로 남음 |
| 관측 위치 | 카드의 좌표는 실제 지도 노드; 가시성은 이를 포함하는 5 m 셀 중심의 결과이며 유효 관측 좌표와 이동 거리를 별도 보고 |
| 경로 | 실제 OSM 노드 연결 그래프에서 최단 보행 거리 계산; 4 km/h 가정의 이동 시간 추정 |
| 접근성 | 알려진 출입 금지·계단·장애물 제한을 처리; 전체 경로의 경사·연석·너비·현재 통제는 미검증 |
| 넓은 풍경 | 남산 숲·능선은 지도 맥락으로 기록하되 전체 장면 가시성은 `unknown`; 가까운 녹지·물을 보인다고 단정하지 않음 |

현재 실자료에서는 **전체 경로와 현재 출입·개방 시간을 확인하지 못했으므로 확정 추천이 0개일 수 있습니다.** 계산상 보이는 지점은 확인 필요 후보로 남습니다. 이것은 오류를 감춘 가짜 Top K 대신 입력 근거의 한계를 보여 주는 의도된 동작입니다. 이동 제한을 늘리는 것만으로 출입 미확인이 해소되지는 않습니다.

실제 서울의 대중교통·자동차 경로 공급자는 연결되지 않았습니다. 해당 수단을 선택하면 이동 정보가 미확인으로 남습니다. 가상 모드의 대중교통·자동차 시간은 기능 시연용 가상 값입니다.

## 시간·날씨·혼잡의 의미

`visit_time`은 **출발할 수 있는 최초 시각**입니다. 예상 도착은 출발 시각에 경로 이동 시간을 더한 값이며, `stay_minutes` 전체가 `available_until`과 개방 시간 안에 들어가는지 확인합니다. `available_until`은 관측 장소에서 떠나는 마감 시각입니다. 출발점으로 돌아오는 여정은 계산하지 않습니다. 시간대는 기본 `Asia/Seoul`이며 API에는 ISO 8601 오프셋을 명시하는 방법을 권장합니다.

서울의 예보를 선택적으로 연결하려면 다음과 같이 실행합니다.

```bash
.venv/bin/python scripts/demo/run.py --online-weather
```

이 옵션은 서버에서 [Open-Meteo Forecast API](https://open-meteo.com/en/docs)를 호출합니다. 강수량, 구름량, 시정, 풍속과 가능한 일출·일몰 자료를 예보로 기록합니다. 현장 관측으로 표현하지 않습니다. 요청 실패, 응답 누락, 지원하는 예보 기간 밖의 방문 시각은 `unknown`으로 남습니다. 기본 실행에서는 외부 예보를 요청하지 않습니다.

서울 요청에 `weather.status`를 `observed`, `forecast`, `estimated` 중 하나로 명시하면 **호출자가 제공한 날씨를 우선 사용**하고 그 요청에서는 외부 예보를 호출하지 않습니다. 원래 값과 시각을 보존하고 `provenance="caller_provided_unverified"`로 표시합니다. 서비스가 그 관측이나 예보를 독립적으로 검증했다는 뜻이 아닙니다. `reference_time`과 해당하는 `valid_from`·`valid_until`을 함께 제공하세요. 기준 시각이 없거나 도착 시각에 유효하지 않은 값은 점수 근거에서 제외합니다. 관측은 유효 구간이 있더라도 관측 후 최대 2시간 이내에서만 사용합니다. 오래된 입력을 최신 예보로 몰래 바꾸지 않습니다. 가상 모드는 명시적인 가상 날씨 fixture를 사용합니다.

현재 공급자는 최대 8개 요청 키를 15분간 캐시하고, 요청당 1 MB 응답 한도와 6초 네트워크 제한을 둡니다. `reference_time`에는 **취득 시각**, `forecast_time`에는 예보 대상 시각을 저장합니다. 모델의 실제 실행 시각은 제공받지 못하면 `model_run_time=null`입니다. 예보는 방문 예상 도착 시각에 맞는 유효 구간 안에서만 평가에 사용합니다. 구름이나 일몰 시각만으로 아름다운 노을, 반사, 조명 점등을 확인했다고 표현하지 않습니다.

혼잡 관측·예보 공급자는 연결되지 않았습니다. 서울 모드의 혼잡은 미확인이며 주거 인구밀도로 대체하지 않습니다. 가상 모드에서만 가상의 방문 인원 수준을 사용합니다.

카드의 태양 방위각·고도는 [NOAA 근사식](https://gml.noaa.gov/grad/solcalc/solareqns.PDF)을 이용한 천문 계산입니다. 실제 지평선, 나무·건물의 그림자, 대기 굴절, 구름과 인공조명은 포함하지 않습니다. 계산상 낮·박명·밤 구분을 현장 조명 상태로 해석하지 않습니다.

## 점수와 근거 비중

필수 조건 검사를 먼저 수행합니다. 출입 금지, 체류 중 폐쇄, 제한 초과, 필수 접근성 실패는 제외하고, 필수 조건이 미검증인 후보는 확정 추천과 분리합니다. 점수는 이 결과를 뒤집지 않습니다.

| 항목 | 기본 가중치 |
|---|---:|
| 선호 풍경과의 일치 | 0.30 |
| 점 가시성과 근거 있는 구도 | 0.25 |
| 방문 시간·날씨 적합성 | 0.20 |
| 이동 편의 | 0.15 |
| 혼잡 선호와의 일치 | 0.10 |

알려진 항목 집합을 A, 항목 점수를 s, 기본 가중치를 w라 하면 다음과 같이 계산합니다.

```text
근거 비중 C = Σ(wᵢ), i ∈ A
임시 총점   = Σ(wᵢ × sᵢ) / C
```

예를 들어 취향·가시성·이동만 평가할 수 있다면 C는 0.70입니다. 빠진 항목에 임의의 중간 점수를 넣지 않습니다. 모든 항목이 미확인이면 총점도 `null`입니다. 이 점수는 **비교용 휴리스틱이며 검증된 만족도 확률이 아닙니다.** 근거 비중 40%의 90점과 근거 비중 100%의 90점은 같은 확실성을 뜻하지 않습니다.

현재 휴리스틱은 확인된 풍경 태그의 선호 일치 비율, 사용자 제한 내 이동 시간, 유효 시점의 날씨·혼잡 자료 등을 사용합니다. `visible`인 단일 대상점만 확인된 경우 가시성 항목은 제한적인 점 증거를 나타내는 60점으로 시작합니다. 타워 전체나 장면 구도가 검증됐다는 뜻이 아닙니다. 항목별 점수와 설명은 응답에 함께 포함합니다.

초기 순위 후 대상 집합·구도 표식이 같고 방향 차이 20° 이내, 거리 120 m 이내인 후보를 묶어 대표를 고릅니다. 이 수치는 초기 데모의 중복 제거 규칙입니다. 전체 서울의 모든 가능한 관측 위치를 검색했거나 경험의 다양성을 최적화했다고 주장하지 않습니다.

## 설명과 예상 이미지

설명은 순위가 결정된 후 생성하며 좌표, 바라볼 방향, 점 가시성의 범위, 이동 시간, 예상 도착과 미확인 사항을 포함합니다. 이미지의 매력은 점수나 재순위에 사용하지 않습니다.

기본 가상 요청에 대해서는 순위 확정 후 생성한 세 PNG가 포함되어 있습니다. **정확히 같은 기본 요청**에만 해당 이미지를 재사용합니다. 날짜, 인원, 선호, 이동 조건 등 요청이 달라지거나 서울 모드를 선택하면 기존 그림을 새 방문의 예상 장면으로 돌려주지 않습니다.

실행 중 이미지 생성 API는 연결되지 않았습니다. 다른 요청에는 `image.status="not_generated"`와 생성 프롬프트를 반환합니다. 프롬프트에는 관측 좌표, 눈높이, 방향, 가정한 시야각, 도착 시각, 대상점 상태, 근거 있는 장면 요소와 미확인 사항을 기록합니다. 실제 장면을 재구성할 기하가 부족하므로 비사실적 분위기 참고 이미지로 제한합니다.

생성된 이미지와 프롬프트에는 항상 다음 표기를 포함합니다.

> AI-generated anticipated view — actual scenery may differ.

## HTTP API

| 요청 | 기능 |
|---|---|
| `GET /api/health` | 서버 상태와 실행 버전 |
| `GET /api/bootstrap` | 기본 요청, 모드 사용 가능 여부, 서울 출발점 |
| `POST /api/recommend` | 검증된 입력으로 전체 추천 흐름 실행 |
| `GET /api/about` | 데이터 설명과 데모 범위 |

정확한 기본 요청으로 실행하는 Python 표준 라이브러리 예제입니다. 서버를 별도 터미널에서 먼저 실행합니다.

```python
import json
from urllib.request import Request, urlopen

base = "http://127.0.0.1:8000"
with urlopen(base + "/api/bootstrap") as response:
    payload = json.load(response)["defaults"]

request = Request(
    base + "/api/recommend",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
)
with urlopen(request) as response:
    result = json.load(response)

for spot in result["recommendations"]:
    print(spot["rank"], spot["name"], spot["score"], spot["evidence_coverage"])
print("확인 필요:", len(result["unverified"]))
print("제외:", len(result["excluded"]), "부족:", result["shortfall"])
```

요청의 핵심 필드는 다음과 같습니다. 아래 예는 기본 이미지 재사용을 위한 완전한 기본 요청과는 다른 사용자 정의 요청입니다.

```json
{
  "mode": "scenario",
  "start": {"lon": 126.978, "lat": 37.571, "name": "가상 출발점"},
  "visit_time": "2026-09-08T16:00:00+09:00",
  "available_until": "2026-09-08T19:30:00+09:00",
  "timezone": "Asia/Seoul",
  "stay_minutes": 30,
  "purpose": "풍경 산책과 사진",
  "group": "성인 2명",
  "activity": "photography",
  "transport_mode": "walking",
  "max_travel_minutes": 45,
  "max_walk_m": 2500,
  "wheelchair": false,
  "stroller": false,
  "no_stairs": false,
  "max_slope_percent": null,
  "preferences": ["nature", "river"],
  "crowd_preference": "quiet",
  "eye_height_m": 1.7,
  "k": 3,
  "weather": {"status": "unknown"}
}
```

좌표는 WGS84 **경도, 위도** 순서입니다. `start.lon`, `start.lat`, `visit_time`은 필수입니다. K는 기본 3이며 1–10을 지원합니다. 생략한 종료 시간은 시작 후 3시간, 체류는 30분으로 처리하고 정규화된 입력을 결과에 돌려줍니다. `purpose`, `group`, `activity`는 구조화해 보존하지만 현재 별도의 언어 모델로 해석하거나 추가 점수로 변환하지 않습니다.

서울 모드는 `mode="seoul"`과 실제 출발 좌표를 사용합니다. 기본 추천 출발 좌표는 지도에 있는 명동역 3번 출구 `lon=126.9854902, lat=37.5607321`입니다. 경로 시작점을 가까운 그래프 노드로 연결하더라도 그 연결 자체의 보행 가능성은 미검증입니다.

응답에는 `request_summary`, `landmarks`, `recommendations`, `unverified`, `excluded`, `duplicates`, `shortfall`, `sources`, `weather`, `visibility_summary`, `pipeline`, `timing_s`가 있습니다. 각 후보에 `hard_constraints`, `criteria`, `criterion_notes`, `evidence_coverage`, 좌표·방향·경로·시각·이미지 상태가 포함됩니다.

잘못된 입력은 HTTP 422, 서울 준비 자료 누락은 HTTP 409입니다. 요청 본문은 JSON 64 KiB 이하이며 비정상 수치·범위와 알 수 없는 모드를 거부합니다. 서버는 한 번에 한 작업을 처리하여 GDAL 데이터셋 핸들을 스레드 간 공유하지 않습니다.

## 추천 데모 테스트

추천 코어와 로컬 API의 테스트에는 pytest와 NumPy가 필요합니다. 데모 실행 자체의 필수 의존성은 아닙니다. 별도 테스트 가상 환경에서 다음과 같이 실행할 수 있습니다.

```bash
python3 -m venv .venv-demo-tests
.venv-demo-tests/bin/python -m pip install pytest==8.4.2 numpy==2.2.6
.venv-demo-tests/bin/python -m pytest tests/test_recommendation.py tests/test_demo_service.py -q
```

[GitHub Actions 구성](../.github/workflows/demo.yml)은 Python 3.11과 3.12에서 같은 두 테스트 파일을 실행합니다. GIS 환경이 없으면 선택적 native 연동 테스트 한 건은 건너뜁니다. 전체 가시성 회귀 검사는 별도로 준비된 GDAL 환경에서 실행해야 합니다.

## 코드 구조와 확장 지점

```text
src/hidden_view_finder/
  models.py         요청 검증·시간대·필수 조건 입력
  scenarios.py      외부 자료 없는 명시적 가상 사례
  providers.py      보행 그래프·선택적 날씨 예보
  seoul.py          기존 가시성 엔진과 실제 지도 노드 연결
  ranking.py        필수 조건·근거 비중·점수·중복 제거
  sunlight.py       근사 태양 위치 계산
  presentation.py   순위 후 설명·이미지 프롬프트·기본 그림
  service.py        실행 순서와 구조화된 결과
  server.py         로컬 HTTP API
  static/           화면·스타일·스크립트·가상 예상 이미지
src/seoul_visibility/
  ...               독립적인 전처리·2.5D 점 가시성 엔진
scripts/demo/
  run.py            데이터 없는 데모 진입점
  acquire_context.py 작은 실제 OSM 맥락의 일회성 취득
```

다음 단계에서 실제 확정 추천을 늘리려면 시간대별 출입 정보, 전 구간 접근성을 검증할 보행 라우팅 자료, 방문 인원 관측·예측과 다양한 대상의 검증된 좌표·높이가 필요합니다. 물·능선·스카이라인 전체의 장면 특징은 분산 대상 표본 또는 별도 기하 분석으로 추가해야 합니다. 이미지 생성 공급자는 이러한 근거 평가와 순위가 끝난 뒤에 연결합니다.

기존 가시성 표면의 해상도, 높이 추정, 나무·담장·공사 누락, 수직 기준의 한계는 그대로 적용됩니다. 이 추천 데모를 추가하면서 기존 엔진을 전체 풍경의 정밀 재구성이나 현재 현장 상태를 확인하는 도구로 바꾸지는 않았습니다. [엔진 기술 문서](engine.md), [원본 자료 설명](data-sources.md), [추천 맥락 자료 설명](demo-sources.md)을 함께 참고하세요.

## 재현 가능한 데모 검증

서비스 시간과 집계 결과를 기록합니다. 원본 GIS나 개별 실제 후보를 내보내지 않습니다.

```bash
python3 scripts/demo/validate.py --output data/demo/validation.json
# 서울 입력과 GDAL 환경이 준비된 경우
.venv/bin/python scripts/demo/validate.py --seoul
```

브라우저 검사는 선택 사항입니다. 별도 터미널에서 데모 서버를 실행한 뒤, 개발 환경에 Playwright와 Chromium을 준비합니다. Linux에서는 Chromium 시스템 라이브러리가 추가로 필요할 수 있습니다.

```bash
.venv/bin/python -m pip install playwright==1.58.0
.venv/bin/python -m playwright install chromium --only-shell
# Linux에서 라이브러리가 없을 때: .venv/bin/python -m playwright install-deps chromium
.venv/bin/python scripts/demo/check_browser.py --url http://127.0.0.1:8000
```

스크립트는 데스크톱·모바일, 이미지, 비교표, 근거 패널, 필수 조건과 초기화를 검사하고 `data/demo/browser/`에 JSON과 스크린샷을 저장합니다. 실제 서울 모드가 준비된 경우 미확인 후보 분리도 검사합니다. [이번 실행 기록](demo-validation.md)을 참고하세요.
