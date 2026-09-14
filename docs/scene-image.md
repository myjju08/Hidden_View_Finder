# 분석 결과로 예상 풍경 이미지 생성

기본 웹 앱(`scripts/demo/run.py`, `hidden-view-demo`)의
결과 카드에서 **예상 풍경 생성**을 누르면, 해당 분석 결과를 바탕으로 OpenAI API가
PNG 이미지를 생성합니다. 키 이외의 필수 이미지 설정이나 추가 Python 패키지는 없습니다.
기존 서울 가시성 분석에 필요한 GIS 자료와 환경은 별도로 준비되어 있어야 합니다.

## API 키 설정과 실행

프로젝트 루트에서 `.env.example`을 `.env`로 복사하고 다음 값을 입력합니다.
기존 `.env`가 있으면 복사하지 말고 해당 항목만 추가·수정합니다.

```dotenv
OPENAI_API_KEY=your-api-key
```

```bash
python3 scripts/demo/run.py
```

`http://127.0.0.1:8000`에서 입력 조건을 채우고 풍경 찾기 → 결과 카드의
**예상 풍경 생성**을 누릅니다. 로딩 후 같은 카드에 이미지가 표시됩니다.
페이지 진입과 분석만으로는 유료 API를 호출하지 않습니다. 생성 버튼을 누르면 API 비용이 발생합니다.
키는 브라우저에 입력하지 않으며, 응답·프롬프트·JSON 내보내기에 포함되지 않습니다.
키를 변경하면 서버를 재시작합니다. 환경변수로 내보낸 값이 `.env`보다 우선합니다.
다른 경로의 파일은 `--env-file /path/to/.env`로 지정할 수 있습니다.
Windows의 UTF-8 BOM·CRLF 파일과 `OPENAI_API_KEY="키" # 주석` 형식도 지원합니다.

