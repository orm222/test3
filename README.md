# FabQuality Worker

실제 PNG 차분 → 정적 제조 관계 → 정책 → 설명 초안 → 사람 승인 → 로컬 mock 등록을 수행한다. 외부 API·LLM·작업 스레드·백그라운드 큐를 앱에서 생성하지 않는다. CPU, Python 3.9, 단일 프로세스·순차 요청·인메모리 저장소를 전제로 한다. 재시작하면 사건과 티켓은 사라진다.

## 설치·실행·평가

프로젝트 루트에서 실행한다. 의존성 버전은 requirements-app.txt에 고정되어 있다.

```sh
python3.9 -m venv .venv
.venv/bin/python -m pip install -r requirements-app.txt
.venv/bin/python -B -m fabquality.data --output data/synthetic
.venv/bin/python -m uvicorn fabquality.app:create_app --factory --workers 1 --host 127.0.0.1 --port 8000
```

다른 터미널에서 사용한다. `/step` 첫 호출은 검사·추적·정책·초안을 계산하고 승인 대기에서 멈춘다. 정상은 CLEARED로 종료한다. 반환된 case_id로 조회한다.

```sh
curl -F event_id=example -F run_id=r0 -F reference=@data/synthetic/evaluation/2-reference.png -F test=@data/synthetic/evaluation/2-test.png http://127.0.0.1:8000/cases
curl -X POST http://127.0.0.1:8000/cases/case-1/step
curl http://127.0.0.1:8000/cases/case-1
curl -H 'Content-Type: application/json' -d '{"approve":true}' http://127.0.0.1:8000/cases/case-1/decision
curl -X POST http://127.0.0.1:8000/cases/case-1/step
curl -X POST http://127.0.0.1:8000/eval/run
```

서버 없이 동일 평가 처리와 unittest를 실행할 수도 있다.

```sh
.venv/bin/python -B -m fabquality.evaluation
.venv/bin/python -B -m unittest discover -s tests/application -v
.venv/bin/python -B .harness/check_unittest.py --start-directory tests/acceptance
```

`create_app(ontology=None, testing=False, ticket_mode='normal', draft_mode='normal')`는 인스턴스마다 메모리를 격리한다. 설정은 서버 생성자에서만 주입한다. ticket_mode는 normal/after_commit_once/always_fail, draft_mode는 normal/fail이다. `testing=True`에만 `GET /_test/mock`가 존재한다. 평가 요청은 별도 Service들을 생성하고 운영 사건·카운터에 접근하지 않는다.

## 판단과 경계

검사기는 두 배열을 signed int16으로 빼고 절댓값 >32 후보 전체를 센다. 후보 1~3개는 최종 마스크가 0이고, 4개 이상은 분리된 픽셀도 모두 포함한다. bbox는 half-open이다. 이 **이미지 이상**은 확정 불량을 의미하지 않는다. **연관 LOT**는 같은 설비·검사 실행의 event-time 기준 60분 양끝 포함 조회 결과이며, 원인·확정 불량을 의미하지 않는다. 초안은 이 근거의 검토만 요청한다.

Graph는 타입·ID·방향·참조·필수 outgoing cardinality·offset 시간·정책 타입과 임계를 시작 시 검사한다. 제조 데이터는 복사하고 사건별 Inspection/INSPECTS만 별도 추가한다. 시각 원문은 근거에 보존한다. LOT는 정렬·중복 제거하고 경로는 전부 보존한다. 정책은 픽셀 또는 LOT 임계 이상이면 urgent이며 경로 수로 LOT 수를 대신하지 않는다.

파일별 1MiB 초과를 다른 필드 오류보다 먼저 413으로 반환한다. 이후 손상/크기/채널/필드/run 오류는 422다. 거절된 입력은 사건이나 event_id를 예약하지 않는다. 동일 event_id는 디코딩 배열과 run을 비교해 중복 또는 409로 나눈다. 승인 대기 step과 잘못된 결정은 409, 없는 사건은 404다. 같은 결정 재전송·종료 step은 이력을 추가하지 않는다.

초안과 근거는 외부로 복사해 반환하며 이후 수정 경로가 없다. BLOCKED는 검사·추적·정책을 보존하고 draft만 null이다. 승인 후 호출 직전 attempts를 올리고 mock을 한 번 호출한다. 저장 후 응답유실 시 동일 초안 재전송은 저장 티켓을 반환한다. 다른 초안은 오류다. 세 번째 실패는 FAILED이며 오류·이력을 보존한다. 정상·반려·BLOCKED는 mock을 호출하지 않는다.

## 자료·평가·요구사항 연결

