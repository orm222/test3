# FabQuality 문제 관련 합성 영상 5개

## 자료의 성격

첨부된 `FABQUALITY_ORAL_FINAL(4).zip`의 `fabquality_worker/PROBLEM.md`와 이미지 생성 규칙을 바탕으로 만든 **추가 연습 자료**다. 공식 기출 영상, 실제 공장 촬영, 실제 SEM 데이터가 아니다. 원본 ZIP에는 기준 PNG 12장·검사 PNG 12장·정답 마스크 12장(총 36장), 구현 코드와 테스트 등이 있고 영상은 없다.

원문 과제는 **128×128, 8비트 단일 채널 PNG 기준·검사 이미지 쌍**을 입력받는다. MP4 입력 기능은 원문 요구사항이나 제공 코드에 없다. 이 영상 묶음은 같은 패턴의 시간에 따른 변화와 경계값을 연습하도록 확장한 자료다. 생성형 영상 모델은 사용하지 않았으며, 원본 기준 이미지에 정해진 좌표의 픽셀 변화와 ±5 잡음을 주입하고 FFmpeg로 인코딩했다.

## 영상

모두 12초, 12fps, 144프레임, 768×768, 무음, H.264 RGB 무손실 MP4다. 카메라 이동·회전·확대 변화·자막·판정 표시는 넣지 않았다. 원본 128×128 픽셀을 최근접 방식으로 6배 확대했다. 아래 구간은 시작 포함·끝 제외다.

| 파일 | 시간별 내용 | 예상 결과 |
|---|---|---|
| `01_normal_noise.mp4` | 0–12초: ±5 잡음 | 전체 normal |
| `02_pattern_addition.mp4` | 0–3초 정상, 3–9초 배경에 9픽셀 추가, 9–12초 정상 | normal → defect → normal |
| `03_pattern_removal.mp4` | 0–3초 정상, 3–9초 밝은 패턴에서 6픽셀 누락, 9–12초 정상 | normal → defect → normal |
| `04_boundary_3_to_4.mp4` | 0–3초 정상, 3–6초 3픽셀 추가, 6–9초 4픽셀 추가, 9–12초 정상 | normal → normal → defect → normal |
| `05_boundary_63_64_65.mp4` | 0–3초 정상, 3–6초 63픽셀, 6–9초 64픽셀, 9–12초 65픽셀 추가 | normal → standard → urgent → urgent |

4번의 후보 3픽셀은 최종 결함 픽셀 수가 **0**, 마스크는 모두 0, bbox는 null이다. 4픽셀이 되면 defect다. 밝기 절댓값 차이가 **32를 초과**한 픽셀만 후보로 센다.

5번은 정책 A(64픽셀 또는 3 LOT)를 사용하되 재검토 LOT를 L1·L2 두 개로 고정한 정적 fixture를 사용한다. 원본 그래프에서 R8과 그 연결만 제거했다. L2의 R2·R3 두 경로는 남겨, 3개 실행 경로를 3 LOT로 잘못 세지 않는지도 확인할 수 있다. 1–4번은 원본 그래프를 사용하고 R1의 관련 LOT는 L1·L2·L3이므로, 결함일 때 작은 결함도 LOT 조건으로 urgent다.

## 실제 입력과 정답

- `references/`: 영상별 기준 PNG 5장.
- `frames/`: 인코딩에 사용한 검사 PNG **720장**, 모두 128×128 단일 채널.
- `masks/`: 해당 프레임의 최종 정답 마스크 720장.
- `labels.json`, `labels.csv`: 프레임 인덱스·시간·후보 수·최종 판정·결함 수·정책 등.
- `videos.json`: 영상 규격과 구간별 예상 결과.
- `source_data/`: 사용한 원본 기준 이미지와 원본 ontology.
- `ontology_policy_A_2_lots.json`: 5번 전용 정적 그래프.
- `PREVIEW.png`: 영상 다섯 개의 7초 프레임 미리보기.

기존 서비스에는 MP4 대신 `references/{영상ID}.png`와 `frames/{영상ID}/{프레임번호}.png`를 `reference`·`test`로 넣는다. `run_id`는 R1이다. 각 프레임을 독립 사건으로 시험할 때 사용할 고유 event_id 예시는 labels.json에 있다. 서로 다른 프레임에 같은 event_id를 반복하면 픽셀이 달라 409 충돌이 발생할 수 있다.

영상의 0–12초는 미디어 시간이며, 제조 이력의 종료 시각이나 60분 조회 범위를 대신하지 않는다. 그래프는 시작 시 고정한다. 5번 fixture는 별도의 실행 환경에서 로드한다. `expected_state_after_first_step` 등은 원문으로부터 계산한 **예상값**이며, 이 묶음 제작에서 전체 HTTP 승인·재시도 흐름을 새로 실행했다는 뜻은 아니다.

## 재생과 정확성

RGB 무손실 H.264는 일부 플레이어에서 재생 지원이 제한될 수 있다. FFmpeg로 전체 프레임을 디코딩하고 128×128 최근접 축소한 RGB 각 채널이 원본 PNG와 **모두 정확히 같은 것**을 검사했다. 일반 재인코딩·보간 축소를 하면 픽셀값이 달라질 수 있으므로 정밀 입력에는 동봉 PNG를 사용한다.

## 재생성 및 검증

Python 3.9 이상, NumPy, Pillow, 시스템 FFmpeg/ffprobe가 필요하다. 이번 실제 검증 환경 버전은 `VALIDATION.json`을 참고한다. Python 3.9 자체에서 별도 실행한 것은 아니다.

```bash
python3 build_videos.py --source-data source_data --output regenerated
python3 -m unittest -v test_videos.py
```

원본 ZIP의 검사·온톨로지 코드까지 함께 검증하려면:

```bash
FABQUALITY_WORKER_PATH=/path/to/fabquality_worker python3 -m unittest -v test_videos.py
```

환경변수를 지정하지 않으면 원본 모듈 대조 테스트 1개만 skip되며, 영상·PNG·경계 검사는 수행된다. 제작 시에는 원본 모듈을 지정해 **5개 테스트 전부 통과, skip 0개**를 확인했다. 전체 720프레임의 판정·마스크·bbox와 결함 프레임의 LOT·정책 결과도 원본 코드에 대조했다. 전체 서비스, HTTP API, 사람 승인, 티켓 생성·재시도를 재검증한 결과는 아니다.

오픈소스 도구: [FFmpeg](https://ffmpeg.org/), [NumPy](https://numpy.org/), [Pillow](https://pillow.readthedocs.io/). 원본 문제·이미지·규칙은 사용자가 제공한 ZIP에서 가져왔다. 실제 제조 환경의 성능을 평가할 수 있는 데이터셋은 아니다.
