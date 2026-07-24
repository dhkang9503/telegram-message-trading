from pathlib import Path

path = Path("live/live_bot.py")
text = path.read_text(encoding="utf-8")

old_actions = '    "OPEN_LONG", "OPEN_SHORT", "ADD", "SET_STOP", "SET_TP",'
new_actions = '    "OPEN_LONG", "OPEN_SHORT", "OPEN_REENTRY", "ADD", "SET_STOP", "SET_TP",'
if old_actions not in text:
    raise SystemExit("allowed-action line not found")
text = text.replace(old_actions, new_actions, 1)

open_start = text.index("    async def open(\n")
open_end = text.index("\n    async def add(", open_start)
new_open = '''    async def open(
        self,
        action_type: str,
        raw_price: Any,
        message_id: int,
        index: int,
    ) -> dict[str, Any]:
        if D(self.state.data["position"]["total_qty"]) > 0:
            raise BotError("OPEN rejected: position already exists")
        if self.state.data["pending"]["entry_order"]:
            raise BotError("OPEN rejected: entry order already pending")

        stopped = self.state.data.get("stopped_position")
        use_reentry = bool(
            stopped
            and stopped.get("available_for_reentry")
            and stopped.get("side") in {"long", "short"}
            and stopped.get("stop_order_id")
            and D(stopped.get("total_margin_usdt")) > 0
        )

        if action_type == "OPEN_REENTRY":
            if not use_reentry:
                raise BotError("REENTRY rejected: no verified SL-closed position is available")
            position_side = str(stopped["side"])
        elif action_type == "OPEN_LONG":
            position_side = "long"
        elif action_type == "OPEN_SHORT":
            position_side = "short"
        else:
            raise BotError(f"Unsupported open action: {action_type}")

        if use_reentry:
            margin = D(stopped.get("total_margin_usdt"))
            sizing_source = "previous_sl_margin_usdt"
        else:
            margin = INITIAL_MARGIN_USDT
            sizing_source = "fixed_1_usdt"

        reference = await self.reference_price()
        notional = margin * LEVERAGE
        if notional < self.config.min_trade_usdt:
            raise BotError(
                f"{sizing_source} gives {notional} USDT notional, "
                f"below minimum {self.config.min_trade_usdt}"
            )
        qty = self.config.floor_size(notional / reference)
        return await self.place_opening_order(
            kind="entry",
            side="buy" if position_side == "long" else "sell",
            qty=qty,
            raw_price=raw_price,
            message_id=message_id,
            index=index,
            entry_sizing={
                "source": sizing_source,
                "margin_usdt": ds(margin),
                "use_reentry": use_reentry,
            },
        )
'''
text = text[:open_start] + new_open.rstrip() + text[open_end:]

dispatch_start = text.index('        if kind == "OPEN_LONG":\n')
dispatch_end = text.index('        if kind == "ADD":\n', dispatch_start)
new_dispatch = '''        if kind in {"OPEN_LONG", "OPEN_SHORT", "OPEN_REENTRY"}:
            return await self.open(kind, price, message_id, index)
'''
text = text[:dispatch_start] + new_dispatch + text[dispatch_end:]

path.write_text(text, encoding="utf-8")
