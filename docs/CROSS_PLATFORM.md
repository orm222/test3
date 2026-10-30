# 프로젝트에 연결한 뒤 할 일

키트 폴더에서 실행하는 `setup_env.py attach --project ...`는 공통 실행 도구와 빈 설정을 준비합니다. 실제 프로젝트의 요구사항과 검사까지 대신 정의하지는 않습니다. 이후 이 문서의 `run_harness.py` 명령은 연결한 프로젝트 폴더에서 실행합니다.

## 자동 진행 기록과 점수 사용

`attach`는 대상 프로젝트의 `docs/progress.md`와 `docs/review-score.md`를 초기화합니다. 첫 상태는 미착수·미검증이며 점수는 미산정입니다. 과거 작업이나 실제로 받지 않은 담당자 승인을 채우지 않습니다. 이미 같은 이름의 사용자 문서가 있으면 덮어쓰지 않고 중단합니다. 기존 문서를 별도 경로에 보관한 뒤 연결하세요. 이미 이 하네스가 관리하는 문서는 재연결해도 이력이 유지됩니다.

일반적인 사용 명령은 그대로입니다. `loop`가 구현·수정·검사·독립 리뷰를 수행하면서 두 문서를 갱신합니다. `verify`는 실제 검사 결과를 진행 기록에 남기고 기존 점수의 유효성을 확인합니다. `status`는 현재 코드와 기록의 유효성을 다시 확인하며 새 모델 리뷰를 수행하지 않습니다. 기존 `review` 명령도 같은 기준과 점수 기록을 사용하지만 기존의 1회 시도·시간 예약 규칙을 유지합니다.

진행 기록에는 목표·실제 경계 답변·확인 방법, 현재 상태, 구현자 메모, 실행한 명령과 종료 코드, 원본 로그 위치, 실패·미실행, 다음 작업을 남깁니다. 구현자가 작성한 판단 이유는 자기보고로 표시하며, 하네스가 실행한 검증 결과와 구분합니다. 실제 변경 파일이나 판단 이유를 알 수 없으면 꾸며 쓰지 않습니다. 기존 `HANDOFF.md`가 있으면 관련 내용을 참조하고 사용자 파일을 덮어쓰지 않습니다. 인계 시점은 작업 전에 정하며, 새 세션이 인계 문서만으로 다음 행동을 파악했는지는 별도의 실제 확인이 있어야 충족입니다.

기록은 `.harness/work/reporting/`의 이력에 기반한 자동 생성물입니다. MD만 고치거나 기록 이력을 삭제해 통과·점수를 바꾸지 마세요. 원본 기록과 두 문서가 어긋나면 실행을 중단합니다. `docs/` 전체를 변경 감지에서 제외하지 않으며, 관리하는 두 MD에만 좁은 예외를 적용합니다. 일반 문서·소스·명세·테스트 변경은 계속 감지합니다. 기록과 리뷰 점수를 원본 요구사항이나 고정 정답 대신 사용하지 않습니다.

코드·명세·설정·공통 기준이 바뀌면 기존 리뷰는 현재 소스의 리뷰가 아닙니다. 다음 작업·검증·상태 확인 시 오래된 결과임을 표시하고 과거 이력을 보존합니다. 항상 켜져 있는 파일 감시기는 없으며, 하네스 밖의 수동 수정은 다음 실행에서 감지합니다.

### 공통 판정 기준

`.harness/review_criteria.json`의 같은 기준을 Sol 구현 전에 전달하고 Terra 및 기존 독립 리뷰에서도 사용합니다. 사용자에게 퀴즈를 내거나 특정 대회의 인터뷰 절차를 강제하지 않습니다. 다음 확인을 모두 기준에 포함합니다.

