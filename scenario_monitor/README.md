# BTC 시나리오 조건·만료 모니터

`main.py`는 기본적으로 Binance USD-M BTCUSDT **알림 전용** 프로그램이다.
`SCENARIO_EXECUTION_MODE=live`를 명시하면 `ENTRY_READY`를 받아 시장가 진입과
전량 TP/SL 조건부 시장가 주문을 실행한다. 자동매매 설정·복구·제약은
[EXECUTION.md](EXECUTION.md)를 먼저 확인한다. 기본 `off` 모드에는 API 키가 필요 없다.
Python 3.12 표준 라이브러리만 사용하며 기존 트레이딩 봇/LLM 서버와 독립적으로 실행한다.

## 내장 계획의 수명

- 분석 시각: **2026-09-08 11:52 KST**
- 절대 만료: **2026-09-08 15:52 KST** (분석 후 4시간)
- 신호 발생 후 재시험과 진입가 근접까지: **최대 60분**, 전체 만료가 더 빠르면 그것을 적용
- 13:00 KST의 새 4시간봉 확정: `REVIEW_DUE` 알림. 자동 재분석/기한 연장 없음
- 기한 이후 처음 실행하면 `EXPIRED`만 기록하며 시장 데이터를 가져오지 않는다.
- 프로세스 재시작/재배포/상태 파일 삭제로 원래 절대 기한이 연장되지 않는다.

이 계획은 계속 재사용하는 일반 전략이 아니다. 새로운 분석이 필요하면 모든 가격선과
시각을 함께 검토한 새 JSON 계획을 사용한다. 시간만 현재로 바꾸는 방식으로 재활성화하지 않는다.

## 정확한 기계 판정

15초마다 최근 1분봉 1,500개를 조회하고, **완전히 닫힌 봉**만 UTC 경계로
5분/15분/1시간/4시간봉으로 집계한다. 거래량 기준은 신호 봉을 제외한 직전
20개 완성 봉의 단순평균이다. 중간에 빠진 분봉이나 중복, 비정상 OHLCV,
90초보다 오래된 최신 완성 봉은 허용하지 않는다.

분석 시각 이전 분봉은 워밍업용이다. 분석 시각에 걸친 불완전한 시작 구간의
신호 봉은 사용하지 않는다. 예를 들어 11:52 시작 계획은 11:45~12:00의
15분봉이나 11:00~12:00의 1시간봉에서 신규 신호를 만들지 않는다.
시작 이후 확정되는 1시간봉의 폐기 조건은 점검한다.

| ID | 1차 신호 (`ARMED`) | 이후 별도 봉의 재시험 (`CONFIRMED`) | 1시간봉 폐기 조건 |
|---|---|---|---|
| A | 직전 60분 이내 79,450~79,650 거래 후 15분 음봉 종가 <79,450, 거래량 >=1.3배 | 다음 5분봉부터 79,450~79,650에 거래되고 음봉 종가 <79,450 | 종가 >79,650, 신호 전에도 적용 |
| B | 1시간봉 종가 <78,550, 거래량 >=1.5배 | 다음 15분봉부터 78,580~78,700에 거래되고 음봉 종가 <78,620 | 신호 이후 종가 >78,750 |
| C | 직전 60분 이내 저가 <78,620, 78,420 미도달, 15분 양봉 종가 >78,750, 거래량 >=1.3배 | 다음 5분봉부터 78,700~78,780에 거래되고 양봉 종가 >78,750 | 신호 이후 종가 <78,550 |
| D | 1시간봉 종가 >80,050, 거래량 >=1.5배 | 다음 15분봉부터 79,880~79,950에 거래되고 양봉 종가 >79,950 | 신호 이후 종가 <79,880 |

“구간에 거래됨”은 해당 봉의 고가~저가가 구간과 겹치는 것이다. 양봉은 종가>시가,
음봉은 종가<시가이며 도지는 확인 봉이 아니다. 이 재시험 정의와 60분 사전 접촉
유효기간, 진입가 ±30 USDT 허용폭은 자연어 조건을 구현하기 위해 명시한 규칙으로,
최적화/승률 검증된 파라미터는 아니다.

