# 수집원 교체 (계획: ~/.claude/plans/structured-snacking-ladybug.md)

## 남은 작업 (여기만 보면 됨)
세션 예약은 Claude 창을 닫으면 사라진다. 그때는 "오늘 동작 확인해 줘"로 같은 확인을 요청한다.

- [x] **10/25 13:24 KST** Cursor 기록 정리 첫 정기 실행 확인: 성공·오류 0, 삭제 0건(가장 오래된 기록 9/25 22:39 KST라 30일 미만), 보존 2,405개 → wiki `6effd9d`
  - 11/1 13:17 KST 실행에서 약 231개·24MB가 처음 삭제될 예정. 그 주 헬스체크 때 journal의 errors 0만 보면 됨
- [x] 하루 3번 갱신(07·12·18시, `d211a45`) 설치. 설치 직후 따라잡기 실행(16:40)에서 브리핑 날짜 분할(`d25f305`)·
  '오늘의 부정 기사'(`d89532a`)·제목 꼬리표 정리(`e077a7c`) 첫 적용 확인: 성공·published, 브리핑 ok, 꼬리표 0, Korea Times 1건, Cursor 14회
- [x] **10/25 18:23 KST** 첫 18:00 정기 회차 확인: 성공·published(`b58b4bf`), 수집 ok 22/22, 신규 0·재확인 20, Cursor 0회(캐시), 브리핑 ok, 부제 "18:00 KST 수집 기준"
- [x] **10/26 07:23 KST** 정기 점검: 게시는 성공·published(`3e7564f`), 브리핑 fallback. 원인은 Cursor 로그인 만료(아래 '사용자 판단 필요' 1번) → Review 참고
- [x] **Tavily 키 사용량**: 10/25 16:46 208 → 10/26 07:23 218. 18:00·07:00 회차 5씩만 늘고 다른 사용 없음. 18:23의 0은 API의 일시적인 값. 사용자 조치 불필요
- [x] Cursor 인증 오류를 `model_authentication_failed`로 분류(`657f488`). 이전에는 `model_cli_failed`로 남고 재시도함
- [x] **12:23 KST** 12:00 회차 확인: 로그인 복구 확인(새 기사 1건 Cursor 분류 성공). 게시 성공·published(`ae27445`).
  브리핑은 다른 원인(제목에 없는 숫자)으로 fallback → 수정 `f2821fc` → Review 참고
- [x] **18:23 KST** 18:00 회차 확인: 브리핑 ok(호출 1회·오류 0), 33건(신규 3), published(`ba646f7`). `f2821fc` 첫 적용 확인
- [x] **10/27** 당일 기사 안내 문구(`47144a7`): '오늘의 부정 기사'·당일 발행 요약·발행일별 표에 "대개 다음 날 수집에서 추가" 안내
- [x] **10/27** 최신 기사 확보 보강(`3d5b9ad`): robots 허용 사이트맵 20개 추가(총 37), 뉴데일리 `+00:00` 날짜 보정. Mac·OCI 피드 실측 37/37 → docs/SOURCE-EVALUATION.md
- [ ] **18:23 KST** 18:00 회차에서 위 두 가지 첫 적용 확인 (수집원 42/42, 안내 문구 게시)
- [ ] (제 판단, 낮은 우선순위) 헬스체크 보강. 이번에 07:23 점검이 원인을 바로 보여 주지 못했고 Research Wiki 1/2편을 ok로 판정함
  - 공유 계정 Cursor 로그인 상태(`agent status`), 뉴스 브리핑 오류 코드, Research Wiki·OSS Radar 선정 수 미달을 표시
  - 네 사본(Claude·Mac Codex·OCI Codex·vault)과 테스트를 함께 갱신
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
- [x] **1. (급함) OCI Cursor 다시 로그인** — 사용자가 10/26 07:56 KST에 다시 로그인. `agent status` 로그인됨·짧은 호출 성공 확인
  - 원인: 9/25 22:37 로그인이 30일 뒤 "stored authentication is invalid"로 무효화. 주기는 확정 못 함(문서에 유효기간 없음, ubuntu의 8/29 로그인은 아직 로그인 표시). 우리 정리 timer와 무관
  - 지금 영향: OSS Radar 05:00 실패, Research Wiki 1/2편만 게시, 뉴스 브리핑 fallback(기사·통계·부정 기사 칸은 정상 게시)
  - 그대로 두면: 뉴스는 새 기사가 나오는 회차부터 게시가 막혀 공개 페이지가 07:00본에 머묾. 내일 03:00 Dev Blog·04:00 Research Wiki·05:00 OSS Radar 모두 실패
  - 할 일(사용자): `ssh oci` 후 `sudo -u wiki-publisher -H env PATH=/home/wiki-publisher/.local/bin:/usr/bin:/bin agent login` → 나오는 주소를 브라우저에서 열어 승인 → 같은 방식으로 `agent status` 확인. 그 뒤는 제가 확인
