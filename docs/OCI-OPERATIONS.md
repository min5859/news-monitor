# OCI 뉴스 모니터 운영 가이드

작성: 2026-10-19, 갱신: 2026-10-23 KST. 공개 샘플이며 사내 인증 사이트가 아니다.

## 현재 구성

- 공개 주소: <https://min5859.github.io/news-monitor/>
- 계정: 기존 `wiki-publisher`. 새 계정·Codex 설치·로그인 복사는 하지 않았다.
- 서비스: `news-monitor.service`, 타이머: `news-monitor.timer`.
- 매일 **07:00 Asia/Seoul**, 무작위 지연 30초·타이머 정밀도 1분 이내.
  첫 예약은 **2026-10-20 07:00 KST**였다. 10/22 정기 실행과 공개 배포를 확인했다.
  다만 이슈 fallback은 부분 성공이며 별도 장애 기록에서 복구 여부를 확인한다.
- `Persistent=true`: 서버가 꺼져 놓친 예약은 복구 후 보완 실행한다.
- 소스 소유자와 실행 계정 모두 기존 게시기와 같은 `wiki-publisher`다. 매일 자동 pull하지 않으며
  검증한 소스를 해당 계정으로 수동 업데이트한다. root로 Git 작업하지 않는다.
- 기존 Dev Blog·Research Wiki·OSS Radar의 유닛·인증·코드는 변경하지 않았다.

```text
/srv/news-monitor/
├── app/       wiki-publisher 소유 소스(main), 설정·프롬프트·실행 코드
├── config/    공개하지 않는 배포 설정·개인키·GitHub 호스트 키
├── data/      수집 상태·기사 메타데이터·검증한 분류 캐시·게시 상태
├── dist/      공개 전 검사 대상 정적 파일 4개
└── publish/   pages-data 전용 Git 작업 디렉터리
```

프로젝트 루트와 app 디렉터리는 `wiki-publisher` 소유 0755이며 기존 소스 파일 모드는 유지한다.
운영 디렉터리는 `wiki-publisher` 0700이다. 개인키는 0600이며
배포 설정·known_hosts는 root 소유, wiki-publisher 그룹 읽기만 허용한다.
별도 `.env`나 `GITHUB_WIKI_TOKEN`은 필요하지 않다.

## 소스 관리 계정 통일

10/23에 root 소유 소스를 기존 세 게시기와 같은 `wiki-publisher` 소유로 통일했다.
서비스 실행 계정·타이머·인증·경로는 그대로다. `ProtectSystem=strict`로 서비스 안에서는
소스/홈을 읽기 전용으로 유지하고, 서비스 밖에서는 해당 계정으로 소스를 관리한다.
같은 계정의 다른 프로그램도 서비스 밖에서는 소스를 수정할 수 있다는 경계는 기존 게시기와 같다.

```bash
# wiki-publisher 셸에서는 바로 조회한다.
cd /srv/news-monitor/app
git log --oneline -10

# ubuntu에서는 소유 계정으로 Git 명령을 실행한다.
sudo -u wiki-publisher -H git -C /srv/news-monitor/app status --short
sudo -u wiki-publisher -H git -C /srv/news-monitor/app log --oneline -10

# 실행 중이 아니고 작업 트리가 깨끗할 때, 검증한 main만 갱신한다.
sudo -u wiki-publisher -H git -C /srv/news-monitor/app pull --ff-only origin main
```

ubuntu에서 직접 Git을 실행하면 소유자가 달라 보호 오류가 날 수 있다. 전역 safe.directory 예외나
전체 chmod 대신 위의 계정 전환 방식을 사용한다. 다른 소유권/계정/배포 정책이 필요하면
적용 전에 이유와 영향을 설명하고 승인받는다. [변경 계획·검증 기록](OWNERSHIP-ALIGNMENT.md)을 본다.

## 매일 실행되는 흐름