`data/synthetic`은 독자 생성한 calibration 4쌍(104729+i)·evaluation 8쌍(130363+i)의 실제 L PNG다. 추가는 배경, 누락은 밝은 패턴에 주입하며 잡음은 ±5 범위다. truth PNG는 주입 좌표에서 생성한다. 정답·시드·파일명은 서비스에 전달하지 않는다. `data/FABQUALITY_VIDEOS_5`는 별도 연습 자료이며 원문 평가 자료와 동일하다고 주장하지 않는다.

| 요구사항 | 구현 | 검증 |
|---|---|---|
| R001 통합/HTTP | fabquality/app.py, core.py Service | 고정 01~15, 추가 ASGI |
| R002 독립 PNG | fabquality/data.py, data/synthetic | 고정 14, 평가 이미지 8쌍 |
| R003 픽셀·마스크 | core.py inspect/decode | 고정 01/02/11, 추가 경계 |
| R004/005 그래프·시간 | core.py Graph, ontology.json | 고정 03/04, 추가 ontology |
| R006 정책·불변 초안 | core.py Graph.policy, Service.step | 고정 09/10, 추가 copy/mock |
| R007 상태·승인 | core.py Service | 고정 01/05/06/08, 추가 ASGI/복구 |
| R008 실제 mock·재시도 | core.py MockTickets | 고정 07/08/10, 추가 복구 |
| R009 입력·중복 | app.py receive, core.py receive | 고정 04/05/12, 추가 ASGI |
| R010 독립 평가 | fabquality/evaluation.py | 고정 15, 추가 결함 주입 |
| R011 범위 | 현재 README, INTERVIEW.md | 고정 13 |
| R012 문서·재현 | README, INTERVIEW | 최종 자기보고; 보호 HANDOFF 유지 |

경계 매핑은 Q-001/004→test_13_reference_excluded_service_scope, Q-002/005→test_08_blocked_preserves_results_and_permanent_failure, Q-003/006→test_12_size_priority_no_event_created이다. SELECTION_REFERENCE는 이번 진단에서 제외된다. 이는 사용자 위임에 따른 진단 조건이며 공식 출제자 답변을 주장하지 않는다.

평가의 기대값은 고정 시나리오 설계에서 독립적으로 검산했다. 실제 생성·처리·승인·재시도를 실행한 결과로 분자를 집계한다. 18개 결함 사건, 43 사건/LOT, 61 경로, 8 티켓을 기대한다. 이미지 8개·양성 340픽셀만 이미지 지표에 포함한다. 비율은 numerator/denominator/value이고 분모 0은 null이다. 실패는 masks/paths/history 진단으로 남긴다. 생성 설명 stub은 기본값 대체나 외부 모델 호출을 하지 않는다. 모델 품질·현실 공장 성능은 미측정이다.

## 이번 구현자의 실행 상태와 한계

이 문서는 builder 자기보고이며 독립 리뷰나 컨트롤러 실행 증거가 아니다. 명령과 stdout/stderr 원본은 실행기가 외부 JSONL로 수집한다. 모델은 scripts/run_logged.py를 사용하거나 관리 로그를 수정하지 않는다.

실행: 자체 CLI 평가 10/10, 이미지 8/8, mask TP340/FP0/FN0, LOT43/43, 경로61/61, 정책18/18, 티켓 정책8/8, 승인 전 호출0·중복0을 관측했다. 추가 unittest는 HTTP·격리·변경 초안 거절·복구·그래프/정책 경계를 검사한다. 격리 임시 복사본에서 `>32`를 `>100`으로 변경해 평가 실패 탐지를 확인한다. 테스트는 스레드 집합의 실행 전후 일치도 확인한다.

필수 acceptance 명령은 TestClient 진입에서 대기하여 완료하지 못했다. 별도 12초 진단에서 같은 대기를 재현했고 asyncio 내부 소켓 접근은 PermissionError(EPERM)를 반환했다. 추가 ASGI 테스트의 1MiB 초과 업로드도 threadpool 대기 때문에 완료하지 못해 해당 HTTP 검증은 미검증이다. Service 수준 413 우선은 검사한다. 제한시간 프로세스 종료는 HTTP timeout 응답이나 앱 작업 취소 검증과 다르다. 이 제한을 앱이나 보호 테스트 변경으로 우회하지 않았다.

별도 OS·새 세션 문서만의 인계·실제 서버/네트워크 curl·설치 재현·HTTP timeout 응답·작업 취소는 미실행이다. 최종 인계 시점은 구현과 가능한 검증 종료 후 최종 응답이다. 보호 HANDOFF.md는 갱신하지 않는다. 다음 단계는 컨트롤러가 TestClient가 동작하는 허용 환경에서 등록된 acceptance 명령과 독립 리뷰를 수행하는 것이다. 이 README의 예시 서버는 이번 작업에서 시작하지 않았다.
