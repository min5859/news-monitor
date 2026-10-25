# 수집원 교체 (계획: ~/.claude/plans/structured-snacking-ladybug.md)

## 남은 작업 (여기만 보면 됨)
세션 예약은 Claude 창을 닫으면 사라진다. 그때는 "오늘 동작 확인해 줘"로 같은 확인을 요청한다.

- [x] **10/25 13:24 KST** Cursor 기록 정리 첫 정기 실행 확인: 성공·오류 0, 삭제 0건(가장 오래된 기록 9/25 22:39 KST라 30일 미만), 보존 2,405개 → wiki `6effd9d`
  - 11/1 13:17 KST 실행에서 약 231개·24MB가 처음 삭제될 예정. 그 주 헬스체크 때 journal의 errors 0만 보면 됨
- [x] 하루 3번 갱신(07·12·18시, `d211a45`) 설치. 설치 직후 따라잡기 실행(16:40)에서 브리핑 날짜 분할(`d25f305`)·
  '오늘의 부정 기사'(`d89532a`)·제목 꼬리표 정리(`e077a7c`) 첫 적용 확인: 성공·published, 브리핑 ok, 꼬리표 0, Korea Times 1건, Cursor 14회
- [x] **10/25 18:23 KST** 첫 18:00 정기 회차 확인: 성공·published(`b58b4bf`), 수집 ok 22/22, 신규 0·재확인 20, Cursor 0회(캐시), 브리핑 ok, 부제 "18:00 KST 수집 기준"
- [ ] **10/26 07:23 KST** 정기 점검 (세션 예약 95a17172). 헬스체크 ok·fallback 없음 + 아래 Tavily 사용량
- [ ] **Tavily 키 사용량 확인** (10/26 07:23 점검 때 함께)
  - 사실: 16:46에 이 키의 사용량이 208(우리 몫 약 33, extract 16은 우리 코드가 쓰지 않음)이었고, 18:23에는 0으로 초기화돼 있었다(월 주기 초기화로 보임)
  - 확인: 07:00 회차 뒤 사용량이 5면 우리만 쓰는 것(18:00 회차 몫은 초기화 전이었음). 5보다 크면 다른 곳에서도 쓰는 것
  - 다른 곳에서 쓰면 영향: 하루 3번이면 우리 몫만 월 약 465라 합치면 1,000을 넘을 수 있고, 넘으면 Tavily 검색이 막혀(`http_432`) 사이트맵·RSS 기사만 남음 → 사용자에게 전용 키 발급 요청
- [ ] **11/8 무렵** 예전 수집 방식 코드(`codex_web_search`·GDELT) 정리
  - 조건: 10/25~11/8 정기 실행이 계속 정상(수집 실패·게시 실패 없음)
  - 영향: 지우면 예전 방식으로 되돌리는 경로가 없어짐. 그 전까지는 설정만 바꿔 되돌릴 수 있음
  - 할 일: 실패 테스트·문서·AGENTS.md 롤백 문구 함께 정리 → Mac 테스트 → 커밋·push → OCI 반영
- [ ] (낮은 우선순위, 제 판단) 영문 제목의 이름 치환이 "Hyosung Chairman 회장님"처럼 어색함. 공개 표시 규칙과 함께 정리
- [ ] (언제든) 피드백 초안 알림: Claude Code 알림에서 0 누르면 삭제. 프로젝트 영향 없음


## 단계 0: 준비
- [x] (사용자) Tavily 키 발급 → OCI `/srv/news-monitor/config/tavily.json` (wiki-publisher 0600)
- [x] Tavily 약관: 개인 비영리·제목/링크/자체 통계만 게시, Tavily 생성물(content/answer/score) 미저장으로 진행

