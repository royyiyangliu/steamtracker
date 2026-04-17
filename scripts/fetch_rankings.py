"""
Steam 全球畅销榜 - 周度排名抓取脚本
- 用 Playwright 渲染 store.steampowered.com/charts/topselling/global
- 优先拦截页面内部 API 响应；失败则降级 DOM 解析
- 更新 docs/rankings.json
"""

import json
import re
import datetime
import os
import sys
import time

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


def load_config():
    with open("games_config.json", encoding="utf-8") as f:
        return json.load(f)


def get_week_start():
    """本周一日期，作为该周数据的 key"""
    today = datetime.date.today()
    return (today - datetime.timedelta(days=today.weekday())).isoformat()


def scrape_top100(week):
    """
    返回 [{'rank': 1, 'appid': '730', 'name': 'Counter-Strike 2'}, ...]
    week: 本周周一日期字符串，如 '2026-04-13'
    """
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

        # ── 拦截所有 JSON 响应，寻找排名数据 ──
        api_hits = []

        def on_response(response):
            if "steampowered.com" not in response.url:
                return
            ct = response.headers.get("content-type", "")
            if "application/json" not in ct:
                return
            try:
                data = response.json()
                api_hits.append({"url": response.url, "data": data})
            except Exception:
                pass

        page.on("response", on_response)

        # 用带日期的周度快照 URL，避免抓到实时滚动数据
        url = f"https://store.steampowered.com/charts/topsellers/global/{week}"
        print(f"加载 Steam Charts 页面：{url}")
        try:
            page.goto(url, wait_until="networkidle", timeout=60000)
        except PWTimeout:
            print("  networkidle 超时，继续尝试...")

        # 等待游戏条目出现
        try:
            page.wait_for_selector("a[href*='/app/']", timeout=20000)
        except PWTimeout:
            print("  等待 app 链接超时")

        time.sleep(2)  # 确保 JS 完成渲染

        # ── 方法 1：从拦截的 API 响应提取 ──
        results = _parse_api_hits(api_hits)
        if results:
            print(f"  方法1（API 拦截）: 获取 {len(results)} 条")
            browser.close()
            return results

        # ── 方法 2：点击 "See all 100" 后 DOM 解析 ──
        print("  API 未命中，改用 DOM 解析...")
        try:
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
                // 收集所有带 /app/XXXXX/ 的链接
                document.querySelectorAll('a[href*="/app/"]').forEach(a => {
                    const m = a.href.match(/\\/app\\/(\\d+)\\//);
                    if (!m || seen.has(m[1])) return;
                    // 过滤掉太小的（导航链接等）
                    const rect = a.getBoundingClientRect();
                    if (rect.width < 60 && rect.height < 20) return;
                    seen.add(m[1]);
                    const name = (a.textContent || a.getAttribute('aria-label') || '').trim();
                    out.push({ rank: out.length + 1, appid: m[1], name });
                });
                return out.slice(0, 100);
            }
        """)
        print(f"  方法2（DOM）: 获取 {len(results)} 条")

        browser.close()
        return results


def _parse_api_hits(api_hits):
    """从拦截的 API 响应中解析出排名列表"""
    for hit in api_hits:
        data = hit["data"]
        candidates = []

        # 尝试各种可能的数据结构
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


def load_rankings():
    path = "docs/rankings.json"
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"weeks": [], "data": {}, "snapshots": [], "last_updated": None}


def save_rankings(rdata):
    os.makedirs("docs", exist_ok=True)
    with open("docs/rankings.json", "w", encoding="utf-8") as f:
        json.dump(rdata, f, ensure_ascii=False, separators=(",", ":"))
    print("已保存 docs/rankings.json")


def main():
    if not os.path.exists("games_config.json"):
        print("错误：请在项目根目录运行")
        sys.exit(1)

    config = load_config()
    games = config["games"]
    target_appids = {str(g["appid"]): g["name"] for g in games}

    week = get_week_start()
    print(f"本周标识: {week}\n")

    # ── 抓取 ──
    top100 = scrape_top100(week)

    if not top100:
        print("未获取到有效数据，退出")
        sys.exit(1)

    # 打印 Top 10 概览
    print(f"\nTop 10（含目标标记）:")
    for item in top100[:10]:
        mark = " ◀" if item["appid"] in target_appids else ""
        print(f"  #{item['rank']:3d}  {item.get('name','')}{mark}")

    rank_map = {item["appid"]: item["rank"] for item in top100}

    # ── 更新 rankings.json ──
    rdata = load_rankings()

    # 保证每个游戏都有数组
    for g in games:
        if g["name"] not in rdata["data"]:
            rdata["data"][g["name"]] = [None] * len(rdata["weeks"])

    # 找或创建本周条目
    if week in rdata["weeks"]:
        idx = rdata["weeks"].index(week)
        print(f"\n覆盖本周已有数据 (idx={idx})")
    else:
        rdata["weeks"].append(week)
        idx = len(rdata["weeks"]) - 1
        for g in games:
            rdata["data"][g["name"]].append(None)
        print(f"\n追加本周新数据 (idx={idx})")

    # 写入各游戏排名
    print("\n目标游戏排名:")
    found = 0
    for g in games:
        rank = rank_map.get(str(g["appid"]))
        rdata["data"][g["name"]][idx] = rank
        status = f"#{rank}" if rank else "不在 Top 100"
        print(f"  {g['name']}: {status}")
        if rank:
            found += 1

    print(f"\n覆盖率: {found}/{len(games)}")

    # 完整 Top 100 快照（保留最近 52 周）
    snapshot = {
        "week": week,
        "top100": [
            {"rank": r["rank"], "appid": r["appid"], "name": r.get("name", "")}
            for r in top100[:100]
        ],
    }
    rdata["snapshots"] = [s for s in rdata.get("snapshots", []) if s["week"] != week]
    rdata["snapshots"].append(snapshot)
    rdata["snapshots"] = sorted(rdata["snapshots"], key=lambda x: x["week"])

    rdata["last_updated"] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    save_rankings(rdata)
    print("\n完成！")


if __name__ == "__main__":
    main()
