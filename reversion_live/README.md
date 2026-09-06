# BTC BB/CCI 1분봉 봇

`main.py`는 Binance USD-M `BTCUSDT` 무기한 선물의 완료된 1분봉만 사용한다. 기본값은
모의매매이며 `LIVE_TRADING=1`을 명시한 경우에만 실주문을 전송한다.

## 전략 고정값

- BB z-score: 종가 90봉, 모집단 표준편차, 최근 10봉 극단값 `±1.5`
- CCI: typical price 40봉, `-100` 상향 돌파(롱) / `+100` 하향 돌파(숏)
- 추세 필터: EMA(600), `adjust=False`; 롱은 EMA 위, 숏은 EMA 아래
- 최초 1,440봉은 워밍업이며, 1분봉 간격이 끊기면 신규 신호를 중단
- 진입: 신호가 확정된 다음 봉부터 종가 대비 유리한 방향 5bp의 `GTX` 지정가, 20분 TTL
- 익절: 체결 평단 대비 60bp의 `GTX` reduce-only 지정가
- 손절: 체결 평단 대비 350bp의 거래소 `STOP_MARKET`, `closePosition=true`
- 최대 보유 12시간, 청산 후 5분 대기
- 물타기/마틴게일 없음. 전 계좌에서 동시에 한 포지션/진입 주문만 허용
- 수수료를 제외한 예정 손절액이 진입 직전 총 계좌자산의 최대 3%가 되도록 수량 산정

손절 갭, 시장가 슬리피지, 수수료와 펀딩비는 3% 예산 밖의 추가 손익이다. 레버리지는
1배 격리로 고정하고, 명목 노출은 이론상 계좌의 약 85.714%(`3% / 3.5%`)다. 거래소
수량 단위로 내림하기 때문에 실제 예정 손실은 3% 이하가 된다.

## 환경변수

필수/모드 변수:

```dotenv
BINANCE_API_KEY=
BINANCE_API_SECRET=
LIVE_TRADING=0
```

키는 `LIVE_TRADING=1`일 때만 필요하다. 봇은 `reversion_live/.env`, 그 다음 저장소 루트의
`.env`를 읽되 이미 서비스 환경에 설정된 값은 덮어쓰지 않는다. 키 값은 로그에 기록하지
않는다.

선택 변수:

```dotenv
REVERSION_DATA_DIR=/home/ubuntu/reversion_live/data
REVERSION_LOG_PATH=/home/ubuntu/reversion_live/data/paper.jsonl
REVERSION_STATE_PATH=/home/ubuntu/reversion_live/data/paper_state.json
PAPER_INITIAL_EQUITY=10000
PAPER_MAKER_FEE=0.0002
PAPER_TAKER_FEE=0.0006
PAPER_MARKET_SLIPPAGE=0.0001
REVERSION_POLL_SECONDS=3
```

모의/실거래 기본 로그와 상태 파일은 서로 분리된다. JSONL 로그에는 UTC와 한국시간이 함께
기록되고, 상태 파일은 원자적으로 교체되어 재시작 시 미체결 주문·포지션을 이어서 관리한다.
모의 자산은 설정된 maker/taker 수수료와 시장가 슬리피지는 반영하지만 펀딩비는 계산하지
않는다. 실거래 펀딩비는 Binance 계정에 실제로 반영되며, 전진검증 성과 집계 시 별도로
포함해야 한다.

## 실거래 전제와 전환 절차

이 봇은 Single-Asset 모드의 전용 Binance USD-M 계정 또는 서브계정을 전제로 한다. 시작 시 다른 심볼의 포지션,
봇 접두사(`bbcci-`)가 없는 주문, 복수 포지션을 발견하면 어떤 주문도 임의로 취소하지 않고
중단한다. 계정이 비어 있을 때 one-way, isolated, 1x를 설정한다. 실포지션을 발견하면 실제
평단과 수량으로 복구하고 손절 주문을 먼저 확인/생성한 후 익절 주문을 관리한다.

1. 최소 며칠 이상 `LIVE_TRADING=0`으로 실행하고 `paper.jsonl`을 검토한다.
2. API 키는 USD-M 선물 거래 권한만 주고 출금 권한은 부여하지 않는다.
3. 전용 계정에 미확인 주문/포지션이 없는지 확인한다.
4. 별도 상태·로그 경로가 자동 선택되는지 확인한 뒤 `LIVE_TRADING=1`로 전환한다.

모의 진입과 익절은 지정가를 정확히 1bp 초과 관통해야 체결되며, 최초 다음 봉 시가에서
지정가가 이미 시장성이면 post-only 거절로 기록한다. 손절 갭은 손절가와 시가 중 불리한
가격에 슬리피지를 더한다.

연구 백테스트는 Binance 차트를 신호로 쓰면서 Bitget 체결 비용을 가정했다. 이 구현은 제공된
키 구성에 맞춰 Binance에서 신호와 체결을 모두 수행하므로, 거래소별 수수료·post-only 체결률
차이를 모의 로그로 먼저 검증해야 한다. 과거 수익률은 향후 성과나 월 20% 수익을 보장하지 않는다.