- 목표·범위·제약·경계 질문의 실제 답변과 요구사항 → 코드 → 검증의 연결.
- 잘못된 값·순서·누락·경계 입력을 테스트가 잡는지, 격리된 복사본에 실제 오류를 넣었을 때 실패하는지 직접 실행. 원본 보호 검사·정답을 약화하지 않으며 오류 주입의 예상 실패는 정상 검사의 실패와 구분.
- 통과·실패·스킵·미실행의 구분. 모듈 검사만 수행한 경우 전체 흐름을 검증했다고 표현하지 않음.
- 업로드·폴링·재시도·폴백 경로의 동작과 timeout 발생 시 결과 반환 / 실제 작업 중단 / 프로그램 종료의 구분.
- 비밀정보 노출·스레드 및 자손 프로세스 누수·개인 컴퓨터 경로 의존성 확인. 비밀값은 보고서에 복사하지 않고 위치와 상태만 기록.
- 문서의 설명·수치와 코드·로그 대조, 데이터·환경·모델·옵션·반복 횟수 등 측정 조건 및 원시 결과. 처리 완료·형식 준수·정답률·기본값 사용을 구분하고 한 사례의 개선을 전체 성능 개선으로 확대하지 않음.
- 미검증·실패·남은 한계의 기록. 실제 담당자 수용과 사람의 이해도를 AI가 대신 확정하지 않음.
- 인계 시점의 사전 계획과 실제 갱신, 새 세션의 문서만을 이용한 인계 확인, 다른 지원 환경에서 README대로 실행한 증거.

### 점수와 완료 판정

리뷰 모델은 기준 ID별 판정·이유·증거 참조를 반환하고, 하네스가 점수를 계산합니다. 숫자 총점만 주장하는 응답은 받지 않습니다. 공통 기준을 빠짐없이 한 번씩 평가해야 하며 중복·누락·알 수 없는 증거는 유효한 점수로 인정하지 않습니다.

`PASS=2`, `PARTIAL=1`, `FAIL/UNVERIFIED=0`입니다. 각 축은 `획득점 / (2 × 적용 항목 수) × 100`으로 계산합니다. 축은 의도·인지·기술·정직성·소통이며 각 100점 기준입니다. 적용 항목이 없는 축은 미산정입니다. 리뷰를 실행하지 않았거나 중단·응답 오류가 발생한 경우 새 점수를 만들어내지 않습니다. 작업별 리뷰와 최종 전체 리뷰의 대상·실행 ID·코드 상태를 구분합니다.

해당 없음(`NA`)은 작업 시작 전에 `automation/codex-loop/config.json`의 `review_not_applicable`에 실제 기준 ID와 사유를 지정한 항목만 허용합니다. 기본값은 빈 객체입니다. 정확한 기준 ID와 문구는 공통 기준 파일에서 확인하세요. 단순히 검증하지 않았다는 이유로 해당 없음 처리하지 않습니다. 등록한 사유와 기준은 실행 중 모델이 바꿀 수 없습니다.

실행이 필요한 기준에 충족·부분충족을 주려면 실제로 끝난 명령과 로그 증거가 있어야 합니다. 파일 존재만으로 실행했다고 인정하지 않습니다. 다만 특정 명령이 기준을 충분히 검사하는지에 대한 의미적 판단은 독립 리뷰의 책임이며, 명령 하나가 통과했다고 관련 없는 기준까지 충족으로 확대하지 않습니다. 새 세션이나 다른 환경에서 확인하지 않았다면 미확인으로 남겨야 합니다.

점수는 내부 참고 평가이며 공식 대회 배점이 아닙니다. 최저 점수로 작업을 자동 통과시키는 기능은 없습니다. 기존 필수 테스트·실행 증거·요구사항 리뷰의 `PASS/FAIL/UNVERIFIED` 기준을 유지하므로 높은 점수도 필수 검사의 실패나 미검증을 덮을 수 없습니다.

## 파일 배치와 역할