이미지 생성 권한과 결제가 활성화된 OpenAI API 키가 필요합니다.
기본 모델은 `gpt-image-2.5-sunburst`, 크기는 `1536x1024`, 품질은 `medium`입니다.
Images API의 `POST /v1/images/generations`와 PNG base64 응답을 사용합니다.
규격 확인: [공식 OpenAI 이미지 생성 문서](https://developers.openai.com/api/docs/guides/image-generation)
(2026-09-14 재확인).

선택 설정:

| 환경변수 | 기본값 | 설명 |
| --- | --- | --- |
| `HVF_SCENE_IMAGE_MODEL` | `gpt-image-2.5-sunburst` | 계정에서 사용할 수 있는 GPT Image 모델 ID |
| `HVF_SCENE_IMAGE_SIZE` | `1536x1024` | `1536x1024`, `1024x1024`, `1024x1536` |
| `HVF_SCENE_IMAGE_QUALITY` | `medium` | `low`, `medium`, `high` |
| `HVF_SCENE_IMAGE_ENABLED` | `true` | `false`이면 키가 있어도 생성 중지 |

별도 전역 프로토타입(`scripts/prototype/run.py`)의 `HVF_AI_ENABLED`, 예산·가격표 설정은
이 모듈과 독립적이며 그대로 유지됩니다. 그 프로토타입 화면의 기존 AI 버튼은 변경하지 않았습니다.
프로토타입 `SceneEvidence` 출력은 아래 Python 모듈과 CLI에서 바로 사용할 수 있습니다.

## 어떤 분석 정보를 반영하는가

- 방향·수평 시야각·눈높이, 근거가 있는 관측자 절대고도
- 보이는 대상의 주요 명칭과 종류: 산·호수·강·건물 등
- 대상별 거리, 상대 방위각, 화면의 좌우 위치, 계산된 고도각
- 대상 높이와 높이 기준: `agl` 지면 위 목표점 높이와 절대고도 구분
- 방문 시각과 제공된 태양 위치

`blocked`, `unknown`, 시야각 바깥의 표본은 이미지 대상에서 제외합니다.
주변 지도에 호수가 있다는 사실만으로 보이는 호수로 추가하지 않습니다.
가시성이 확인된 호수가 출력에 포함되면 그 명칭과 수면 고도를 반영합니다.
현재 전역 프로토타입은 수면 고도 근거가 부족한 강 표본을 `unknown`으로 반환하므로,
그런 표본은 생성 장면에도 포함되지 않습니다.

단일 목표점 높이를 산 전체 높이나 건물 전체 크기로 해석하지 않습니다.
값이 없는 높이·고도각은 `null`로 유지합니다. 시야각이 없는 기본 앱 결과는
60°를 가정하며 `camera.fov_assumed=true`로 구분합니다.
이미지 모델에 수치와 구도를 지시하는 방식이며, 기하학적으로 정확한 렌더링을 보장하지 않습니다.
이미지에는 **AI 생성 예상 풍경 · 실제 경치와 다를 수 있습니다**를 표시하고,
추천 순위·가시성 판정에는 사용하지 않습니다.

## 내보낸 output JSON → PNG

화면의 JSON 다운로드 결과를 저장한 뒤 실행합니다.

```bash
python3 examples/generate_scene_image.py --input hidden-view-result.json --output output/view.png
```

기본은 첫 번째 추천이며 `--candidate-id forest_window`처럼 지정할 수 있습니다.
`recommendations`/`unverified`, 프로토타입의 `views`, 단일 `SceneEvidence`를 지원합니다.
PNG와 함께 이름·높이·프롬프트·모델 정보가 담긴 `output/view.json`을 저장합니다.

API 호출 없이 산·호수 예시의 프롬프트와 변환 결과 확인:

```bash
python3 examples/generate_scene_image.py --input examples/scene-image.synthetic.json --output output/example.png --dry-run
```

`--dry-run`을 빼면 `.env`의 키로 실제 이미지를 생성합니다. 예시의 지명과 수치는 가상입니다.

Python에서 재사용:

```python
from pathlib import Path
from hidden_view_finder.scene_image import load_image_env, build_scene, SceneImageGenerator

load_image_env(Path('.env'))
scene = build_scene(
    output['recommendations'][0],
    output['landmarks'],
    output['request_summary']['request'],
)
image = SceneImageGenerator().generate(scene)
Path('view.png').write_bytes(image['png'])
```

## 웹 API와 작업 수명

1. `POST /api/recommend`: 각 후보의 `image.scene`에 변환된 장면,
   `image.generation.scene_id`에 서버가 발급한 장면 ID가 추가됩니다.
2. `POST /api/images`에 `{"scene_id":"..."}` 전달: `202 queued/running`.
3. `GET /api/images/{scene_id}` 조회: `generated`가 되면 `url`, `scene`, `prompt`, 모델 설정 반환.
4. 반환된 `/api/images/{scene_id}/content`에서 PNG 표시·저장.

같은 장면의 중복 요청은 기존 작업을 조회합니다. 임의 프롬프트나 API 키를 받는 웹 엔드포인트는 없습니다.
이미지 작업은 가시성 계산과 다른 워커에서 실행하며 동시 실행 1개, 대기 포함 3개로 제한합니다.
장면 최대 64개·완성 이미지 최대 8개를 메모리에 보관합니다. 1시간 만료 또는 용량 초과·서버 재시작 시
사라지므로 보관하려면 PNG와 JSON을 저장합니다. 만료 시 분석을 다시 실행합니다.

키 없음, 인증/모델 권한 오류, 사용량/잔액 제한, 네트워크 오류, 잘못된 이미지 응답을 구분합니다.
연결이 끊기면 화면의 **생성 상태 확인**으로 조회를 이어갑니다.
실패한 API 호출은 자동 재시도하지 않으며 동일 장면의 실패도 만료까지 보존합니다.
키·모델 설정을 고친 경우 서버를 재시작한 뒤 다시 분석·생성할 수 있습니다.
공개 호스팅에는 기존 인증 프록시를 사용해야 서버의 키로 생성하는 버튼에 대한 접근을 제한할 수 있습니다.

## API 키만으로 가능한 범위와 검증 결과

2026-09-13 검증에서 Python 3.12의 `-S` 옵션(설치 패키지 로딩 안 함)으로
새 서버 프로세스를 실행하고 `.env`에 `OPENAI_API_KEY` 한 항목만 넣었을 때
이미지 기능 활성화와 분석 결과의 생성용 장면 ID 반환을 확인했습니다.
키가 없는 경우에는 가상 분석은 정상 동작하고 이미지 생성만 비활성화됩니다.

| 확인 항목 | 결과 |
| --- | --- |
| 새 작업 사본·추가 패키지 없이 가상 분석 실행 | 통과 |
| API 키 한 항목으로 기본 모델·크기·품질 자동 선택 | 통과 |
| .env 자동 로딩, 내보낸 환경변수 우선, BOM·CRLF·주석·공백 | 통과 |
| 시야각·지형 명칭·높이 기준·거리 전달, 가림/미확인 제외 | 통과 |
| HTTP 생성 요청 → 상태 조회 → PNG 응답 | 모의 제공자 통합 테스트 통과 |
| 중복 호출 병합, 대기열·캐시 제한, 인증/잔액/네트워크 오류 | 통과 |
| Chromium에서 생성 버튼·로딩·이미지 디코딩·모바일 화면 | 모의 제공자 브라우저 테스트 통과 |
| 실제 OpenAI 계정의 결제·권한 및 유료 이미지 생성 | 키 미제공으로 미실행 |

관련 회귀 테스트: 기본 Python 환경에서 **155 passed, 1 skipped**. 생략된 GDAL 의존
테스트 1개도 GIS 가상환경에서 별도로 실행해 **통과**했습니다. 테스트 자체는 OpenAI에
요청을 보내지 않습니다.

```bash
python -m pytest tests/test_scene_image.py tests/test_public_server.py tests/test_demo_service.py tests/test_recommendation.py -q
python -S examples/generate_scene_image.py --input examples/scene-image.synthetic.json --output /tmp/scene-example.png --dry-run
```

Playwright/Chromium이 설치된 개발 환경의 브라우저 재검증:

```bash
python scripts/demo/check_scene_image.py
```

따라서 **이미 분석 결과가 있거나 가상 시나리오로 실행하는 경우, 이미지 기능의 추가 필수 설정은
유효한 OpenAI API 키 한 개**입니다. 서울 실제 지형 분석 자료, OpenAI 계정의 모델 권한·결제,
외부 HTTPS 연결까지 API 키가 대신 준비해 주는 것은 아닙니다.

### 2026-09-14 브랜치 게시 전 재검증

`adding-API(image)`의 독립 작업 사본에서 관련 테스트 **156개가 모두 통과**했습니다.
GIS 환경으로 실행해 앞서 생략됐던 테스트도 포함했습니다. Chromium 모의 제공자 검사도
통과했습니다: 버튼 클릭 전 API 호출 없음, 생성 대기 표시, PNG 디코딩·표시,
중복 호출 방지, 키 비노출, 모바일 화면 및 JavaScript 오류 없음.
추가 패키지를 로딩하지 않는 `python3 -S` CLI의 `--dry-run`도 통과했습니다.

점검한 로컬 프로젝트에는 `.env`가 없었고 실행 환경의 `OPENAI_API_KEY`도
설정되어 있지 않았습니다. 따라서 **키 입력을 위한 코드는 준비됐지만 실제 키는
미설정이며, 실제 계정 인증·결제·모델 권한 및 유료 생성 성공은 검증하지 않았습니다.**
`.env.example`의 키 값은 빈칸으로 유지하며, 실제 키를 넣은 `.env`는 Git에서 제외됩니다.
