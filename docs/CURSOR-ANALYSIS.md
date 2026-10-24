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