```text
대상 프로젝트/
├─ FUNCTIONAL_SPEC.md                # 사용자가 준비할 앱 명세
├─ BOUNDARY_QUESTION_TEMPLATE.md     # T01~T15 공통 경계 질문 규칙
├─ BOUNDARY_ANSWERS.md               # clarify가 생성한 실제 질문/답변 기록
├─ run_harness.py                    # 프로젝트의 공통 진입점
├─ docs/progress.md                  # 실행기가 작성하는 진행·수정·검증 기록
├─ docs/review-score.md              # 실행기가 작성하는 독립 리뷰 점수
├─ requirements-harness.txt
├─ .venv/                           # 이 컴퓨터에서 생성한 Python 3.9 환경
├─ .harness/
│  ├─ checks.json                    # 최종 검증 명령
│  ├─ acceptance.json                # 두 실행기가 공유하는 검사·보호 계약
│  ├─ boundary.json                  # 선택한 명세의 분석·실제 답변 상태
│  ├─ clarify.py                     # 읽기 전용 질문 분석과 답변 기록
│  ├─ check_unittest.py              # 일반 unittest용 JSON 검사기
│  ├─ reports.py                     # 진행 기록·점수 계산·기록 무결성
│  ├─ review_criteria.json            # 개발자와 리뷰어의 공통 판정 기준
│  ├─ harness.py
│  └─ work/                          # 최종 검증 기록
└─ automation/codex-loop/
   ├─ WORKFLOW_SPEC.md               # 첨부한 워크플로 기술 명세
   ├─ run.py
   ├─ config.json                    # 이 프로젝트의 실행 설정
   ├─ tasks.json                     # 작업과 요구사항별 검사
   ├─ review.schema.json
   └─ prompts/
      ├─ sol.md
      └─ terra.md
```

`WORKFLOW_SPEC.md`는 구현/리뷰를 운영하는 방식을 설명합니다. `config.json`의 `spec`는 **이번에 만들 앱의 명세**를 가리킵니다. 워크플로 MD를 폴더에 두는 것만으로 그 문장이 자동 실행되는 것은 아닙니다. 실제 동작은 실행 코드, 프롬프트와 설정으로 결정됩니다.

## 명세 준비와 경계 질문

앱 명세를 작성하고 `automation/codex-loop/config.json`의 `spec`를 그 파일의 프로젝트 상대 경로로 설정합니다. 기본값은 `FUNCTIONAL_SPEC.md`이며 `attach --spec docs/MY_SPEC.md`로 연결할 때 바꿀 수 있습니다. 모델 분석에 사용할 `builder_model`에는 계정에서 접근할 수 있는 정확한 Sol 모델 ID를 입력합니다.

이후 명령은 대상 프로젝트 폴더에서 실행합니다. Windows에서는 `python3.9` 대신 `py -3.9`를 사용합니다.

```bash
python3.9 run_harness.py clarify
```

기본 `clarify`는 실제 모델을 읽기 전용으로 호출합니다. 선택한 명세와 `BOUNDARY_QUESTION_TEMPLATE.md`의 T01~T15 중 관련 규칙을 사용하여 구현 결과가 달라지는 누락·모호·상충 조건을 한 번에 1~3개 질문합니다. 명확한 명세는 질문 없이 준비 상태가 될 수 있습니다. 원문의 용어를 사용하고 출처 페이지·절·행 위치는 질문에 출력하지 않습니다. 프로젝트의 기존 `AGENTS.md`는 그대로 독립적으로 유지됩니다.

질문이 나오면 실제 담당자 답변을 UTF-8 일반 텍스트 파일에 저장합니다. 다음은 실제로 출력된 질문 ID가 `Q-001`일 때의 예시입니다. 질문 ID와 답변 파일을 자신의 실제 결과에 맞추세요.

```bash
python3.9 run_harness.py clarify --answer Q-001 --answer-file answer.txt
python3.9 run_harness.py clarify
python3.9 run_harness.py clarify --status
python3.9 run_harness.py clarify --check-ready
```

여러 질문은 각각 답변을 기록한 뒤 재분석합니다. 답변을 제출하면 `needs_analysis`가 되며, 모호한 답변은 T12 후속 질문으로 다시 확인합니다. 모든 기존 질문에 실제 답변이 있고 마지막 분석에 미해결 조건과 새 질문이 없어야 `ready`가 됩니다. 질문을 기다리거나 준비되지 않은 `clarify` 분석·상태 확인은 종료 코드 2를 반환합니다. `--answer`, `--status`, `--check-ready`는 모델을 호출하지 않습니다. 답변 파일은 기록을 위한 입력이며 이후 실행에서는 상태에 저장된 실제 답변을 사용합니다.

