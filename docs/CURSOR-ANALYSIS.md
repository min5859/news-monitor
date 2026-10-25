# 분류·브리핑 AI 전환: Codex → Cursor Sonnet 5.5

작성일: 2026-10-25 KST. 사용자 요청: 분류 AI를 Cursor `claude-sonnet-5-5-medium`(Sonnet 5.5, medium effort)으로 바꾼다.

## 확인한 제약

- 기존 Cursor 경로는 `--sandbox enabled`를 강제했다. OCI는 `kernel.apparmor_restrict_unprivileged_userns=1`이라
  `cursorsandbox`의 사용자 네임스페이스 생성이 거부된다(`apparmor="DENIED" … capname="sys_admin"`).
  10/24 단건 호출로 재현했다([10/18 기록](STAGE-4.md)과 같음).
- 같은 `wiki-publisher` 계정의 dev-blog·research-wiki·oss-radar는 Cursor를 샌드박스 없이 쓴다.
  계정 설정이 `sandbox.mode: disabled`(allowlist)이고 `agent -p --mode ask --trust`로 호출한다.
- news-monitor 서비스만 `ProtectHome=read-only`다. Cursor는 실행마다 `~/.cursor/projects`에 기록한다.
  쓰기를 허용하지 않으면 `ENOENT: … mkdir '/home/wiki-publisher/.cursor/projects/…'`로 실패한다.

## 결정

- 다른 게시기와 같이 `--sandbox disabled`로 실행한다. 대신 다음을 유지한다.
  - ask 모드로 실행한다.
  - 매 호출마다 빈 workspace를 새로 만든다. 경로는 고정해서 Cursor 프로젝트 기록이 쌓이지 않게 한다.
  - Shell·Read·Write·WebFetch·MCP를 모두 거부한다.
  - 응답은 기존처럼 엄격히 검증한다.
- 전역 AppArmor·커널 설정은 바꾸지 않는다.
- 서비스에는 쓰기 경로 `/home/wiki-publisher/.cursor` 하나만 추가한다. `ProtectSystem=strict`와 기존 경로는 유지한다.
  기존 유닛은 `/root/news-monitor.service.bak-20261025`에 백업했다.
- 브리핑도 `briefing.provider`로 같은 Cursor 모델을 쓴다. Cursor에는 출력 스키마 옵션이 없으므로
  프롬프트에 JSON 형태를 명시하고, 기존 ID·근거·숫자 검증이 형식을 강제한다.
- 모델이 바뀌면 분류 캐시가 무효가 된다. 한 번 전체를 다시 분류하도록 실행당 호출 상한을 30에서 60으로 올렸다.
- Codex(`provider=codex`, `gpt-6-luna`)는 설정만 바꾸면 되돌릴 수 있다.

## 검증

- 실패 테스트를 먼저 작성했다.
  - `--sandbox disabled`·고정 workspace 재사용
  - 브리핑 Cursor 경로·`provider` 기록
- 수정 후 Mac 3.12/3.14에서 155개, Node 7개, Ruff를 통과했다.
- `1a2a434`를 OCI에 ff-only로 반영하고 유닛을 설치·daemon-reload했다. 설치본은 저장소와 같고, OCI 테스트도 통과했다.
- 서비스와 같은 보호 설정의 임시 유닛에서, 운영 데이터 사본(14건)을 PrivateTmp에 두고 전체 실행했다.
  - Cursor 분류 13회(같은 제목 1건은 캐시)로 실패는 0건이었다. 분포는 긍정 12·중립 2로 Codex와 같았다.
  - 브리핑은 1회 호출로 이슈 6개를 만들었다. 전체 101초가 걸렸다.
  - 공개 전 검사는 사본이 이전 기사만 남은 10/24 자료라서 `has_current_search_results`만 실패했다. 나머지는 통과했다.
- 운영 data/dist/publish 해시는 전후로 같았다. 진단 중 만든 Cursor 임시 프로젝트 기록 1개는 삭제했다.
- 첫 정기 실행은 10/25 07:00 KST다. 새 기사 약 17건을 포함해 30건 전체를 다시 분류한다(상한 60회).

## 첫 정기 실행 (10/25 07:00 KST)

- 분류:
  - Cursor 호출 25회, 실패 0건이었다. 모델이 바뀌어 캐시 적중은 0이다.
  - 상한 60회 안에서 끝났고 공개 전 검사도 통과했다.
- 브리핑: 2회 모두 `group_date_span_exceeded`로 거부되어 제목별 fallback으로 게시됐다.
- 원인:
  - 30일 기사가 늘면서 모델이 같은 이슈로 본 기사를 13~21일 간격으로 한 묶음에 넣는다.
    운영 입력(25건)으로 다시 호출해도 2/2 같은 결과였다.
  - 프롬프트에 날짜 차이 규칙과 예시를 넣어도 3/3 실패했다. 모델이 날짜 계산을 지키지 않는다.
