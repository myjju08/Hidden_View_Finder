# Hidden View Finder · 서울 한 점 가시성 엔진

서울의 **높이가 명시된 3D 목표점 하나를 지상 어디에서 볼 수 있는지** 계산하는 Python 패키지입니다. 지형과 높이 속성이 있는 2D 건물을 2.5D 표면으로 준비하고, 목표점 중심의 GDAL viewshed 한 번으로 주변 관측 위치를 판정합니다.

**실제 서울 자료로 5 km / 5 m 계산을 실행·검증했습니다.** 개발 환경의 warm uncached 중앙값 **0.544초**, p95 **0.599초**, 자동 테스트 **173개 통과**입니다. 현재 검증 범위는 중앙 서울 11.5 × 11.5 km이며 건물 높이는 추정값이므로 결과는 **APPROXIMATE**입니다.

이 GitHub 저장소에는 **코드·테스트·설정 예제·취득 스크립트·집계 성능 보고서**를 올렸습니다. 원본 SHP, 건물 도형, 가공 래스터, 공간 인덱스, 지도 결과 등 **데이터 파일은 포함하지 않습니다.**

[데이터 상세](data-sources.md) · [실측 성능·검증](benchmarks.md) · [English technical guide](engine.md)

## 어떤 데이터를 사용했나요?