1. `.daily.lock`으로 전체 실행을 잠근다. 단계별 `.collection.lock`도 유지한다.
2. Tavily 뉴스 검색어 5개(검색어당 최대 20건, 하루 5크레딧)와 `feeds`의 언론사 사이트맵·RSS 17개를 읽는다.
   키는 `/srv/news-monitor/config/tavily.json`(wiki-publisher 0600)이며 출력·로그에 남기지 않는다.
   사이트맵·RSS는 문서 호스트의 robots.txt가 허용할 때만 받는다. 기사 페이지·본문·사진은 받지 않는다.
   일부 수집원 실패는 `partial`이고, 모두 실패하면 `failed`로 이전 정상 데이터를 보존한다.
   [수집원 비교 기록](SOURCE-EVALUATION.md)을 본다. 이전 Codex 웹 검색 기록은 [보완 기록](SEARCH-BUDGET.md)에 있다.
3. 제목 인명·발행일·URL을 코드 규칙으로 검사하고, 같은 URL은 언론사 제공 날짜를 우선한다.
   이전 30일 안의 검증 기사와 누적하며 공개 선정은 최대 60건이다.
   최초·마지막 확인 시각과 이전 수집 재사용을 구분한다.
4. Cursor `claude-sonnet-5-5-medium`으로 제목만 분류한다(실행당 최대 60회). 기존 Cursor 로그인과 분류 캐시를 재사용한다.
5. 공개용 제목을 같은 Cursor 모델에 전달해 이슈·브리핑을 작성한다. 최대 2회·호출당 90초로
   제한하며 입력·모델·프롬프트가 같으면 `data/briefing` 캐시를 재사용한다.
   AI는 기사 ID를 선택하고, 인용 근거는 코드가 해당 실제 제목에서 그대로 복사한다.
   실패하면 제목별 표시/partial로 남긴다. 기사 논조 분류 실패와는 구분한다.
6. 정적 HTML/CSS/JS/JSON을 만들고 공개 전 검사를 통과한 경우에만 push한다.
7. `pages-data` 루트에서 GitHub Pages가 빌드한다. 공개 파일 4개의 바이트가 실제
   push한 파일과 같은지 최대 180초 확인한다. 성공은 `published`로 기록한다.

수집·검증·분류 호출 실패, 보류뿐인 결과, 이전 기사만 유지한 결과는 새 배포를 차단한다.
일부 검색 실패는 `partial`로 명시하며, 검증한 관련 분류가 있고 모델 호출 실패가 없어야
게시할 수 있다. 판단 보류 자체는 호출 실패가 아니며 통계 분모에서 제외한다.
최대 실행 시간은 60분, 메모리는 2 GiB, CPU는 1코어 상당으로 제한했다.
읽기 전용 소스/홈과 허용된 운영 경로만 사용하며 전역 AppArmor를 변경하지 않았다.

## 일일 Health Check

`oci-publisher-healthcheck` 스킬(Claude·Codex 공통, 읽기 전용)이 news-monitor도 점검한다.
Mac에서 `python3 <skill-dir>/scripts/healthcheck.py --ssh oci [--date YYYY-MM-DD]`를 실행한다.
당일 수집의 `published`가 예약 실행 시간 안에 있어야 성공이며, 실패 시 이유와 preflight 실패 항목을 함께 보여 준다.
직접 확인할 때는 아래 명령을 OCI의 ubuntu 셸에서 실행한다. Mac에서는 `ssh oci`로 접속한다.

```bash
sudo systemctl list-timers news-monitor.timer --all --no-pager
sudo systemctl show news-monitor.service -p Result -p ExecMainStatus -p ActiveState
sudo journalctl -u news-monitor.service --since today --no-pager -n 30
sudo -u wiki-publisher python3 -m json.tool /srv/news-monitor/data/collection_status.json
sudo -u wiki-publisher python3 -m json.tool /srv/news-monitor/data/pipeline_status.json
sudo -u wiki-publisher python3 -m json.tool /srv/news-monitor/data/publication_status.json
```

확인할 것은 예약 활성화, 최근 실행 시각/종료 코드, 실제 수집 상태, 분류 실패 수,
게시 `published` 상태와 최근 수집 시각의 일치, 공개 화면의 수집 시각이다.
타이머가 active라는 이유만으로 뉴스 갱신 성공으로 판정하지 않는다.
첫 예약 전에는 `LastTrigger`가 비어 있고 정식 서비스 실행 이력이 없는 것이 정상이다.
2026-10-19 첫 실제 실행은 별도 임시 `news-monitor-collect-trial.service` 기록이다.

