# Scenario execution

Plan creation and market analysis remain external. This extension consumes the
existing plan schema and `ENTRY_READY` events; it does not generate plans or
change stops/targets to make a trade pass validation.

## Modes and activation

The existing systemd unit/ExecStart is unchanged. Add the settings from
`.env.example` to `/home/ubuntu/scenario_monitor/.env` (preserve Telegram keys):

```dotenv
SCENARIO_EXECUTION_MODE=off
SCENARIO_BINANCE_API_KEY=
SCENARIO_BINANCE_API_SECRET=
SCENARIO_SLIPPAGE_BPS=5
```

- `off` (default): existing monitor only; no exchange account calls/orders.
- `dry-run`: public quote/filter checks and sizing with **assumed $200 equity and
  0.05% taker fee**. No private calls, simulated fills, protection, or profit claims.
  A `DRY_RUN` message is a hypothetical order, not an execution.
- `live`: production Binance USD-M market orders. Requires the two scenario API
  variables. There is no fallback to the other bots' credentials.

Use an **exclusive account/subaccount**, USDT single-asset, one-way position mode,
with futures trading permission and no withdrawal permission. Restrict the key to
the server IP. Do not run a manual trader, leading-room bot or reversion bot in
this same account, even for another symbol. A local process lock cannot coordinate
with external traders; closePosition orders affect the entire BTC position.

Activate by setting `SCENARIO_EXECUTION_MODE=live`, supplying the dedicated keys,
and restarting the service. Deployment alone does not change the mode. Do not
enable live against old ENTRY_READY history: provide a newly reviewed plan first.

```bash
python3 /home/ubuntu/scenario_monitor/main.py --plan /home/ubuntu/scenario_monitor/plan.json --validate
sudo systemctl restart scenario-monitor
sudo journalctl -u scenario-monitor -n 40 --no-pager
```

`--validate` is offline and cannot trade. `--once` **can trade in live mode**.
The built-in September 8 plan is expired; it is not a runnable current strategy.
The JSON is loaded on startup; replacing it requires a restart. Preserve the
data directory across plan changes, code rollbacks and deployments.

## Entry contract

1. Receive a fresh ENTRY_READY (maximum 30 seconds since candle close).
2. Require valid plan/signal, READY state, healthy data, no PAUSE. Block new entries
   at the first 4h UTC boundary after `as_of`, including before REVIEW_DUE delivery.
   A new analyzed plan is required after that boundary.
3. Reject any existing account position or open ordinary/conditional order. Never
   adopt or close someone else's existing position as a new scenario trade.
