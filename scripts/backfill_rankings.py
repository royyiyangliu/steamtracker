"""
Steam 畅销榜历史排名回填脚本
- 逐周爬取 store.steampowered.com/charts/topsellers/global/{date}
- 自动点击 "See all 100" 展开完整榜单
- 跳过 rankings.json 中已有的周次
- 每 5 周增量保存一次，中途崩溃可续跑

用法（在项目根目录）:
    pip install playwright
    playwright install chromium
    python scripts/backfill_rankings.py
"""

import json
import datetime
import os
import sys
import time

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

START_DATE = "2023-01-02"   # 回填起始日（会自动对齐到周一）
SAVE_EVERY = 5              # 每爬 N 周保存一次
DELAY_SECS = 4              # 每页延迟（秒）


# ──────────────────────────────────────────
# 日期工具
# ──────────────────────────────────────────

def align_to_monday(date_str):
    d = datetime.date.fromisoformat(date_str)
    return d - datetime.timedelta(days=d.weekday())


def get_weeks_to_fill(start_str, existing_weeks):
    """返回从 start 到上周一（不含本周）的所有周一，跳过已有的"""
    start = align_to_monday(start_str)
    today = datetime.date.today()
    this_monday = today - datetime.timedelta(days=today.weekday())

    weeks = []
    cur = start
    while cur < this_monday:
        s = cur.isoformat()
        if s not in existing_weeks:
            weeks.append(s)
        cur += datetime.timedelta(weeks=1)
    return weeks


# ──────────────────────────────────────────
# Playwright 爬取
# ──────────────────────────────────────────

def scrape_week(page, week_date_str):
    """
    加载指定周的畅销榜，返回 [{'rank':1,'appid':'730','name':'...'}, ...]
    优先 API 拦截；失败则点击 See all 100 后 DOM 解析
    """
    url = f"https://store.steampowered.com/charts/topsellers/global/{week_date_str}"

    api_hits = []

    def on_response(response):
        if "steampowered.com" not in response.url:
            return
        if "application/json" not in response.headers.get("content-type", ""):
            return
        try:
            api_hits.append({"url": response.url, "data": response.json()})
        except Exception:
            pass

    page.on("response", on_response)

    try:
        page.goto(url, wait_until="networkidle", timeout=60000)
    except PWTimeout:
        pass  # 继续尝试

    try:
        page.wait_for_selector("a[href*='/app/']", timeout=20000)
    except PWTimeout:
        pass

    time.sleep(2)

    # 方法 1：API 拦截
    results = _parse_api_hits(api_hits)
    if len(results) >= 20:
        page.remove_listener("response", on_response)
        return results

    # 方法 2：点击 See all 100 后 DOM 解析
    try:
        # 按钮文字可能是英文或本地化，用宽松匹配
        btn = page.locator("button, a").filter(has_text="100").first
        if btn.count() and btn.is_visible(timeout=3000):
            btn.click()
            time.sleep(2)
    except Exception:
        pass

    results = page.evaluate("""
        () => {
            const out = [];
            const seen = new Set();
            document.querySelectorAll('a[href*="/app/"]').forEach(a => {
                const m = a.href.match(/\\/app\\/(\\d+)\\//);
                if (!m || seen.has(m[1])) return;
                const rect = a.getBoundingClientRect();
                if (rect.width < 60 && rect.height < 20) return;
                seen.add(m[1]);
                const name = (a.textContent || a.getAttribute('aria-label') || '').trim();
                out.push({ rank: out.length + 1, appid: m[1], name });
            });
            return out.slice(0, 100);
        }
    """)

    page.remove_listener("response", on_response)
    return results


def _parse_api_hits(api_hits):
    for hit in api_hits:
        data = hit["data"]
        candidates = []
        if isinstance(data, list):
            candidates = [data]
        elif isinstance(data, dict):
            for key in ("charts", "items", "games", "results", "ranks", "response", "data"):
                v = data.get(key)
                if isinstance(v, list) and len(v) >= 10:
                    candidates.append(v)
                    break
        for lst in candidates:
            if len(lst) < 10:
                continue
            parsed = []
            for i, item in enumerate(lst[:100]):
                if not isinstance(item, dict):
                    break
                appid = str(item.get("appid") or item.get("app_id") or "")
                if not appid:
                    break
                name = item.get("name") or item.get("title") or ""
                rank = item.get("rank", i + 1)
                parsed.append({"rank": int(rank), "appid": appid, "name": name})
            if len(parsed) >= 10:
                return parsed
    return []


# ──────────────────────────────────────────
# JSON 读写与重建
# ──────────────────────────────────────────

