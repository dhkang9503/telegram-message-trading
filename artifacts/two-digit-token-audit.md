# Two-digit token audit

Scanned every JSONL row under `data/**/*.jsonl` on PR #31's merge snapshot.

- Files: base train/validation/test, labeled/pending feedback, regression live failures
- Total parsed message rows: 2,839
- Rows containing a standalone two-digit integer token: 24
- Unique message/action pairs containing such a token: 18
- Cases where a standalone two-digit token numerically matched an explicit non-null action `price`: 0

The 18 unique cases divide into non-price quantities/times and price-level commentary that is not labeled as an explicit priced action.

Non-price examples include percentages or time/duration: `10퍼`, `25프로`, `75프로`, `20퍼`, `20분`, `9시30분`, `10시반`.

Price-context examples:

- `59깨지면 그냥 약손절 해버릴게요! ...` -> `actions=[]`
- `84라인 마지노선으로 보고있어서 ...` -> `actions=[]`
- `89깨지는지 보고 대응할게요!` -> `actions=[]`
- `롱 ㅂㅈ 61까지만 봐볼게여` -> `OPEN_LONG(price=null)`
- `손절 너무 멀어서 지금은 못 잡고 61후반정도 오면 ...` -> `actions=[]`
- `손절 70890걸어두고 71부근오면 짤짤이 물탈거에요 전!` -> `SET_STOP(price=70890)`; `71` is future commentary, not an ADD action
- `이거 그냥 59약손절 생각하고 그냥 둘게요!` -> `actions=[]`
- `일단 첫비중까지 다 날리겠습니다! 76중반 깨지는거 보고 대응할게요!` -> `CLOSE_ALL(price=null)`

Conclusion: the current dataset contains no labeled executable example equivalent to `64 물타기` -> `ADD(price=64)`. Therefore the intended restoration of a two-digit priced action cannot be derived from existing labels.