선택한 명세, 질문, 답변과 분석 상태는 `.harness/boundary.json`이 관리하고 읽을 수 있는 기록은 `BOUNDARY_ANSWERS.md`로 생성합니다. 직접 파일을 고쳐 `ready`를 만들지 마세요. 실제 답변을 정정할 때는 같은 질문 ID로 `--answer`를 다시 실행하고 재분석합니다. 명세·질문 템플릿·참조 문서가 바뀌어도 기존 준비 상태는 유효하지 않습니다.

`clarify --spec docs/MY_SPEC.md`는 해당 호출의 명세를 선택하며 loop 설정을 자동 변경하지 않습니다. 이후에도 같은 명세를 쓰도록 `config.json`의 `spec`를 맞추세요. 다른 설정 파일을 사용하면 `clarify --config 경로`와 `loop --config 경로`에 같은 파일을 지정합니다. 분석 모델만 지정하려면 `clarify --model 실제모델ID`를 사용할 수 있습니다.

## 공통 통과 기준 등록

`unittest discover`는 테스트 0개나 전부 skip이어도 정상 종료할 수 있습니다. 명령 종료 코드나 성공 정규식만으로 프로젝트 완료를 판단하지 않습니다. `.harness/acceptance.json`에 공통 검사기, 고정된 필수 테스트 ID 목록, 보호할 검사·기준 자료를 등록합니다. 등록하지 않은 초기 상태에서는 최종 검증과 작업 준비 점검이 실패합니다.

다음은 `tests/acceptance/test_requirements.py`의 `AcceptanceTests.test_primary_requirement`를 필수 검사로 정한 예시입니다. 실제 요구사항을 검증하는 테스트를 먼저 준비하고 이름을 맞추세요. 예시 이름 자체가 업무 요구사항을 대신하지 않습니다.

`acceptance/required_tests.json`:

```json
{
  "version": 1,
  "required_test_ids": ["test_requirements.AcceptanceTests.test_primary_requirement"]
}
```

`.harness/acceptance.json`:

```json
{
  "version": 1,
  "checker_argv": ["{python}", "-B", ".harness/check_unittest.py", "--start-directory", "tests/acceptance"],
  "required_ids_manifest": "acceptance/required_tests.json",
  "protected_paths": ["tests/acceptance"],
  "boundary_tests": {
    "Q-001": ["test_requirements.AcceptanceTests.test_primary_requirement"]
  }
}
```

위 `boundary_tests`는 실제 답변이 있는 `Q-001`을 그 답변 내용까지 검사하는 필수 테스트에 연결한 예시입니다. 자신의 실제 질문 ID와 검사 ID로 바꿉니다. 실제 답변이 있는 모든 질문을 하나 이상의 `required_test_ids`에 연결해야 하며 알 수 없는 질문 ID는 넣을 수 없습니다. 질문 없이 준비가 완료된 명세는 `boundary_tests`를 `{}`로 두거나 생략할 수 있습니다. 매핑이 있다는 사실만으로 검사가 충분해지지는 않습니다. 각 검사는 답변이 결정한 허용·금지 동작과 경계를 실제로 확인해야 합니다.

필수 ID 목록은 요구사항과 함께 검토해 확정하며, 검사할 때마다 현재 수집 결과에서 자동으로 다시 만들지 않습니다. 필수 ID 누락·이름 변경·skip·예상된 실패도 통과가 아닙니다. 기본 검사기는 skip을 허용하지 않습니다. 운영체제별 선택 검사가 필요한 프로젝트는 별도 검사기에서 적용 조건을 명시적으로 확인하고, 필수 업무 검사를 skip으로 대체하지 마세요.

수량을 유지하려고 다른 테스트를 추가해도 빠진 필수 검사를 대신하지 못합니다. 추가 테스트는 허용하며 실행한 추가 검사도 모두 통과해야 합니다. 개발용 테스트는 보호된 인수테스트와 다른 폴더에 둡니다.