- 수정(`d25f305`):
  - 모델 묶음이 `max_group_span_days`(7일)를 넘으면 코드가 날짜순으로 나눈다.
    묶음의 첫 기사부터 7일 안의 기사끼리 한 묶음이다.
  - 나뉜 묶음은 label을 유지한다. 요약은 묶음 전체에 대해 쓴 글이므로 각 묶음의 기사 제목으로 바꾼다
    (기간 필터의 `select_groups`와 같은 방식).
  - 공개 검증(날짜 간격·숫자 근거·ID)은 바꾸지 않았다.
- 검증:
  - 실패 테스트를 먼저 작성했다. 수정 후 Mac 3.12/3.14에서 156개, Node 7개, Ruff를 통과했다.
  - 운영 입력으로 시험했다. 요약을 그대로 두면 3회 중 1회가 숫자 근거 검증에 걸렸다.
    요약을 바꾼 뒤에는 4/4 ok였다.
  - 시험은 `/tmp` 사본으로 했다. 운영 data·dist는 쓰지 않았고 시험 파일은 지웠다.
- 정기 실행은 성공했으므로 오늘 공개본은 fallback 그대로 두었다.
  수동으로 다시 게시하면 정기 실행 검증과 섞이기 때문이다. 다음 정기 실행(10/26 07:00 KST)부터 적용된다.

## 날짜 분할 첫 적용 (10/25 16:40 KST)

- 하루 3번 timer 설치 직후 따라잡기 실행에서 처음 적용됐다.
- 브리핑 ok(17묶음), fallback 없음. 분류는 Cursor 14회(신규 6건과 제목이 정리된 기사 재분류)·캐시 16·실패 0이다.
- 10/25 18:00 회차도 브리핑 ok였다(기사 목록이 같아 이슈 캐시 재사용).

## 10/26 07:00 정기 실행: Cursor 로그인 만료

- 결과: 성공(exit 0)·published(`3e7564f`), 29건·24매체, 공개 전 검사 통과. 브리핑은 fallback(제목별 29묶음)이다.
- 원인은 날짜 간격이 아니라 Cursor 로그인이다. 브리핑 호출 2회가 각각 1초 안에 실패했다.
  - 04:00:47 KST Research Wiki 실행 중 Cursor CLI가 "Your stored authentication is invalid"를 내고 저장된 로그인을 지웠다.
    그 뒤 공유 계정 `wiki-publisher`의 `agent status`는 "Not logged in"이다.
  - 로그인 폴더(`~/.config/cursor`)는 9/25 22:37 KST에 만들어졌고 무효화는 30일 뒤였다. 다만 원인을 30일 만료로 확정할 수 없다.
    Cursor 문서에는 로그인 유효기간이 없고, ubuntu 계정의 8/29 로그인 파일은 아직 로그인 상태로 나온다(로컬 표시일 수 있음).
  - Cursor 기록 정리 timer는 원인이 아니다. 10/25 13:17에 0건을 지웠고 `~/.config`는 쓸 수 없게 막혀 있다.
- 분류는 29건 모두 캐시라 Cursor를 부르지 않아 게시는 됐다. 새 기사가 한 건이라도 나오면 분류가 실패하고
  공개 전 검사(`all_model_calls_completed`)가 게시를 막는다. 공개 페이지는 마지막 성공본에 머문다.
- 같은 원인으로 OSS Radar 05:00 실행이 실패했고, Research Wiki는 논문 2편 중 1편만 게시했다.
- 코드 결함: 인증 오류 판별이 Codex 문구("401", "unauthorized")만 알아서 Cursor 문구를 `model_cli_failed`로 기록하고
  재시도했다. Cursor 문구를 `model_authentication_failed`로 분류하도록 고쳤다(`657f488`, 실패 테스트 먼저, Mac·OCI 161개 통과).
  이제 같은 실행에서 재시도하지 않고 원인이 상태 파일에 그대로 남는다.
- 복구는 사용자 작업이다. 공유 계정으로 다시 로그인해야 한다([운영 가이드](OCI-OPERATIONS.md#자격-증명보존점검-범위)).
- 복구: 사용자가 07:56 KST에 `wiki-publisher`로 다시 로그인했다(`auth.json` 0600, 소유자 wiki-publisher).
  `agent status` 로그인됨, 빈 작업 폴더에서 같은 옵션으로 짧은 호출 1회 성공(exit 0). 다음 확인은 12:00 회차 브리핑이다.
- 날짜 분할(`d25f305`)은 10/25 16:40·18:00 회차에서 이미 확인했다. 10/26 07:00 fallback과는 무관하다.