## 단계 1: 매체 중복 수정
- [x] 실패 테스트: 서브도메인 동일 기사 1건·매체 1곳, `.co.kr` 3라벨, 구형 publisher 병합·최초 확인일 보존, 미정규화 publisher preflight 거부
- [x] `publisher_domain` 추가·적용 (collect / report / preflight / discovery)
- [x] Mac 3.12·3.14 146개·Node 7개·ruff 통과 → 커밋 `bdddc52`
- [x] OCI 읽기 전용 시뮬레이션: 14행 → 13기사, 13호스트 → 10매체
- [x] push·OCI ff-only 반영 (`bdddc52`), OCI 146개·Node 7개 통과
- [x] OCI 읽기 전용 호환 확인: 기존 articles.json 14행 정규화 읽기, report.json stale 표시 호환, 병합 13기사·10매체
- [x] 10/25 07시 정기 실행 결과 확인·docs 기록 (`a881395`)

## 단계 2: 실측 (운영 상태 미변경)
- [x] probe CLI `news_monitor.sources` (`1ac51cc`): Tavily / 언론사 사이트맵·RSS (robots 확인, 메타데이터만)
- [x] OCI 1회 실측(3크레딧, 운영 해시 불변) → `docs/SOURCE-EVALUATION.md`
- [x] 수집원 결정: Tavily + 사이트맵·RSS (근거: 겹침 적고 상호 보완. 직접 결정 — lessons.md 참고)

## 단계 3: 전환 구현
- [x] provider `tavily_feeds`·설정 검증·수집 모듈·날짜 근거·이전 Codex 행 유지 (`e8e7392`)
- [x] 날짜만 다른 같은 기사 중복 제거 (`896d2b1`)
- [x] Mac 154개·Node 7개·ruff, 변이 검사 4개 검출, OCI 반영·테스트
- [x] OCI 쓰기 없는 실측 2회: 22/22 수집원 성공, 병합 30건·23매체·중복 0
- [x] AGENTS.md 수집원 문구 갱신 (`c99960b`, 사용자 스테이징분과 함께 커밋)
- [x] 10/25 07:00 KST 첫 정기 실행 검증 기록: 성공·published, 22/22 수집원, 25건·19매체 (`a881395`)
- [x] 브리핑 fallback(`group_date_span_exceeded`) 수정: 7일 넘는 모델 묶음을 날짜로 분할 (`d25f305`), OCI 실입력 4/4 ok
- [x] 브리핑 ok 확인: 10/25 16:40 KST 실행(하루 3번 timer 설치 직후)에서 17묶음·fallback 없음

## 헬스체크 스킬
- [x] news-monitor 점검 추가 (Claude·Mac Codex·OCI ubuntu Codex 3곳 동일, 테스트 24개)
- [x] OCI 실데이터: 10/24 failed(이유·preflight 실패 항목 표시), 10/25 pending

## AI 전환: Codex → Cursor claude-sonnet-5-5-medium
- [x] AppArmor가 Cursor 샌드박스 거부 확인 → 다른 게시기와 같이 `--sandbox disabled` + ask·빈 workspace·도구 거부
- [x] 서비스 ReadWritePaths에 `~/.cursor`만 추가, 유닛 설치·백업 (`1a2a434`)
- [x] 브리핑도 provider 선택 → Cursor, 호출 상한 60
- [x] 운영 사본 전체 실행: 분류 13회 실패 0, 브리핑 ok, 101초 (`docs/CURSOR-ANALYSIS.md`)