def load_rankings():
    path = "docs/rankings.json"
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"weeks": [], "data": {}, "snapshots": [], "last_updated": None}


def rebuild_and_save(rdata, new_results, games):
    """
    new_results: {week_str: [{'rank':N,'appid':'xxx','name':'...'}]}
    将新结果合并进 rdata，重建 weeks/data/snapshots，写入文件
    """
    # 合并所有快照
    snapshots_map = {s["week"]: s for s in rdata.get("snapshots", [])}
    for week, top100 in new_results.items():
        snapshots_map[week] = {
            "week": week,
            "top100": [{"rank": r["rank"], "appid": r["appid"], "name": r.get("name", "")}
                       for r in top100[:100]],
        }

    # 合并所有周次的 rank_map
    existing_rank_maps = {}
    for i, w in enumerate(rdata["weeks"]):
        existing_rank_maps[w] = {}
        for g in games:
            ranks = rdata["data"].get(g["name"], [])
            existing_rank_maps[w][str(g["appid"])] = ranks[i] if i < len(ranks) else None

    new_rank_maps = {}
    for week, top100 in new_results.items():
        new_rank_maps[week] = {item["appid"]: item["rank"] for item in top100}

    # 合并并排序
    all_weeks = sorted(set(existing_rank_maps) | set(new_rank_maps))
    new_data = {}
    for g in games:
        new_data[g["name"]] = []
        for w in all_weeks:
            if w in new_rank_maps:
                rank = new_rank_maps[w].get(str(g["appid"]))
            else:
                rank = existing_rank_maps.get(w, {}).get(str(g["appid"]))
            new_data[g["name"]].append(rank)

    rdata["weeks"] = all_weeks
    rdata["data"] = new_data

    # 快照：全量保留（回填完成后不截断，让 fetch_rankings.py 的滚动逻辑处理）
    rdata["snapshots"] = sorted(snapshots_map.values(), key=lambda x: x["week"])
    rdata["last_updated"] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    os.makedirs("docs", exist_ok=True)
    with open("docs/rankings.json", "w", encoding="utf-8") as f:
        json.dump(rdata, f, ensure_ascii=False, separators=(",", ":"))


# ──────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────

def main():
    if not os.path.exists("games_config.json"):
        print("错误：请在项目根目录运行")
        sys.exit(1)

    with open("games_config.json", encoding="utf-8") as f:
        config = json.load(f)
    games = config["games"]
    target_appids = {str(g["appid"]): g["name"] for g in games}

    rdata = load_rankings()
    existing_weeks = set(rdata["weeks"])

    weeks_to_fill = get_weeks_to_fill(START_DATE, existing_weeks)
    total = len(weeks_to_fill)

    print(f"已有数据：{len(existing_weeks)} 周")
    print(f"需回填：{total} 周（{weeks_to_fill[0] if weeks_to_fill else '-'} 至 {weeks_to_fill[-1] if weeks_to_fill else '-'}）\n")

    if not weeks_to_fill:
        print("无需回填，退出")
        return

    pending = {}   # 本批次待写入的结果
    failed = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = context.new_page()

        for i, week in enumerate(weeks_to_fill):
            print(f"[{i+1:3d}/{total}] {week} ...", end=" ", flush=True)

            try:
                top100 = scrape_week(page, week)
            except Exception as e:
                print(f"异常: {e}")
                failed.append(week)
                time.sleep(5)
                continue

            if not top100 or len(top100) < 10:
                print(f"数据不足 ({len(top100) if top100 else 0} 条)，跳过")
                failed.append(week)
                time.sleep(3)
                continue

            # 打印目标游戏命中情况
            hits = [target_appids[a] for item in top100
                    if (a := item["appid"]) in target_appids]
            rank_strs = [f"{target_appids[item['appid']]}#{item['rank']}"
                         for item in top100 if item["appid"] in target_appids]
            print(f"{len(top100)} 条  目标: {', '.join(rank_strs) if rank_strs else '无'}")

            pending[week] = top100

            # 每 SAVE_EVERY 周保存一次
            if len(pending) >= SAVE_EVERY:
                rebuild_and_save(rdata, pending, games)
                # 重新加载（rdata 已被 rebuild 更新，但为安全起见重读）
                rdata = load_rankings()
                pending = {}
                print(f"  → 进度已保存（累计 {len(rdata['weeks'])} 周）")

            time.sleep(DELAY_SECS)

        browser.close()

    # 保存剩余
    if pending:
        rebuild_and_save(rdata, pending, games)
        rdata = load_rankings()

    print(f"\n回填完成！共 {len(rdata['weeks'])} 周数据")
    if failed:
        print(f"失败/跳过的周次（可重跑）: {failed}")


if __name__ == "__main__":
    main()