`CONFIRMED` 뒤 확정 1분봉 종가가 아래 **1차 진입가 ±30 USDT** 안에 오면
`ENTRY_READY`를 한 번 알린다. 신호와 재시험을 같은 봉에서 동시에 처리하지 않는다.

| ID | 1차 진입 | 추가 진입 참고 | 손절 경계 | 익절 참고 1 / 2 / 3 |
|---|---:|---:|---:|---|
| A | 79,450 | 79,580 | 79,850 | 78,900 / 78,680 / 77,950 |
| B | 78,580 | 78,680 | 78,980 | 77,950 / 77,300 / 76,400 |
| C | 78,780 | 78,700 | 78,420 | 79,250 / 79,600 / 80,100 |
| D | 79,950 | 79,880 | 79,550 | 80,500 / 81,300 / 82,150 |

위 표는 만료된 내장 플랜의 예시다. 고정 증거금 15%+10% 안내는 제거했다.
판정기는 시나리오를 독립 감시하며 실제 체결은 별도 실행기가 관리한다.
자동매매는 격리 3배, 계획 위험 1% 이하, 첫 번째 목표에서 전량 익절하며
`add`와 두 번째·세 번째 목표로 추가/분할 주문하지 않는다. 비용 포함 손익비
1.5 미만이면 목표를 임의로 바꾸지 않고 진입을 건너뛴다.

## 폐기·만료와 알림 의미

- `WATCHING` → `ARMED` → `CONFIRMED` → `READY` 순서.
- `READY`는 **조건 알림을 보냈다는 상태**이며 진입/체결 완료가 아니다.
- `INVALIDATED`: 표의 1시간봉 폐기 조건. C는 신호 전에도 1분 저가 <=78,420이면 폐기.
  모든 시나리오는 신호 이후 1분 고가/저가가 손절 경계에 닿으면 진입 계획 폐기.
- `MISSED`: 신호 이후 첫 익절가에 먼저 도달했거나, 복구 중 과거의 진입 기회를 발견한 경우.
  `READY` 이후에도 실제 체결 여부를 모르므로 이 판정은 **미진입 계획에 관한 알림**이다.
- `EXPIRED`: 전체 절대 기한 또는 신호 60분 기한. `READY`에도 적용하며 실제 보유분 청산 뜻이 아니다.
- `SUSPENDED`: 불완전/오래된 데이터, 시계 불일치 또는 수동 중단으로 재평가 필요.
- 같은 시각에는 **만료 → 폐기/목표가 선도달 → 진입 조건** 순서로 처리한다.
- 폐기/만료/놓침/중단 상태는 그 계획 안에서 다시 활성화하지 않는다.
- 네트워크 일시 오류는 알림을 중단하고 재시도한다. 복구 시 누락 분봉을 순서대로 처리한다.
  복구할 수 없는 공백은 `SUSPENDED` 처리한다. 네트워크 장애 중에도 절대 기한은 흐른다.
- 재시작 시 지난 조건은 복원하지만 90초 이상 지난 진입 기회는 소급 추천하지 않는다.
- 뉴스/유가/금리/매물대 재분석은 자동화하지 않는다. `REVIEW_DUE`는 수동 점검 요청이다.
  자동매매 실행기는 새 4시간봉 마감 이후 신규 진입을 차단한다. 보유 포지션 관리는 계속한다.
  중단하려면 데이터 디렉터리에 `PAUSE` 파일을 만든다. 제거만으로 기존 계획을 부활시키지 않는다.

봉 기반 모니터이므로 알림에는 봉 확정 대기와 조회 지연이 있다. 거래소 스톱 주문을
대신할 수 없다. `off` 모드에서는 실제 손절 보호를 별도로 설정해야 한다.
`live` 모드에서는 실행기가 실제 체결을 확인하고 거래소에 TP/SL을 등록한다.

## 실행과 환경변수

```bash
python3 scenario_monitor/main.py --validate
python3 -m unittest discover -s tests -p 'test_scenario*.py' -v
python3 scenario_monitor/main.py --once
```

