"""
Steam 每日峰值在线人数 - 数据更新脚本
- 从 steamcharts.com 获取各游戏最新数据
- 增量追加到 data/history.csv
- 生成前端使用的 docs/games.json
"""

import json
import csv
import urllib.request
import urllib.error
import datetime
import os
import time
import sys


def load_config():
    with open("games_config.json", encoding="utf-8") as f:
        return json.load(f)


def load_history(game_names):
    """
    读取 data/history.csv
    返回: (history_dict, fieldnames)
    history_dict: {date_str: {game_name: int_or_None}}
    """
    history = {}
    with open("data/history.csv", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            date = row["DateTime"][:10]  # 取 YYYY-MM-DD
            history[date] = {}
            for name in game_names:
                val = row.get(name, "").strip()
                history[date][name] = int(val) if val else None
    return history, fieldnames


def fetch_steamcharts(appid, retries=3):
    """获取 steamcharts 小时级在线数据，返回 [[timestamp_ms, count], ...]"""
    url = f"https://steamcharts.com/app/{appid}/chart-data.json"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except Exception as e:
            print(f"    尝试 {attempt + 1}/{retries} 失败: {e}")
            if attempt < retries - 1:
                time.sleep(5)
    return None


def compute_daily_peaks(raw_data, after_date):
    """
    从小时级数据中提取每日峰值（仅 after_date 之后、今天之前的完整日期）
    返回: {date_str: peak_count}
    """
    daily = {}
    today = datetime.date.today()

    for item in raw_data:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        ts_ms, count = item[0], item[1]
        if count is None or count == 0:
            continue
        try:
            dt = datetime.datetime.utcfromtimestamp(ts_ms / 1000).date()
        except (OSError, OverflowError):
            continue
        date_str = dt.isoformat()
        if date_str > after_date and dt < today:
            if date_str not in daily or count > daily[date_str]:
                daily[date_str] = int(count)

    return daily


def save_history(history, fieldnames, game_names):
    """将 history 写回 data/history.csv"""
    sorted_dates = sorted(history.keys())
    with open("data/history.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        writer.writerow(fieldnames)
        for date in sorted_dates:
            row_data = history[date]
            row = [f"{date} 00:00:00"] + [
                row_data.get(name) if row_data.get(name) is not None else ""
                for name in game_names
            ]
            writer.writerow(row)
    print(f"已更新 data/history.csv（共 {len(sorted_dates)} 行）")


def generate_games_json(history, config):
    """生成前端消费的 docs/games.json"""
    games = [g["name"] for g in config["games"]]
    sorted_dates = sorted(history.keys())

    # 每个游戏的数据数组与 dates 数组按索引对齐
    data = {g: [] for g in games}
    for date in sorted_dates:
        row = history[date]
        for g in games:
            data[g].append(row.get(g))  # None 表示该日期无数据

    output = {
        "games": games,
        "appids": {g["name"]: g["appid"] for g in config["games"]},
        "dates": sorted_dates,
        "data": data,
        "last_updated": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }

    os.makedirs("docs", exist_ok=True)
    with open("docs/games.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, separators=(",", ":"))

    size_kb = os.path.getsize("docs/games.json") / 1024
    print(f"已生成 docs/games.json（{len(sorted_dates)} 天，{len(games)} 款游戏，{size_kb:.0f} KB）")


def main():
    # 脚本必须从项目根目录运行
    if not os.path.exists("games_config.json"):
        print("错误：请在项目根目录下运行此脚本")
        sys.exit(1)

    config = load_config()
    game_names = [g["name"] for g in config["games"]]

    history, fieldnames = load_history(game_names)

    last_date = max(history.keys())
    today = datetime.date.today()
    yesterday = (today - datetime.timedelta(days=1)).isoformat()

    print(f"历史数据截止: {last_date}")
    print(f"今天: {today}\n")

    if last_date >= yesterday:
        print("数据已是最新，跳过抓取。")
    else:
        print(f"需要补充 {last_date} 到 {yesterday} 的数据\n")
        updated = False

        for game in config["games"]:
            name = game["name"]
            appid = game["appid"]
            print(f"正在获取 {name} (AppID: {appid})...")

            raw = fetch_steamcharts(appid)
            if raw is None:
                print(f"  跳过（获取失败）\n")
                time.sleep(2)
                continue

            daily = compute_daily_peaks(raw, last_date)

            if not daily:
                print(f"  无新数据\n")
            else:
                print(f"  新增 {len(daily)} 天：{sorted(daily.keys())[0]} ~ {sorted(daily.keys())[-1]}\n")
                for date_str, peak in daily.items():
                    if date_str not in history:
                        history[date_str] = {g: None for g in game_names}
                    history[date_str][name] = peak
                updated = True

            time.sleep(2)  # 避免频繁请求

        if updated:
            save_history(history, fieldnames, game_names)
        else:
            print("所有游戏均无新数据。")

    print()
    generate_games_json(history, config)
    print("\n完成！")


if __name__ == "__main__":
    main()
