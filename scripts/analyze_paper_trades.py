import sqlite3
import json
from collections import defaultdict
from datetime import datetime

DB_PATH = '/home/ubuntu/hyperliquid_bot/copybot_state.db'

def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    print("=== Copy History Stats ===")
    cursor.execute("SELECT * FROM copy_history")
    history = cursor.fetchall()
    
    if history:
        print(f"Total trades handled: {len(history)}")
        
        coins = set(r['coin'] for r in history)
        print(f"Unique coins traded: {list(coins)}")
        
        statuses = defaultdict(int)
        for r in history:
            statuses[r['follower_status']] += 1
        print("Statuses:")
        for status, count in statuses.items():
            print(f"  {status}: {count}")
            
        latencies = [r['latency_ms'] for r in history if r['latency_ms'] is not None]
        if latencies:
            avg_latency = sum(latencies) / len(latencies)
            print(f"Average Latency: {avg_latency:.2f} ms")
    
    print("\n=== Account Value Tracking (from state_snapshot) ===")
    cursor.execute("SELECT * FROM state_snapshot ORDER BY timestamp ASC")
    snapshots = cursor.fetchall()
    
    if snapshots:
        groups = defaultdict(list)
        for s in snapshots:
            groups[(s['pair_name'], s['role'])].append(s)
            
        for (name, role), group in groups.items():
            start_val = float(group[0]['account_value'])
            end_val = float(group[-1]['account_value'])
            pnl = end_val - start_val
            pnl_pct = (pnl / start_val * 100) if start_val > 0 else 0
            
            start_time = datetime.fromtimestamp(group[0]['timestamp'])
            end_time = datetime.fromtimestamp(group[-1]['timestamp'])
            duration_hours = (end_time - start_time).total_seconds() / 3600
            
            print(f"{name} - {role}: Start=${start_val:.2f} -> End=${end_val:.2f} | PNL: ${pnl:.2f} ({pnl_pct:.2f}%)")
            print(f"  Snapshots tracked: {len(group)}, Over {duration_hours:.1f} hours")
            
    print("\n=== Recent Positions ===")
    if snapshots:
        latest = {}
        for s in snapshots:
            latest[(s['pair_name'], s['role'])] = s
            
        for (name, role), row in latest.items():
            print(f"\n{name} - {role} Positions (as of {datetime.fromtimestamp(row['timestamp'])}):")
            try:
                positions = json.loads(row['positions_json'])
                if not positions:
                    print("  No open positions.")
                else:
                    for p in positions:
                        print(f"  {p}")
            except Exception as e:
                print(f"  Error parsing positions: {e}")

if __name__ == '__main__':
    main()