- `pushed`: GitHub push까지만 확인한 상태다. Pages 성공과 다르다.
- `verification_failed`: push는 됐지만 공개 파일 일치를 확인하지 못했다.
  GitHub Actions의 `pages build and deployment` 결과와 공개 시각을 확인한다.
- `retained_articles_only`: 검색은 끝났지만 이번에 확인한 선정 기사는 없고 이전 기사만 남았다.
  새 성공으로 게시하지 않는다. 원문 전체가 0건이라는 뜻은 아니다.
- 기사 **당일 발행 0건**과 수집 실패는 다르다. 화면의 선택 기간 기사와 당일 영역을 구분한다.
- 모델은 기존 Cursor 구독 사용량을 소비한다(`agent status`로 로그인 확인). 인증 만료·사용 한도는 계정 관리자가 조치한다.
- wrapper가 게시 단계 전에 실패하면 publication_status에는 이전 게시가 남을 수 있다.
  반드시 service·pipeline·collection의 최신 시각도 함께 비교한다.

## 수동 실행·안전한 재시도

```bash
# 전체 실행: 먼저 request_state.json의 retry_not_before를 확인한다.
sudo systemctl start --no-block news-monitor.service
sudo journalctl -u news-monitor.service -f

# 수집 없이 최근 유효 입력으로 다시 분류/화면 생성; 24시간 정합성 검사는 유지한다.
sudo -u wiki-publisher -H bash -lc '
  cd /srv/news-monitor/app
  python3 -B -m news_monitor.run --reuse-collection \
    --data-dir /srv/news-monitor/data --output-dir /srv/news-monitor/dist
'

# 재배포: 검증을 우회하지 않으며 공개 파일 일치까지 확인한다.
sudo -u wiki-publisher -H bash -lc '
  cd /srv/news-monitor/app
  python3 -B -m news_monitor.publish \
    --data-dir /srv/news-monitor/data --output-dir /srv/news-monitor/dist \
    --checkout /srv/news-monitor/publish \
    --publish-config /srv/news-monitor/config/publish.json
'

# 예약 중단. 현재 실행 중인 서비스나 이미 공개한 페이지는 삭제하지 않는다.
sudo systemctl disable --now news-monitor.timer
```

호출 대기·잠금 파일을 지우거나 다른 data-dir로 재실행하지 않는다. Git push가 실패하면
권한·네트워크·작업 디렉터리의 수동 변경을 확인한다. force push/reset으로 덮지 않는다.
`--push-only`는 최초 Pages 설정 전 브랜치 생성용이며 일상 운영의 성공 확인용이 아니다.

## 자격 증명·보존·점검 범위

- GitHub 전용 deploy key ID `166885129`, 제목 `OCI news-monitor daily Pages`.
  이 저장소의 **전체 쓰기** 권한이며 브랜치별 제한은 아니다. 다른 저장소 권한은 없다.
- 개인키는 `/srv/news-monitor/config/pages_ed25519`에만 둔다. 공개키만 GitHub에 등록했다.
  무기한 키이므로 서버 폐기/노출 시 저장소 Settings → Deploy keys에서 철회한다.
- 호스트 키는 GitHub 공식 `/meta`의 공개 SSH 키로 고정했다. 키 변경 오류가 나면 공식
  공지/지문을 확인한 뒤 교체한다. StrictHostKeyChecking을 끄지 않는다.
- 로그는 systemd journal에 요약 상태만 기록한다. 원시 모델 출력·stderr·전문·사진은
  앱 로그/데이터에 저장하지 않는다. 서버의 기존 journal 보존 정책을 변경하지 않았다.
- 기사 목록은 최근 30일·최대 30건으로 갱신하며 상태 파일은 최신 값으로 교체한다.
  실패 시 마지막 정상 자료는 남기며 과거 일별 스냅샷은 아직 만들지 않는다.