## 최종 검증 등록

`.harness/checks.json`과 반복 작업의 검사에 동일한 공통 명령을 등록합니다.

```json
{
  "inputs": ["FUNCTIONAL_SPEC.md"],
  "checks": [
    {
      "name": "acceptance",
      "command": ["{python}", "-B", ".harness/check_unittest.py", "--start-directory", "tests/acceptance"],
      "timeout_seconds": 120
    }
  ]
}
```

`{python}`은 대상 프로젝트의 Python 3.9 실행 파일로 바뀝니다. 실제 요구사항을 검증하는 테스트를 준비하고 등록하세요. 실행할 검사가 없는 초기 상태는 성공으로 처리하지 않습니다. 프로젝트의 별도 라이브러리가 필요하면 `.venv`의 Python으로 설치해야 합니다. 최종 검증은 계약의 공통 검사기를 반드시 포함하며 같은 명령은 중복 실행하지 않습니다.

## Sol/Terra 작업 등록

`automation/codex-loop/tasks.json`은 작업 배열입니다. 아래 구조에서 목표·요구사항·검사를 자신의 프로젝트에 맞게 채웁니다.

```json
[
  {
    "id": "T001",
    "goal": "앱 명세의 해당 기능을 구현한다. 실제 목표로 바꿀 것.",
    "requirements": [
      {
        "id": "R001",
        "text": "구체적인 입력과 예상 결과를 여기에 적는다.",
        "check_ids": ["acceptance"]
      }
    ],
    "checks": [
      {
        "id": "acceptance",
        "argv": ["{python}", "-B", ".harness/check_unittest.py", "--start-directory", "tests/acceptance"],
        "success_regex": "\"status\"\\s*:\\s*\"passed\""
      }
    ]
  }
]
```

각 요구사항의 `check_ids`는 같은 작업에 정의한 검사 ID와 연결합니다. 각 검사에는 명령 인수 배열 `argv`와 성공 출력을 확인할 `success_regex`가 필요합니다. 공통 검사는 정규식 외에도 JSON 결과의 필수 ID와 실제 통과 상태를 확인합니다. `acceptance`라는 이름으로 다른 명령을 등록하면 거부합니다.

작업이 여러 개라면 작업별 검사는 해당 기능 범위로 정할 수 있습니다. loop는 최종 통합 검사·리뷰에 공통 검사기를 포함하고, 최종 소스에서 그 검사가 통과해야 완료합니다. 단일 작업에서 같은 공통 검사와 리뷰가 이미 통과했다면 불필요하게 중복 실행하지 않습니다.

Windows에서 인수 자체에 큰따옴표 문자(`"`)가 들어가는 검사는 PowerShell 버전별 전달 차이 때문에 거부합니다. 이런 로직은 검사 스크립트 파일 안에 넣고 `argv`에는 파일 경로와 단순 인수를 지정하세요. 공백·한글이 있는 파일 경로는 사용할 수 있습니다.

`.harness/checks.json`과 `tasks.json`은 형식과 실행 시점이 다릅니다. 같은 테스트 스크립트를 재사용할 수 있지만, 반복 작업 자체를 최종 검사의 한 항목으로 등록하지 마세요. 반복 작업 안에서 `run_harness.py verify`를 호출하는 구성도 피하세요. 검증 기록 생성이 소스 변경 감지와 충돌할 수 있습니다.

## 모델과 기록 위치 설정

`config.json`의 `builder_model`과 `reviewer_model`에 사용할 수 있는 정확한 모델 ID를 입력합니다. 기본 `SET_ACTUAL_...` 값은 자리표시자입니다. Sol/Terra라는 역할명만으로 접근 가능한 모델을 추정하지 않습니다.

