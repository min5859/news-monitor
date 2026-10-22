# OCI 소스 소유권 통일

작성일: 2026-10-23 KST. 기존 세 게시기와 동일한 관리 계정으로 맞추라는 사용자 승인에 따른다.

## 목적과 변경 범위

- 기존 Dev Blog·OSS Radar·Research Wiki는 소스와 실행 계정 모두 `wiki-publisher`다.
  뉴스 모니터만 root 소유로 설치해 Git 조회/수정에 다른 절차가 필요했다.
- `/srv/news-monitor` 디렉터리 자체와 `/srv/news-monitor/app` 내부만
  `wiki-publisher:wiki-publisher` 소유로 통일한다. 파일 내용과 모드는 바꾸지 않는다.
- config/data/dist/publish의 소유권·권한, 인증·타이머·서비스 유닛·다른 프로젝트는 변경하지 않는다.
- `ProtectSystem=strict`와 기존 ReadWritePaths를 유지해 서비스 안에서는 소스를 읽기 전용으로
  둔다. 서비스 밖에서는 같은 계정의 프로그램이 소스를 수정할 수 있으며 기존 운영 방식과 같다.

## 진행 계획

1. 서비스가 실행 중이지 않은지, 소스 작업 트리가 깨끗한지, 경로·소유권·링크를 확인한다.
2. 위의 두 대상만 소유권을 변경한다. Git safe.directory 전역 예외나 전체 디렉터리 chmod는 하지 않는다.
3. `wiki-publisher`로 예외 설정 없이 Git 조회/상태 확인·로컬 단위 테스트를 수행한다.
4. 동일한 systemd 보호를 적용한 별도 임시 유닛에서 소스 쓰기가 EROFS로 차단되는지 확인한다.
   뉴스 수집·모델 호출·기존 정기 서비스 재실행·배포는 하지 않는다.
5. 현재 운영 가이드·에이전트 지침·OCI wiki에 변경과 검증 결과를 기록하고 커밋한다.

## 일상 관리 원칙

- 소스 Git 명령은 `wiki-publisher`로 실행한다. root로 Git 작업해 root 소유 파일을 다시 만들지 않는다.
- ubuntu에서 접근할 때는 `sudo -u wiki-publisher -H git -C /srv/news-monitor/app ...`를 사용한다.
- 전역 `safe.directory=*`나 소스 전체 쓰기 허용으로 해결하지 않는다.
- 서비스 유닛 자체의 변경은 관리자 작업이며 소스 관리와 구분한다. 매일 자동 pull하지 않는다.
- 기존 운영 방식과 다른 소유권·계정·배포 정책은 적용 전에 이유·영향을 설명하고 승인받는다.

## 결과

- 10/23 00시대 KST, 서비스 inactive·소스 clean과 root 소유 app 항목 332개를 확인했다.
  외부 심볼릭 링크·일반 파일 하드링크·다른 파일시스템 경로가 없는 것을 확인한 뒤 변경했다.
- app의 332개 항목과 프로젝트 루트 디렉터리만 소유권을 변경했다. 변경 직후 전체 소스/
  Git 파일 해시·모드가 같고 config/data/dist/publish 소유권·모드도 같음을 비교했다.
- `wiki-publisher`에서 별도 Git 신뢰 예외 없이 log/status가 정상 동작했다.
- 같은 계정으로 OCI Python 테스트 137개와 Node 화면 테스트 7개를 통과했다.
- 동일 ProtectSystem/ProtectHome/ReadWritePaths로 실행한 임시 systemd 검증 유닛에서
  README와 .git/config 쓰기 열기가 모두 EROFS로 차단됐다. data/dist/publish는 쓰기 가능했다.
  파일을 변경하거나 모델/뉴스 호출을 하지 않았다. 검증 유닛은 종료 후 자동 수거했다.
- 실제 정기 service/timer는 수정하거나 다시 실행하지 않았다. 10/23 07시 예약은 그대로다.