- 분류 캐시는 자동 삭제하지 않는다. 공개 브랜치 Git 이력과 함께 월 1회 용량을 확인한다.
  큰 증가가 있으면 실제 파일을 확인한 뒤 별도 정리한다. 무검증 자동 삭제를 넣지 않았다.
- Cursor·Codex 자체의 인증/로그/캐시(`~/.cursor`, `~/.codex`)와 CLI 업데이트는 공유 계정 기존 구성이다. 뉴스 서비스
  전용 데이터 정리 명령으로 공유 계정의 홈이나 다른 프로젝트 파일을 삭제하지 않는다.

```bash
sudo du -sh /srv/news-monitor/{app,data,dist,publish}
df -h /
```

공개 사이트는 원문 링크를 통해 인물을 알 수 있다. 회사 내부 자료나 담당자 평가를 넣지 않는다.
이슈 묶음은 제목만으로 추정한다. 장기 일별 수집 스냅샷·유료 소스·사내 인증은 별도 확장 작업이다.

## 화면 스키마와 이슈 처리 점검

- 공개 report schema_version=2, insights 안에 이슈·모델 호출/캐시 상태·기간별 집계가 있다.
- pipeline_status의 briefing.status가 ok인지 확인한다. fallback이면 제목별 표시로 내려간
  부분 성공이다. 새 분류 캐시와 이슈 캐시는 서로 다르며 원래 기사 수집 시각을 바꾸지 않는다.
- preflight는 새 스키마·제목 근거·기간/집계·HTML/JSON·현재 JS/CSS 일치를 검사한다.
  구형 v1 자료는 실패 시 표시만 호환하고 새 공개 준비 완료로 처리하지 않는다.
- 실제 조회 범위가 14일인 이전 입력으로 30일 화면을 만들면 부족한 날짜는 자료 범위 밖이다.
  날짜별 표는 현재 확보 기사 발행일 분포이며 과거 일별 수집 스냅샷이 아니다.

## 오류 원인 확인과 제한된 이슈 재검증

- `collection_status.json`의 검색 오류에는 reason/elapsed_seconds가 있다.
- `data/briefing_status.json`에는 마지막 이슈 실행의 단계·오류 코드·시도 횟수·캐시/강제 갱신
  여부를 남긴다. `data/diagnostics.json`은 오류만 최대 200개 보존한다. 다음 성공이 과거 오류를
  지우지 않는다. raw stderr·모델 응답·인증 정보는 기록하지 않는다.
- 허용된 코드는 시간/출력/검색 한도, 사용량/인증/서비스 오류, JSON·ID·제목 근거·숫자·
  날짜 간격 등이다. 사용량/인증/고정 스키마 오류는 같은 실행에서 추가 재시도하지 않는다.
- 이슈 단계만 확인할 때는 아래 명령을 사용한다. 검색은 실행하지 않고 논조는 캐시만 허용하며,
  캐시가 부족하면 classification_cache_required로 중단한다. 기존 유효기간 검사도 유지한다.

```bash
sudo -u wiki-publisher -H bash -lc '
  cd /srv/news-monitor/app
  python3 -B -m news_monitor.run --briefing-only --refresh-briefing --briefing-attempts 1 \
    --data-dir /srv/news-monitor/data --output-dir /srv/news-monitor/dist
'
sudo -u wiki-publisher python3 -m json.tool /srv/news-monitor/data/briefing_status.json
```

강제 갱신은 기존 캐시를 삭제하지 않는다. 이 명령은 파일 생성까지이며 공개 배포는 별도다.
pipeline_status.operation=briefing_only는 수동 이슈 재처리이므로 아침 예약 실행의 성공으로
오인하지 않는다. 재검증/수정 결과는 [장애 기록](DIAGNOSTICS-REPAIR.md)에 남긴다.

## 근거

확인일: 2026-10-19.

- [GitHub Pages 브랜치 게시](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site)
- [GitHub deploy key의 범위와 제한](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys)
- [GitHub 공개 호스트 키 메타데이터](https://api.github.com/meta)
- [OpenAI 비대화형 읽기 전용 실행](https://developers.openai.com/codex/noninteractive/)
