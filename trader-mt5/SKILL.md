---
name: trader-mt5
description: >-
  MetaTrader 5 bridge. Connects to the user's running, logged-in MT5 terminal
  (any broker, e.g. Hantec Markets) through the official MetaTrader5 Python
  package and does the whole day-trade loop on forex and CFDs: account status
  (DEMO or REAL), contract specs, a multi-timeframe technical read (EMA
  20/50/200 stack, RSI, ATR, ADX/DI, prior-day pivots, H1 swings, bias
  score), a scan that ranks pairs by trend strength and cost, lot sizing to a
  money risk with the broker's own profit math, risk checks (max loss per
  trade, daily loss, trades per day, reward to risk, spread vs ATR, margin),
  tickets that need the user's explicit "sim" / "yes" one at a time, sending
  with order_check first, closing, moving stops and a read-only monitor. DEMO
  by default: a REAL account is refused unless the user sets allow_live.
  Use when the user says "MT5", "MetaTrader", "analisa EURUSD", "scan dos
  pares", "abre uma compra", "manda a ordem", "minhas posicoes", "fecha a
  posicao", "move o stop", "monitora", or names a forex pair.
argument-hint: "[comando] [simbolo]"
---

# trader-mt5

The MetaTrader 5 skill does for forex and CFDs what the rest of the pack does
for US stocks, against the user's own MT5 terminal. The terminal must be open
and logged in; the Python package talks to it on the same computer. No keys
or passwords ever pass through the chat or this tool.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
On Windows the interpreter is `python`.

```bash
python "${CLAUDE_SKILL_DIR}/mt5.py" status                               # DEMO/REAL, balance, Algo Trading on/off
python "${CLAUDE_SKILL_DIR}/mt5.py" symbols                              # specs of the config's symbols
python "${CLAUDE_SKILL_DIR}/mt5.py" scan --tf M15                        # rank the pairs
python "${CLAUDE_SKILL_DIR}/mt5.py" analyze EURUSD --tf M15              # full read of one pair
python "${CLAUDE_SKILL_DIR}/mt5.py" plan EURUSD --side buy --sl-atr 1.5 --rr 2 --why "pullback EMA20 M15 a favor do H1"
python "${CLAUDE_SKILL_DIR}/mt5.py" tickets
python "${CLAUDE_SKILL_DIR}/mt5.py" approve --index 1 --reply "sim"
python "${CLAUDE_SKILL_DIR}/mt5.py" send --index 1
python "${CLAUDE_SKILL_DIR}/mt5.py" positions
python "${CLAUDE_SKILL_DIR}/mt5.py" modify --ticket 123456 --sl 1.08420 --reply "sim"
python "${CLAUDE_SKILL_DIR}/mt5.py" close --ticket 123456 --reply "sim"
python "${CLAUDE_SKILL_DIR}/mt5.py" history --days 1
python "${CLAUDE_SKILL_DIR}/mt5.py" watch --every 30 --minutes 60        # read only, run in the background
```

## Setup

1. `python -m pip install MetaTrader5` (Windows; it brings numpy).
2. Copy `mt5_config.example.json` to `trader/mt5_config.json` and set the
   limits in the account's currency: `risk_per_trade`, `max_daily_loss`,
   `max_trades_per_day`, `min_reward_risk`, `max_lots`. `terminal_path`
   points at the broker's `terminal64.exe`.
3. Open MT5, log in to the **demo** account, and turn on the **Algo Trading**
   button in the toolbar (without it, `send` is refused by the terminal).
4. `mt5.py status` must say `Conta DEMO`.

## Steps: one trade

1. `scan`, then `analyze` the best pair. Explain the read in plain words:
   the bias across D1/H4/H1/M15/M5, ADX strength, RSI extremes, where price
   sits against pivots and swings, and the cost (spread vs ATR).
2. Agree a setup with the user. `plan` sizes it to `risk_per_trade` with the
   broker's own `order_calc_profit`, so the loss at the stop is in the
   account's money, and runs every check. A BLOCK is final for that ticket.
3. Show the ticket and ask: "Envio este ticket? Responda sim ou nao." One
   ticket per question. Never "sim para todos".
4. `approve --index N --reply "<the user's reply, word for word>"`. If it
   prints "Nao registrado", ask again. Never turn a reply into a yes yourself.
5. Only after "Registrado seu sim", `send --index N`. It re-reads the price,
   refuses if the stop or target was crossed or the risk grew more than 20 %,
   runs `order_check`, then `order_send` with the stop and target attached.
6. Start `watch` in the background so stops, targets and the daily limit are
   reported. `watch` never trades.

## Closing and moving the stop

`close` and `modify` act on an open position and also need the user's own yes
in `--reply`. `modify` refuses to move a stop further away (more risk) unless
the user asks for it and `--widen` is passed. Moving a stop to breakeven or
closer is always allowed.

## Files

| file | what |
| --- | --- |
| `trader/mt5_config.json` | limits; `allow_live` stays false unless the user sets it |
| `trader/mt5/orders.json` | every ticket `plan` wrote, with its checks |
| `trader/mt5/log.json` | planned, approved, declined, sent, failed, closed, modified (append only) |

## What it never does

- Never sends an order without a PASS and the user's explicit yes for that one ticket.
- Never sends to a REAL account while `allow_live` is false, and never edits that flag.
- Never trades from `watch`, a schedule or a background task: nobody is there to say yes.
- Never retries a failed order without a new `plan` and a new yes.
- Never asks for, reads or stores the MT5 password.

## Safety

- Not financial advice. The analysis is arithmetic on past candles; a PASS is a rules check, not a recommendation.
- Forex and CFDs are leveraged: a small move can cost more than expected. The loss shown is at the stop price;
  a gap or a fast market can fill worse.
- Demo first. Going live is the user's own decision, made by editing their own config.