- [ ] **2. 오늘 놓친 OSS Radar·Research Wiki 재실행 여부** — 로그인 후 수동으로 다시 돌릴지. 다른 프로젝트라 제가 임의로 돌리지 않음
- [ ] **4. (선택) 보존 폭이 짧은 피드를 매시간 확인** m-i.kr·asiatime·asiatoday는 사이트맵에 2~3시간분만 있어 하루 3번으로는 놓침. 피드만 매시간 읽는 timer를 따로 두는 방식이라 정기 timer 정책(AGENTS.md) 변경 승인 필요. 추천: 이번 사이트맵 추가 효과를 1주 본 뒤 결정
  - 사용자 10/27: "1주 보고 결정" → **11/3 무렵** 10/27~11/2 당일 확보율(발행 당일 처음 확인한 비율)과 놓친 매체를 정리해 보고
  - 같은 기간 Claude 웹 검색으로 당일 기사를 따로 찾아 비교(AI 웹 검색을 수집원으로 쓸지 판단 근거). 파이프라인은 바꾸지 않음
- [ ] **3. (선택) 로그인 무효화 재발 방지** Cursor 문서가 무인 실행용으로 안내하는 API 키(`CURSOR_API_KEY`)를 4개 service에 넣는 방식. 다른 게시기 service 설정을 바꾸는 일이라 승인 필요. 추천: 헬스체크에 로그인 상태 점검을 먼저 넣고(제 일), 다시 풀리면 그때 API 키로 전환
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

### 10/26 12:00 KST 정기 실행
- 헬스체크: News Monitor ok(경고 `briefing_fallback`). 서비스 12:00:15–12:03:05 성공, published(`ae27445`)
- 로그인: `agent status` 로그인됨. 새 기사 1건 Cursor 분류 성공(실패 0) → 서비스 안에서도 복구 확인
- 브리핑 fallback: 2회 모두 `group_number_not_grounded`. 새 기사 한 건짜리 묶음에 모델이 제목에 없는 "100"을 넣음(운영 입력 재현 3/3)
- 조치: 그 묶음의 문구만 기사 제목으로 바꾸도록 수정(`f2821fc`). 실패 테스트 → Mac 3.12/3.14 162개·Node 7·ruff → OCI 반영·테스트 통과 → 운영 입력 2/2 ok
- 수동 재게시 안 함(정기 실행 성공, 18:00부터 적용)

### 10/26 07:00 KST 정기 실행
- 헬스체크: Dev Blog ok(11/11), Research Wiki ok(단 1/2편), OSS Radar failed, News Monitor ok(경고 `collection_partial`·`briefing_fallback`)
- 뉴스: 07:00:27–07:02:22 성공, 공개 전 검사 통과, published(`3e7564f`). 수집원 22/22, 29건·24매체(신규 0·재확인 13·유지 16), partial은 `upstream_limit_reached`(오류 아님)
- 브리핑 fallback: Cursor 호출 2회 각 1초 안 실패. 04:00:47 Research Wiki 실행 중 Cursor 로그인 무효화 → 이후 "Not logged in". 날짜 분할(`d25f305`)과 무관(10/25 16:40·18:00 ok)
- 분류는 29건 모두 캐시라 게시는 됐음. 공개 페이지 부제 "2026.10.26 07:00 KST 수집 기준", 오늘 부정 기사 없음
- 조치: 인증 오류 분류 수정(`657f488`, 실패 테스트 → Mac 3.12/3.14 161개·Node 7·ruff → OCI 반영·161개 통과). 로그인 복구는 사용자 작업. 수집·게시는 성공했으므로 수동 재시도 안 함
- Tavily: 우리 회차당 5크레딧 확인, 다른 사용 없음

### 10/25 07:00 KST 첫 정기 실행
- 결과: 4개 파이프라인 모두 ok. news-monitor는 성공(exit 0), preflight 13개 통과, pushed → published(`ac555a3`)
- 수집: 22/22 수집원 성공·오류 0, 25건(신규 12·재확인 2·유지 11)·19매체, 상태 partial(`upstream_limit_reached`, 오류 아님)
- 분류: Cursor 25회·실패 0. 브리핑: 2회 모두 `group_date_span_exceeded` → fallback
- 원인: 모델이 같은 이슈를 13~21일 간격으로 묶음(재현 2/2), 프롬프트 보강으로도 3/3 실패
- 조치: 날짜 분할 + 분할 묶음 요약을 자체 제목으로(`d25f305`). 실패 테스트 → Mac 156개·Node 7·ruff → OCI 실입력 4/4 ok → OCI `a881395` 반영·테스트 156개 통과
- 운영 data·dist는 쓰지 않음, 시험 사본(`/tmp/news-monitor-trial`)·프롬프트 시험 파일 삭제