고정 인수테스트, 독립 정답·픽스처, 별도 평가기를 공통 계약의 `protected_paths`에 등록합니다. 계약 파일·필수 ID 목록·검사기 자체와 선택한 명세·질문 템플릿·참조 문서·질문/답변 상태도 자동 보호합니다. `config.json`의 `protected_paths`에는 추가 보호 대상을 등록할 수 있습니다. 경로는 작업 시작 전에 실제로 존재해야 하며 빈 폴더는 보호 대상이 아닙니다. 보호된 파일의 변경·삭제는 loop를 중단시킵니다. 앱 구현과 별도 개발용 테스트는 계속 수정할 수 있습니다. 보호 기능은 변경을 탐지하며 파일을 자동 복원하지 않습니다.

`project`는 작업 대상, `spec`는 대상 프로젝트 기준 명세 경로, `tasks_file`은 설정 파일 기준 작업 파일 경로입니다. `run_root`는 반복 작업 로그를 저장할 위치이며 **프로젝트와 실행기 폴더 바깥**이어야 합니다. 연결 시 생성한 설정은 프로젝트 옆 `.codex-loop-runs` 아래를 사용합니다. 다른 컴퓨터로 옮기면 그 컴퓨터의 가상환경과 경로를 다시 준비하세요.

## 프로젝트 전용 검사기와 로그

업무 지표나 데이터 무결성을 함께 검사해야 한다면 `checker_argv`를 프로젝트의 Python 검사기로 바꿉니다. 검사기는 필수 ID의 실제 실행·통과와 업무 결과를 직접 확인하고 불완전하면 비정상 종료해야 합니다. stdout에는 다음 형태의 JSON 객체 하나만, unittest 원문과 평가 로그는 stderr에 출력합니다.

```json
{
  "schema": "project-acceptance/v1",
  "status": "passed",
  "test_results": [
    {"id": "test_requirements.AcceptanceTests.test_primary_requirement", "status": "passed"}
  ]
}
```

출력만 꾸며내는 검사기는 검증 근거가 아닙니다. 실행기가 보호하는 검사기에서 실제 결과를 수집해야 합니다. 두 컨트롤러는 종료 코드와 이 결과를 함께 검사합니다. 업무별 추가 JSON 필드는 허용합니다.

검사기는 프로젝트 안에 로그·결과 파일을 만들지 않게 합니다. loop는 stdout/stderr와 실행 증거를 외부 `run_root`에 저장하고, verify는 기존 `.harness/work`에 저장합니다. Terra는 읽기 전용으로 검사하므로 외부 폴더라도 직접 파일을 쓸 수 있다고 가정하지 않습니다. `work/` 전체를 변경 감지에서 제외하거나 감지를 끄는 방법을 사용하지 마세요. 실제 코드가 검사 중 바뀌면 계속 중단해야 합니다.

## 실행 순서

선택한 명세 준비 → `clarify` 질문 → 실제 답변 기록 → 재분석 → `--check-ready` → 답변별 필수 테스트 등록까지 마친 뒤 대상 프로젝트의 터미널에서 진행합니다. Windows에서는 `python3.9` 대신 `py -3.9`를 사용하세요.

```bash
python3.9 run_harness.py loop --preflight
python3.9 run_harness.py loop
```

첫 명령은 질문/답변의 준비 상태·질문별 필수 테스트 매핑·공통 검사 계약·필수 ID 목록·보호 경로의 존재와 설정 및 CLI/로그인 상태를 점검합니다. 등록 검사·실제 모델 작업·tmux 실행은 하지 않습니다. `clarify`, `loop --help`와 `loop --probe-models`도 tmux 없이 실행합니다. 다만 `clarify` 분석과 `--probe-models`는 실제 모델을 호출하며, 후자는 일반 준비 확인에는 붙이지 않습니다.

실제 `loop` 명령은 운영체제를 자동 감지합니다. Windows는 현재 터미널에서, Linux / Codespaces는 tmux에서 실행합니다. 이미 tmux 안이면 현재 패널을 사용합니다. Linux에 tmux가 없으면 설치 안내와 함께 중단합니다. 별도 OS 선택 명령은 없습니다.

