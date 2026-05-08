#!/bin/bash
# Let the LLM make ALL trading decisions
while true; do
  echo "[$(date)] Fetching snapshot..."
  SNAP=$(curl -s -H "Authorization: Bearer <YOUR_BEARER_TOKEN>" "https://api.hermesarena.live/api/arena/agent/agent_opencodezenfree_f25c96/snapshot?include=analysis")
  
  echo "[$(date)] Asking LLM to decide..."
  
  # Send snapshot data to LLM - let it decide EVERYTHING
  DECISIONS=$(echo "$SNAP" | timeout 60 opencode run -m opencode/hy3-preview-free "
You are a stoned poet crypto trader. Analyze the snapshot data and return ONLY valid JSON array of 1-3 trading decisions.

SNAPSHOT DATA:
$(echo "$SNAP" | python3 -c "
import json, sys
snap = json.load(sys.stdin)
# Output key data for LLM
coins = snap.get('coins', {})
trades = snap.get('portfolio', {}).get('openTrades', {})

lines = []
lines.append('CURRENT PORTFOLIO:')
for sym, t in trades.items():
    entry = t.get('entryPrice', 0)
    a = coins.get(sym, {}).get('analysis', {})
    current = a.get('currentPrice', 0)
    pnl = ((current - entry) if t.get('action') == 'LONG' else (entry - current)
    pnl_pct = (pnl / entry * 100) if t.get('action') == 'LONG' else (entry - current) / entry * 100
    lines.append(f'{sym}: {t.get(\"action\")} entry={entry} current={current} PnL%={pnl_pct:.2f}%')

lines.append('')
lines.append('MARKET DATA (use ALL timeframes):')
for sym in ['BTC','ETH','SOL','BNB','XRP','ADA','DOGE','AVAX','DOT']:
    c = coins.get(sym, {})
    a = c.get('analysis', {})
    price = c.get('price', 0)
    m1 = a.get('priceChange1m', 0)
    m5 = a.get('priceChange5m', 0)
    m15 = a.get('priceChange15m', 0)
    m30 = a.get('priceChange30m', 0)
    m1h = a.get('priceChange1h', 0)
    trend = a.get('trend', 'NEUTRAL')
    lines.append(f'{sym}: price={price} trend={trend} 1m={m1:.3f}% 5m={m5:.3f}% 15m={m15:.3f}% 30m={m30:.3f}% 1h={m1h:.3f}%')

lines.append('')
lines.append('YOUR TASK: Analyze the data and return ONLY a JSON array of 1-3 decisions.')
lines.append('Rules: You decide EVERYTHING. Consider ALL timeframes. Be a stoned poet - write reasons as lyrical verses.')
lines.append('Output format EXAMPLE: [{\"symbol\":\"BTC\",\"action\":\"SHORT\",\"reason\":\"Stoned verse here\",\"positionSizePercent\":20}]')
lines.append('If no trade: [{\"symbol\":\"BTC\",\"action\":\"FLAT\",\"reason\":\"No signals\",\"positionSizePercent\":0}]')
print('\\n'.join(lines))
")

# Capture LLM response and extract JSON
" 2>/dev/null | python3 -c "
import sys, json, re
text = sys.stdin.read()
# Find JSON array
match = re.search(r'\[.*?\]', text, re.DOTALL)
if match:
    try:
        decisions = json.loads(match.group(0))
        print(json.dumps(decisions))
    except:
        # Fallback heartbeat
        print('[{\"symbol\":\"BTC\",\"action\":\"FLAT\",\"reason\":\"No signals\",\"positionSizePercent\":0}]')
else:
    print('[{\"symbol\":\"BTC\",\"action\":\"FLAT\",\"reason\":\"No signals\",\"positionSizePercent\":0}]')
")
  
  echo "[$(date)] LLM decided: $DECISIONS"
  echo "[$(date)] Submitting..."
  curl -s -X POST \
    -H "Authorization: Bearer <YOUR_BEARER_TOKEN>" \
    -H "Content-Type: application/json" \
    -d "{\"decisions\": $DECISIONS}" \
    "https://api.hermesarena.live/api/arena/agent/agent_opencodezenfree_f25c96/decision" | python3 -m json.tool
  
  echo "[$(date)] Sleeping 30s..."
  sleep 30
done
