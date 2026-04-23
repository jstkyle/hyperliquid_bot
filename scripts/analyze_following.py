import sqlite3
import statistics

DB_PATH = '/home/ubuntu/hyperliquid_bot/copybot_state.db'

def safe_float(val):
    try:
        return float(val)
    except (TypeError, ValueError, TypeError):
        return None

def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM copy_history WHERE follower_status='filled'")
    rows = cursor.fetchall()
    
    if not rows:
        print("No filled trades in copy_history.")
        return
        
    print(f"Total filled copy trades: {len(rows)}")
    
    slippages_bps = []
    ratios = []
    
    # Let's break down by coin as well
    coin_stats = {}

    for r in rows:
        lp = safe_float(r['leader_price'])
        fp = safe_float(r['follower_price'])
        ls = safe_float(r['leader_size'])
        fs = safe_float(r['follower_size'])
        side = r['follower_side'].lower()
        coin = r['coin']
        
        if coin not in coin_stats:
            coin_stats[coin] = {'slippages_bps': [], 'ratios': [], 'count': 0}
            
        coin_stats[coin]['count'] += 1
        
        if lp and fp and lp > 0:
            if side == 'buy':
                slip = (fp - lp) / lp * 10000
            elif side == 'sell':
                slip = (lp - fp) / lp * 10000
            else:
                slip = 0
            
            slippages_bps.append(slip)
            coin_stats[coin]['slippages_bps'].append(slip)
            
        if ls and fs and ls > 0:
            ratio = fs / ls
            ratios.append(ratio)
            coin_stats[coin]['ratios'].append(ratio)
            
    print("\n--- Overall Following Performance ---")
    if slippages_bps:
        print(f"Average Slippage (bps): {statistics.mean(slippages_bps):.2f} bps")
        print(f"Median Slippage (bps):  {statistics.median(slippages_bps):.2f} bps")
        print(f"Max Slippage (bps):     {max(slippages_bps):.2f} bps")
        print(f"% Trades with Positive Slippage (Worse Price): {sum(1 for s in slippages_bps if s > 0) / len(slippages_bps) * 100:.2f}%")
        print(f"% Trades with Negative Slippage (Better Price): {sum(1 for s in slippages_bps if s < 0) / len(slippages_bps) * 100:.2f}%")
        print(f"% Trades with Zero Slippage (Same Price): {sum(1 for s in slippages_bps if s == 0) / len(slippages_bps) * 100:.2f}%")

    if ratios:
        print(f"\nAverage Size Ratio (Follower/Leader): {statistics.mean(ratios):.5f}")
        print(f"Min Size Ratio: {min(ratios):.5f}")
        print(f"Max Size Ratio: {max(ratios):.5f}")
        if len(ratios) > 1:
            print(f"Std Dev Size Ratio: {statistics.stdev(ratios):.5f} (Lower = more consistent following)")
        
    print("\n--- Breakdown By Coin ---")
    for coin, stats in coin_stats.items():
        print(f"\n{coin} ({stats['count']} trades):")
        if stats['slippages_bps']:
            print(f"  Avg Slippage: {statistics.mean(stats['slippages_bps']):.2f} bps")
        if stats['ratios']:
            print(f"  Avg Size Ratio: {statistics.mean(stats['ratios']):.5f}")
            if len(stats['ratios']) > 1:
                print(f"  Ratio Std Dev:  {statistics.stdev(stats['ratios']):.5f}")
            
if __name__ == "__main__":
    main()