`loop`에서는 실제 질문/답변과 `boundary_tests` 매핑을 Sol과 Terra에 함께 전달합니다. Sol이 구현하고 등록 검사를 실행한 뒤 Terra가 답변의 의미와 검사 결과를 독립적으로 확인합니다. 실패 시 설정된 `max_repairs` 범위에서 수정합니다. 작업 결과는 출력되는 실행 기록 경로에서 확인합니다. 미해결 질문이나 매핑 누락은 실제 loop 시작 전에 거부되며 Linux에서는 준비 상태 확인 후 tmux를 시작합니다.

새 tmux 세션은 작업 결과를 보여준 뒤 Enter를 누를 때까지 유지됩니다. 작업 중 `Ctrl+B` 다음 `D`를 누르면 화면에서 분리하고 작업은 계속 실행합니다. 다시 보려면 출력된 재접속 명령을 사용하세요. 대화형 터미널이 없는 실행은 세션 이름과 재접속 명령을 출력한 뒤 반환합니다. **세션 생성의 성공과 작업 완료는 다릅니다.** 재접속하거나 실행 기록의 상태와 로그를 확인한 뒤 다음 단계로 진행하세요.

작업 완료 후 기존 하네스의 `verify`와 `status`로 현재 파일에 대한 검증을 남깁니다.

```bash
python3.9 run_harness.py verify
python3.9 run_harness.py status
```

`verify`도 같은 경계 질문 준비 상태와 답변별 필수 테스트 매핑을 요구합니다. 기존 `review` 명령은 준비가 완료된 실제 질문/답변을 전달하는 별도의 1회 독립 리뷰입니다. 반복 Terra 리뷰는 `loop`가 관리합니다. 두 모드는 응답 형식과 반복 정책이 다르므로 리뷰 보호 파일을 삭제해 합치지 마세요.

## Windows와 Linux를 오갈 때

선택형 API 검수는 키트의 `docs/OPTIONAL_API_REVIEW.md`를 참고하세요. Schemathesis 4.4.4와 Hurl 8.1.0은 별도 도구 환경에 설치하고, 필요한 프로젝트의 최종 `verify`에만 연결합니다. 개발 loop에는 자동 추가하지 않습니다. 결과는 진행 기록과 이후 리뷰의 근거로 연결하며 기존 리뷰 점수를 자동 가산하지 않습니다. OS를 바꾸면 해당 OS에서 도구를 설치하고 `enable`로 경로를 다시 연결해야 합니다.

`.venv`는 복사해 재사용하지 말고 각 컴퓨터의 Python 3.9로 만드세요. Windows PowerShell에서는 현재 터미널을 사용하며 tmux가 필요 없습니다. Linux / Codespaces에서는 같은 Python 진입점이 실제 작업을 tmux에서 실행하므로 tmux를 준비해야 합니다.

Debian / Ubuntu 기반 Linux에서 tmux가 없으면 사용자가 `sudo apt update`와 `sudo apt install tmux`를 실행해 준비할 수 있습니다. 다른 배포판은 해당 패키지 관리자를 사용하세요. 키트의 `install`, `doctor`, `attach`는 tmux 세션을 시작하지 않습니다. Linux 진단은 tmux가 없으면 준비 미완료로 표시하며, 키트가 OS 패키지를 자동 설치하지 않습니다.

도구가 없으면 Python 3.9와 Git을 먼저 준비하고, Codex CLI는 [OpenAI 공식 설치 안내](https://learn.chatgpt.com/docs/codex/cli)를 따르세요. `codex login`으로 로그인한 후 `codex login status`로 상태를 확인합니다. Codespaces의 브라우저 연결이 어려우면 계정에서 허용하는 경우 `codex login --device-auth`를 사용할 수 있습니다. [OpenAI 공식 인증 안내](https://learn.chatgpt.com/docs/auth)

설치·진단에서는 실제 모델을 호출하지 않습니다. 모델 접근 권한과 실제 작업 성공 여부는 별도로 확인해야 합니다. Codespaces 안내는 Linux 분기를 설명하며 실제 Codespaces에서의 실행 검증을 뜻하지 않습니다. 배포본 검증 범위는 키트 루트의 `VALIDATION.txt`를 확인하세요.
