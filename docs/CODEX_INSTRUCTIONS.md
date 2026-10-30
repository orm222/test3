# Codex 작업 지시 기록

## 사용자 요청 — 2026-10-31
PROBLEM.md는 문제 파일, FABQUALITY는 이미지 관련 자료, codex_computer_setup은 하네스다.
명령어를 MD 파일에 저장하고, 오류와 해결 방법은 다른 MD에 저장한다.
하네스에 부족한 설정이 있으면 보완한다.

## 이번 작업 범위
환경 점검, Python 3.9/tmux 준비, 하네스 연결, 모델 ID 설정, 기록 규칙 추가.
이 브라우저 작업에서 Codex 대화창에 구현 지시를 제출하지 않았다.

## 이후 사용할 고정 지시
AGENTS.md와 HANDOFF.md를 읽고 이어서 작업한다.
PROBLEM.md를 앱 명세로 사용한다.
모든 실행 명령과 결과를 docs/COMMANDS.md에 저장한다.
실패는 docs/ERRORS_AND_FIXES.md에 원인·해결·재검증까지 기록한다.
누락된 요구사항이나 실제 답변이 필요한 조건은 추측하지 않는다.

## 추가 사용자 요청
질문 답변을 받았다고 가정하고 모의 답변을 넣어 계속 진행하여 오류 위치를 확인한다. 모의 답변은 실제 담당자 답변과 구분한다.

## 진단 실행 권한 — 2026-10-31
사용자는 질문 답변을 받았다고 가정하고 계속 질문/답변하며 실행 오류 위치를 확인하라고 명시적으로 요청했다.
따라서 이번 진단 회차의 조건 선택은 에이전트에게 위임되었다.
이는 원래 출제자의 답변을 받았다는 뜻이 아니다. 진단 회차에 적용하는 테스트 조건으로 취급한다.
추가 질문도 이 진단 범위에서 합리적인 조건을 선택해 답변하고 출처를 보존한다.

## 인수테스트 준비 지시
원문은 BOOTSTRAP_TESTS_PROMPT.md에 보존. 자동 loop 전에 실제 검사와 작업을 등록한다.
# 인수테스트·계약 준비 요청 원문 (이번 진단 회차)

사용자가 하네스를 실제로 계속 실행해 오류 위치를 확인하도록 요청했다. 이 호출은 자동 loop 시작 전에 필요한 독립 인수테스트와 계약을 준비하는 단계다. AGENTS.md, PROBLEM.md, BOUNDARY_ANSWERS.md, docs/CROSS_PLATFORM.md를 읽어라. 실제 담당자 답변을 받았다고 주장하지 말고 현재 진단 회차에 위임된 적용조건을 따른다.

이 단계에서는 애플리케이션 구현을 만들지 말고 아래만 완성하라:
1. Python 3.9, 표준 unittest 기반 tests/acceptance/test_fabquality.py와 독립 fixture. Pillow/NumPy/FastAPI TestClient를 사용해 실제 PNG와 HTTP 경로 검증. 원문 10개 시나리오, >32 및 3/4픽셀, 60분 경계, 중복 LOT 경로, 정책 63/64/65와 정책 B, 승인 이전 0호출, 재시도/응답유실/영구실패, BLOCKED의 결과 보존, 413 우선순위까지 실질적으로 검증. 서비스 출력에서 기대값을 만들지 말고 직접 고정한 기대값/마스크를 사용. 추가 연습 data는 보조자료이며 원문 12쌍은 시드를 분리해 합성 fixture로 생성 가능하다. 데이터 생성과 서비스 판정은 분리.
2. 테스트할 앱의 공개 인터페이스를 docs/IMPLEMENTATION_CONTRACT.md로 정하라. fabquality.app.create_app(...) FastAPI factory를 정하고 테스트용 모드/정적 ontology fixture를 인자로 주입하도록 설계. HTTP 응답 필드명과 구조를 명확히 하라. 원문에 없는 구현 선택임을 표시.
3. 고정 필수 테스트 ID 목록 acceptance/required_tests.json, .harness/acceptance.json, .harness/checks.json, automation/codex-loop/tasks.json을 실제 생성한 인수테스트에 맞춰 작성. tests/acceptance, acceptance, 계약 문서를 보호 대상으로 지정. 모든 실제 답변 질문 Q-001~006을 의미에 맞는 고정 테스트에 boundary_tests로 연결. Q-001/Q-004는 참조 문서가 없어도 서비스가 실행되는 범위 검사로 연결 가능.
4. task는 하나의 통합 구현 작업으로 등록해 원문 요구사항을 모두 포함한다. 공통 검사기 .harness/check_unittest.py를 사용하고 테스트 0개/skip으로 통과시키지 않는다.
5. 원본 PROBLEM.md, 질문답변 상태, 하네스 소스/프롬프트/기준, 모델 설정은 바꾸지 않는다. 테스트를 앱이 없는데 성공시키지 말 것. 현재 앱 부재로 실패하는 red 결과를 실제 실행해 남긴다.
6. 필요한 의존성은 프로젝트 .venv에 설치하고 requirements-app.txt에 실제 버전을 고정한다. pytest 금지. 오류·해결과 명령은 요청된 MD에 기록하되 관리 문서 progress.md/review-score.md는 직접 수정하지 않는다.
7. HANDOFF.md에 테스트 준비 결과와 남은 앱 구현 작업을 업데이트한다. Git 커밋/푸시는 하지 않는다.
질문을 사용자에게 되묻지 말고 미정 구현 선택은 이번 진단용으로 명시하여 결정하라. 완료 후 생성 파일, 테스트 개수, 예상 red 실패 이유를 보고하라.