## Cursor 기록 정리 (사용자 승인 10/25)
- [x] 정리 스크립트: wiki-publisher `~/.cursor`에서 30일 넘게 안 바뀐 chats/*/*, projects/*/agent-transcripts/*, agent-tools/*, terminals/*만 삭제. 심볼릭 링크 미추적, 이름·내용 미출력, 건수·용량만 기록
- [x] 실패 테스트 먼저 → Mac 3.12/3.14·OCI 3.12 통과(6개), 안전장치 변이 4개 검출
- [x] OCI 시험 실행: 30일 기준 0건(보존 2,405), 7일 기준 1,429건·140MB
- [x] 원본은 wiki vault `assets/ops/cursor-history-cleanup/`, 설치는 root 소유 `/usr/local/libexec` + systemd service/timer(주 1회 일 13:17 KST, 게시기 시간과 겹치지 않음) — wiki `871d258`
- [x] 시험 항목 1개로 실제 service 실행: 그 1개만 삭제·오류 0·실기록 보존 → wiki 운영 노트 기록
- [x] 첫 정기 실행 10/25 13:17 KST 결과 확인: 성공·오류 0·삭제 0건(30일 넘은 기록 없음)·보존 2,405개 → wiki `6effd9d`

## 사용자 판단 필요 (자리 비운 동안 보류)
- [ ] 피드백 초안: Claude Code 알림에서 1(확인)·2(전송)·0(삭제) 선택
- [x] OCI wiki 기록 (사용자 요청): 뉴스 계획 페이지 `fd067b0`, Health Check 노트·스킬 원본 동기화 `e008548`
- [x] `~/.cursor/projects`의 10/18 `tmp-news-monitor-model-*` 3개 삭제 (내용은 작업 폴더 신뢰 표식 129바이트뿐)
- [x] Tavily 상한: 08:5x 재측정에서 검색어 5개 모두 7~10건(상한 20 미만) → 가끔 나는 신호라 검색어·크레딧 유지 (누적 28/월 1000)
- [x] 10/25 수동 재게시 안 함: 기사·통계는 이미 정상 게시, 이슈 묶음만 10/26 07시부터 반영 (판단 목록에서 제외)

- [x] Cursor 대화 기록 정리: 사용자 승인(10/25 "진행") → 아래 'Cursor 기록 정리' 절

## 제가 판단해 보류한 것 (근거)
- 검토 #3 정책 차단과 실패의 종료 코드 구분: 헬스체크가 이유와 preflight 실패 항목을 보여 주고, 새 수집원에서는 이전 기사만 남는 날이 드물어 보류
- 검토 #4 검증 로직 중복 정리(preflight와 validate_decision 등): 공개 게이트를 건드리는 위험이 이득보다 커서 보류
- 검토 #5 GDELT 필드 필수 검증·분류 캐시 크기 제한: 영향이 미미해서 보류
- 수집 `upstream_limit_reached` → `partial` 판정 유지: 결과가 잘렸을 수 있다는 사실 그대로의 신호라 숨기지 않음
- `codex_web_search`·GDELT 코드: 되돌리기용으로 유지. 11/8까지 정기 실행이 계속 정상이면 정리(제 판단, 사용자 판단 목록에서 제외)
- 10/25 수동 재게시 안 함: 정기 실행은 성공했고 fallback은 설계된 안전 표시. 수동 게시는 정기 실행 검증과 섞임

## Review

### 10/25 07:00 KST 첫 정기 실행
- 결과: 4개 파이프라인 모두 ok. news-monitor는 성공(exit 0), preflight 13개 통과, pushed → published(`ac555a3`)
- 수집: 22/22 수집원 성공·오류 0, 25건(신규 12·재확인 2·유지 11)·19매체, 상태 partial(`upstream_limit_reached`, 오류 아님)
- 분류: Cursor 25회·실패 0. 브리핑: 2회 모두 `group_date_span_exceeded` → fallback
- 원인: 모델이 같은 이슈를 13~21일 간격으로 묶음(재현 2/2), 프롬프트 보강으로도 3/3 실패
- 조치: 날짜 분할 + 분할 묶음 요약을 자체 제목으로(`d25f305`). 실패 테스트 → Mac 156개·Node 7·ruff → OCI 실입력 4/4 ok → OCI `a881395` 반영·테스트 156개 통과
- 운영 data·dist는 쓰지 않음, 시험 사본(`/tmp/news-monitor-trial`)·프롬프트 시험 파일 삭제