| 용도 | 실제 사용한 자료 | 시점·규모 | 처리와 한계 |
| --- | --- | --- | --- |
| 지형 | [서울 열린데이터광장 / 국토지리정보원 등고선·표고점](https://data.seoul.go.kr/dataList/OA-22241/F/1/datasetView.do) | 2023년 자료, 등고선 8,570개 + 표고점 45,870개 | 실제 `CONT`·`NUME` 표고 필드(m), EPSG:5174 / CP949. 타일별 TIN으로 5 m DTM 생성 |
| 건물 장애물 | [GlobalBuildingAtlas](https://github.com/zhu-xlab/GlobalBuildingAtlas)의 [Source Cooperative 변환본](https://source.coop/tge-labs/globalbuildingatlas-lod1) | 2025년 공개본, 높이 영상은 주로 2019년·일부 2018년. 주변 294,919개 취득, 준비 격자에 62,482개 사용 | 2D 도형 + `height_m` AGL **추정 높이**. 2025년 실측 건물 높이가 아님 |
| 서울 출력 경계 | [OpenStreetMap relation 2297418](https://www.openstreetmap.org/relation/2297418) | 2026-09-07 취득 | 장애물 계산 후 출력 마스크로 적용. 출입 가능 여부는 판단하지 않음 |
| 과거 건물 비교 | [서울시 2015년 건물 SHP](https://data.seoul.go.kr/dataList/mapView.do?infId=OA-13224&srvType=M) | 원본 659,187개 | 지상층수 `GRO_FLO_CO` 검사·비교용. 실측 높이 필드가 없어 계산용 높이로 혼합하지 않음 |

지형은 공공누리 1유형, OSM 경계는 © OpenStreetMap contributors / ODbL입니다. GBA는 원 출처에 따라 ODbL 및 **CC BY-NC 4.0** 조건이 적용됩니다. 데이터의 이용 조건은 코드 배포와 별개입니다. 정확한 필드, 좌표계, 수직 기준, 높이 추정, 출처별 이용 조건은 [데이터 문서](data-sources.md)에 정리했습니다.

최신 공식 실측 높이 자료는 당시 다운로드 접근 오류로 확보·검증하지 못했습니다. 지형과 건물의 시점 차이, 누락 건물, 추정 높이 오차가 남아 있으며 **서울 전역의 최신 현황이나 현장 가시성 정확도를 보장하지 않습니다.**

## 계산 결과의 의미

- 목표점은 WGS84 **경도, 위도** 순서와 명시적인 높이를 받습니다. `agl`은 **맨땅 DTM 위 높이**이고 건물 지붕 위 높이가 아닙니다. `absolute`는 DTM과 같은 수직 기준 선언이 필요합니다.
- 관측자는 열린 지면의 `DTM + 눈높이`에 있습니다. 건물 셀은 관측 위치에서 제외하지만 모든 건물은 장애물로 유지합니다. 지붕 아래에 묻힌 목표점은 오류로 거부합니다.
- 기본 반경은 5 km, 격자는 5 m, 눈높이는 1.7 m, 곡률 계수는 6/7입니다. 목표점은 포함 셀 중심으로 정렬되며 요청·유효 좌표를 모두 반환합니다.
- 필요한 지형·건물 높이가 누락되면 `IncompleteCoverageError`로 거부합니다. 미지 영역을 높이 0으로 채우지 않습니다.
- 출력은 `blocked=0`, `visible=1`, `excluded=2`, `unknown=3`인 작은 `uint8` 래스터입니다. `excluded`와 `unknown`은 차폐 판정이 아닙니다.

5 m 격자는 모든 골목·전경 장애물을 구분하지 못합니다. 건물의 보수적 `all_touched` rasterization과 최고 지형 기반 지붕 추정은 좁은 틈을 닫거나 차폐를 과대평가할 수 있습니다. 수목·간판·발코니·교량 하부·대기 상태는 포함하지 않으며, 결과는 건물 전체의 가시성·경관의 매력·공공 출입 가능성을 뜻하지 않습니다.

## 데이터 없이 바로 실행하기

검증 환경은 **Ubuntu 24.04 / Python 3.12 / GDAL 3.8.4**입니다. 시스템 GDAL과 Python 바인딩 버전을 맞춰야 합니다. 고정 버전은 [requirements.lock](../requirements.lock), 설치·자원 검사는 [scripts/install.sh](../scripts/install.sh)에 있습니다.

```bash
git clone https://github.com/myjju08/Hidden_View_Finder.git
cd Hidden_View_Finder

# Ubuntu 24.04: Python·venv·컴파일러와 시스템 GDAL 개발 라이브러리 설치
sudo apt-get install --no-install-recommends \
  python-is-python3 python3-venv python3-dev build-essential libgdal-dev
bash scripts/install.sh
.venv/bin/python -m pytest -q

# 서울 원본 없이 작은 합성 지형을 만들고 dense/sparse/cache API 실행
.venv/bin/python examples/usage.py
```

합성 예제는 가상의 지형·건물을 로컬 `data/usage/`에 생성합니다. 실제 서울 결과와 구분됩니다. GPU, 3D 메시, ML 학습, 데이터베이스 서버는 필요하지 않습니다.

## 실제 서울 입력 취득·준비·질의

아래 스크립트는 공개 출처에서 검증한 자료를 취득하고 로컬 `data/`에 보존합니다. 외부 서비스가 변경되거나 응답 해시가 달라지면 재검사를 요구하며 멈춥니다. 취득·보간·건물 rasterization은 **한 번만 수행**하고, 이후 질의는 준비 래스터의 필요한 창만 읽습니다.

```bash
.venv/bin/python -m pip install -r requirements-acquisition.txt
.venv/bin/python scripts/acquire_terrain.py
.venv/bin/python scripts/acquire_buildings.py
.venv/bin/python scripts/acquire_seoul_boundary.py

# 작은 영역과 중앙 서울 11.5 km 정방형 지형 준비
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
  .venv/bin/python scripts/subset_seoul_terrain.py --prepare-pilot --prepare-full

# 명시적으로 최고 지형 기반 건물 바닥 추정을 선택한 별도 근사 제품
.venv/bin/python scripts/prepare_real_seoul.py --area pilot --base-method maximum --prepare
.venv/bin/python scripts/prepare_real_seoul.py --area central --base-method maximum --prepare

# 광화문 부근의 지면 위 100 m 가상 목표점, 반경 5 km
.venv/bin/python examples/real_seoul.py
```

총 프로젝트 데이터 20 GiB, 최소 여유 공간 8 GiB, 임시 파일 4 GiB 정책을 검사합니다. 기존 소스나 다른 결과를 덮어쓰지 않습니다. 데이터·로컬 경로가 들어간 생성 설정은 `.gitignore`로 제외합니다. 과거 공식 건물 비교 자료는 선택적으로 `scripts/acquire_official_buildings.py`로 취득할 수 있습니다.

```python
from seoul_visibility import State, TargetPoint, VisibilityEngine

with VisibilityEngine.from_manifest(
    "data/seoul/processed/central-gba-maximum/manifest.json"
) as engine:
    # 특정 건축물의 실측 높이가 아닌, 명시적인 가상 점
    target = TargetPoint(126.9777, 37.578, 100.0, "agl")
    result = engine.visible_from_target(
        target=target,
        radius_m=5_000,
        eye_height_m=1.7,
        resolution_m=5,
        curvature_coefficient=6 / 7,
    )
    visible = result.states == State.VISIBLE
    print(result.timings, result.metadata["quality"])
```

다른 SHP/DXF/DTM에는 `inspect → plan → prepare → query` 명령과 [명시적 필드 설정 템플릿](../examples/config.template.json)을 사용합니다. 좌표계·높이 단위·수직 기준을 모호하게 추정하지 않습니다. 후보 관측점 마스크와 소수 관측 좌표의 LOS 검사도 지원합니다. [API·전처리 상세](engine.md)를 참고하세요.

## 실제 측정 결과

중앙 서울 5 m 준비 자료, Threadripper PRO 3955WX, GDAL 3.8.4에서 반경별 **30회 결과 캐시 없는 질의 + 30회 동일 결과 캐시 조회**를 측정했습니다. 파일시스템이 따뜻한 상태이며 cold-disk 성능이 아닙니다.

| 반경 | 결과 캐시 없는 중앙값 | p95 | 동일 결과 캐시 중앙값 |
| --- | ---: | ---: | ---: |
| 1 km | 0.01693 s | 0.02257 s | 0.717 ms |
| 3 km | 0.18628 s | 0.21253 s | 0.707 ms |
| **5 km** | **0.54365 s** | **0.59854 s** | **0.721 ms** |
| 10 km | 준비 범위 부족으로 미측정 | — | — |

별도 2 m 실제 자료는 준비하지 않았으며 5 m 제품을 확대한 결과를 2 m로 부르지 않습니다. 자동 테스트 **173개 통과**, 실제 표본 100셀의 GDAL/reference 비교에서 **false-visible 3개 / false-blocked 0개**가 있었습니다. 이는 서로 다른 표면 교차 모델의 불일치이며 현장 정확도 측정이 아닙니다.

[측정 환경·메모리·전처리·한계](benchmarks.md) · [JSON](../reports/seoul-real-benchmark/benchmark.json) · [180회 CSV](../reports/seoul-real-benchmark/runs.csv) · [합성 1–10 km 벤치마크](../reports/benchmark_report.md)

```bash
.venv/bin/seoul-visibility benchmark \
  data/seoul/processed/central-gba-maximum/manifest.json \
  --output reports/reproduced-seoul --runs 30 --radii 1000 3000 5000 10000
```

명령은 지원하지 않는 목표점·반경의 제외 사유도 기록합니다. 자세한 기하 계약, 곡률 수식, GDAL 셀 정렬 측정, 독립 LOS reference와 의존 영역 검증은 [기술 문서](engine.md)에 있습니다.

## 코드 구성

| 경로 | 역할 |
| --- | --- |
| [src/seoul_visibility](../src/seoul_visibility) | 타입·공개 API, GDAL adapter, 데이터 검사·전처리, LOS reference, 자원·캐시 관리 |
| [scripts](../scripts) | 실제 자료 취득, 지역 준비, 검증·프로파일 재현 |
| [tests](../tests) | 결정적인 합성 fixture와 기하·누락 입력·예산·재개 회귀 검사 |
| [examples](../examples) | 합성 및 실제 서울 API 예제, 입력 설정·manifest 예제 |
| [docs](../docs) / [reports](../reports) | 출처 설명 및 데이터 자체를 제외한 집계 검증 결과 |