4. Configure isolated BTC margin and 3x leverage (never automatically change the
   account's one-way/hedge or single/multi-asset mode).
5. Fetch live BTC filters, USDT wallet/available balance, actual taker commission,
   best bid/ask and mark. Quotes must be within 5 seconds; server/local clock skew
   must be <=1 second. Check entry quote is inside plan tolerance and TP/SL ticks
   are exact; never silently move the plan's levels.
6. Use `targets[0]` as the **only full-position TP** and `stop` as the SL.
   `add`, later targets and informational `_execution*` JSON metadata do not
   authorize orders. Require gross reward/risk >=1.5 at the executable best
   ask for longs or best bid for shorts, else skip. Fees and the configured
   slippage reserve do not alter this entry gate.
7. Round quantity down to live LOT_SIZE/MARKET_LOT_SIZE steps. Check minimum
   quantity/notional, maximum quantity and available initial margin plus fees.
8. Persist entry intent, then send MARKET once. Confirm actual fill/average.
9. Register STOP_MARKET first, then TAKE_PROFIT_MARKET, both on the Algo Order
   API with `closePosition=true`, `workingType=MARK_PRICE`, `priceProtect=false`.
   Do NOT combine `closePosition` with `quantity` or `reduceOnly`.
10. Recheck gross reward/risk from the actual average fill, the risk budget,
    configured adverse-slippage limit and liquidation distance.
    An execution outside the tolerance/risk contract is flattened; levels aren't
    widened. No averaging down, pyramiding, split exits, trailing or SL movement.

Sizing still reserves adverse entry slippage and adverse exit slippage (5 bps
each by default), plus undiscounted actual commission. Future referral rebates
are not counted in sizing or daily-loss accounting, but fees and reserves are
excluded from the reward/risk gate. The calculation works for longs and shorts:

```text
budget = min(wallet * 1%, remaining daily loss allowance)
worst_entry = executable quote moved adversely by configured slippage
loss_per_BTC = directional(worst_entry - SL) + entry/exit fees + SL slippage
quantity = floor_to_exchange_step(budget / loss_per_BTC)
gross_rr = directional(TP - executable_quote) / directional(executable_quote - SL)
```

The KST-day loss allowance is 2% of the smaller of that day's first observed wallet
balance and current wallet balance. Actual negative REALIZED_PNL, COMMISSION and
FUNDING_FEE cash flows since KST midnight consume it. Positive trades and referral
rebates do not replenish it. The next trade's planned risk must fit the remainder.
History is paginated; truncated or non-USDT costs fail closed. This is conservative
(e.g. gross losing trades plus commission), not an exact net daily-return metric.

**1% is a sizing budget, not a guaranteed loss cap.** MARKET and STOP_MARKET have
no price guarantee. Gaps, slippage beyond the reserve, funding, liquidation,
network failures and exchange rejection can cause larger losses. TP/SL registration
is not atomic with entry. There is a short unprotected interval after a fill.

## Recovery and account lifecycle

`execution-live.sqlite3` is an account-wide SQLite journal, independent of the
monitor's per-plan JSON. Every side-effect intent is durably committed first.
One plan_id allows at most one entry attempt across all A/B/C/D candidates, even
after closure or a same-ID plan edit. A failed/no-fill attempted plan is consumed.

- Entry timeouts/5xx: query the deterministic client order ID. **Never blindly
  resubmit a MARKET entry**, including after a crash between journaling and send.
  If acceptance cannot be established, leave ENTRY_PENDING, block all new plans,
  and alert ENTRY_UNCERTAIN. This can require manual reconciliation.
- Partial fills: cancel any remainder, confirm the terminal entry, protect the
  actual position with close-all exits. No automatic top-up.
- Protection response lost: query its clientAlgoId before deciding failure.
  No second conditional submission with the same uncertain intent.
- Missing, canceled or mismatched protection: attempt reduce-only MARKET flattening.
  Do not announce EMERGENCY_CLOSED until the position is zero and orders cleaned.
- Emergency timeout: query the close ID. No blind close retry. If a prior close
  is confirmed terminal and some owned position remains, submit the next
  reduce-only attempt. Keep any existing protection until flat is confirmed.
- Normal exit: confirm flat, inspect conditional child fills to classify TP/SL,
  cancel the sibling, verify cleanup, then permit a different new plan.
- Unknown/foreign position changes block new trading and alert; do not infer they
  belong to the bot. Never delete the journal to clear an uncertain order.

Recovery runs before public candle fetching and every cycle, **even after plan
expiry, REVIEW_DUE, PAUSE or a public-market outage**. Active positions keep the
original persisted exit levels when a new plan loads. No position is closed merely
because the monitor emits EXPIRED/INVALIDATED/MISSED. Those are signal states.

The reconciliation interval is the monitor poll interval (15 seconds by default).
Immediate protection is attempted in the entry handler. Exchange-hosted exits
continue if the process stops; sibling cleanup/recovery requires the process and
API connection. Do not switch to off/dry-run while a live position is open: those
modes do not service the live journal. PAUSE is the supported way to stop new
entries while retaining management. Bad/missing plan files still prevent startup;
repair them promptly rather than deleting position state.

Telegram execution notifications have their own durable outbox; sending them
never gates order protection. Delivery is at least once, so an ID may repeat after
a crash. ENTRY_READY is not proof of an order. Use ENTRY_FILLED, PROTECTION_SET,
ENTRY_SKIPPED, CLOSED_TP/SL/OTHER, EMERGENCY_PENDING/CLOSED and EXECUTION_ERROR.

## Deployment and verification

The existing workflow packages all four Python modules and validates them together.
The remote script stages/compiles before stopping the service, installs the set,
and rolls code back if startup fails. `.env`, plan JSON and data are preserved.
SQLite schema is versioned; this change uses version 1. The default mode remains
off so merging/deploying does not itself authorize live trading.

```bash
python3 -m unittest discover -s tests -p 'test_scenario*.py' -v
bash -n scenario_monitor/deploy_remote.sh
```

Tests use a fake exchange and cover request payloads, sizing/gates, accepted-but-
lost responses, partial fills, restart recovery, exit failure, sibling cleanup,
dry-run and notification failure. They do not prove live exchange execution,
strategy profitability, account permissions or production connectivity.

API reference checked September 8, 2026:
[Binance USD-M Trade REST API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade).
Use `/fapi/v1/order` for MARKET entries/reduce-only emergency exits and
`/fapi/v1/algoOrder` for TP/SL, query and cancellation.