선택 환경변수:

```dotenv
SCENARIO_DATA_DIR=/home/ubuntu/scenario_monitor/data
SCENARIO_POLL_SECONDS=15
SCENARIO_TELEGRAM_BOT_TOKEN=
SCENARIO_TELEGRAM_CHAT_ID=
# 재분석한 외부 계획을 쓸 때만 지정
# SCENARIO_PLAN_PATH=/home/ubuntu/scenario_monitor/plan.json
```

텔레그램 토큰/채팅 ID를 모두 지정하면 그 채팅으로 알림을 보낸다. 둘 다 없으면
표준 로그에만 출력한다. 실제 값은 저장소에 커밋하지 않는다. `.env`는 Python이
자동 로딩하지 않으며 아래 systemd의 `EnvironmentFile`이 읽는다.

상태와 이벤트는 `data/state-<plan-hash>.json`에 원자적으로 저장한다. 같은 데이터
디렉터리에서 이중 실행은 파일 잠금으로 차단한다. 로그는 journald에서 확인한다.
텔레그램 전송 실패 시 outbox를 보존하고 재시도하되, 만료되거나 90초를 지난 진입
알림은 버린다. 전송 성공 직후 저장 전 프로세스가 종료되면 같은 이벤트 ID의 알림이
중복될 수 있다(at-least-once). 토큰/HTTP 응답 원문은 로그에 남기지 않는다.

새 계획 파일의 전체 형식은 다음 명령으로 얻는다. 가격/시각을 재검토하고 `plan_id`를
새로 지정한 후 `--validate`하고 서비스를 재시작한다. 기존 상태 파일은 보존된다.

```bash
python3 /home/ubuntu/scenario_monitor/main.py --print-default-plan > /home/ubuntu/scenario_monitor/plan.json
python3 /home/ubuntu/scenario_monitor/main.py --plan /home/ubuntu/scenario_monitor/plan.json --validate
```

## EC2 배포와 systemd

`.github/workflows/deploy-scenario-monitor.yml`의 **Deploy scenario monitor bot**을 수동 실행한다.
기존 reversion 배포와 같은 `production` 환경의 `EC2_HOST`, `EC2_USER`, `EC2_SSH_KEY`
시크릿과 `EC2_SSH_PORT` 변수를 사용한다. 별도 서비스 변수는
`EC2_SCENARIO_MONITOR_SERVICE`이며 기본값은 `scenario-monitor`다.

워크플로는 요청 ref의 테스트/설정 검증 후 `main.py`, `execution.py`,
`execution_store.py`, `binance_futures.py`를 `~/scenario_monitor/`에 함께 배포한다.
기존 `.env`, 외부 계획, 상태는 유지한다. 서비스가 이미 실행 중이면 재시작하고 실패 시
코드를 롤백한다. 첫 배포 또는 서비스가 정지된 상태라면 파일만 설치하고 시작은 운영자에게 맡긴다.
서비스 파일 자체는 자동 설치하지 않는다. Python 3.12가 설치되어 있어야 한다.

1. PR을 병합하고 Actions에서 **Deploy scenario monitor bot**을 실행한다(`ref=main`).
2. `scenario-monitor.service.example` 내용을 `/etc/systemd/system/scenario-monitor.service`에 작성한다.
   EC2 계정이 `ubuntu`가 아니면 `User`와 모든 `/home/ubuntu` 경로를 변경한다.
3. 필요하면 `/home/ubuntu/scenario_monitor/.env`에 위의 전용 텔레그램 설정을 넣고 `chmod 600` 한다.
4. 아래 명령으로 실행한다.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now scenario-monitor
sudo systemctl status scenario-monitor --no-pager
sudo journalctl -u scenario-monitor -f
```

수동 중단/점검:

```bash
touch /home/ubuntu/scenario_monitor/data/PAUSE
```

만료 상태에서도 프로세스는 대기하므로 systemd가 정상인 것과 활성 시나리오가 있는 것은
다르다. 이 프로그램은 자동으로 새로운 매매 시나리오를 만들어내지 않는다.
