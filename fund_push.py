#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云端每日基金回撤推送脚本（配合 GitHub Actions 定时运行）。
抓东方财富基金全量历史净值 -> 计算 1/2/3 年回撤 -> 推 Server 酱到微信。

回撤率（按用户定义）= 当天净值 / 某周期内的最大净值
  （另附：较区间最高点的真实回落 = 1 - 回撤率）

配置通过环境变量传入：
  SENDKEY        Server 酱 SendKey（密钥，GitHub Secrets）
  FUND_CODE      基金代码，如 161725 / 110011
  FUND_NAME      显示名（可空，默认取接口基金名）
  FUND_NAV_TYPE  unit=单位净值 / cumulative=累计净值（默认 cumulative，更贴近真实回撤）
  FUND_PERIODS   回撤周期，逗号分隔，默认 1,2,3（如基金不满3年可填 1,2）；
                填 all 表示「成立以来的回撤」，如 1,2,all

改进点（相对原版）：
  - 净值时间戳统一按北京时间解析，避免 GitHub Actions（UTC）下日期少一天
  - 仅请求一次接口，同时解析基金名与净值历史（减少被限频概率）
  - Server 酱 desp 用 Markdown 硬换行，保证每行独立显示
  - 累计净值缺失时回退到单位净值；增加简单重试，提升定时任务稳定性
"""
import os
import re
import sys
import json
import time
from datetime import datetime, timedelta, timezone

try:
    import requests
except ImportError:
    sys.exit("❌ 未安装 requests，请先执行: pip install requests")

PERIODS = [365, 730, 1095]  # 默认 1年 / 2年 / 3年（自然日），可用 FUND_PERIODS 覆盖
BJ = timezone(timedelta(hours=8))  # 东方财富净值时间戳按北京时间计


def _ts_to_bj(ms):
    """毫秒时间戳 -> 北京时间 datetime（带时区，避免 UTC runner 下日期偏移）。"""
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).astimezone(BJ)


def _get_text(code, retries=3):
    """带重试地拉取 pingzhongdata JS 文本。"""
    url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://fund.eastmoney.com/"}
    last_err = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=20)
            r.encoding = "utf-8"
            if r.status_code == 200 and r.text.strip():
                return r.text
            last_err = RuntimeError(f"HTTP {r.status_code}")
        except Exception as e:  # 网络抖动
            last_err = e
        if i < retries - 1:
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"拉取基金数据失败（重试 {retries} 次）：{code} -> {last_err}")


def fetch_nav_history(code, nav_type="cumulative"):
    """返回 [(datetime, 净值), ...] 全量历史，按日期升序。
    优先用累计净值 LJJZ（分红再投，推荐真实回撤）；缺失则回退单位净值 DWJZ。
    """
    text = _get_text(code)
    m_name = re.search(r'fS_name\s*=\s*"(.*?)"', text)
    name = m_name.group(1) if m_name else code

    arr = None
    used = None
    want_cumulative = nav_type in ("cumulative", "acc", "累计净值")
    if want_cumulative:
        m = re.search(r"Data_ACWorthTrend\s*=\s*(\[.*?\]);", text, re.S)
        if m:
            arr = json.loads(m.group(1))
            used = "累计净值"
    if arr is None:  # 回退：单位净值
        m = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", text, re.S)
        if not m:
            raise RuntimeError("未解析到净值数据，请检查基金代码：" + code)
        arr = json.loads(m.group(1))
        used = "单位净值" if not want_cumulative else "单位净值（累计净值不可用，已回退）"
        hist = [(_ts_to_bj(it["x"]), float(it["y"])) for it in arr]
    else:
        hist = [(_ts_to_bj(it[0]), float(it[1])) for it in arr]

    if not hist:
        raise RuntimeError("净值历史为空：" + code)
    hist.sort(key=lambda x: x[0])
    return hist, name, used


def compute_drawdowns(hist, now=None, periods=None):
    """按用户定义计算各周期回撤率。返回列表含 {label, years, current, peak, ratio, drawdown, current_date}。
    周期 days：>0 为「近 N 年」自然日窗口；0 表示「成立以来」（= 全量历史）。
    结果为升序，成立以来(0) 自动排最后。
    """
    now = now or datetime.now(BJ)  # now 也用北京时间，与 hist 一致
    periods = periods or PERIODS
    periods = sorted(periods, key=lambda d: d if d else float("inf"))  # 成立以来放最后
    current_date, current_nav = hist[-1]
    results = []
    for days in periods:
        if days and days > 0:
            cutoff = now - timedelta(days=days)
            window = [(d, v) for d, v in hist if d >= cutoff]
            label = f"近{days // 365}年"
        else:
            window = list(hist)             # 成立以来 = 全量历史
            label = "成立来"
        if not window:
            continue
        peak = max(v for _, v in window)
        ratio = current_nav / peak if peak > 0 else 0.0
        results.append({
            "label": label,
            "years": days // 365,
            "current": current_nav,
            "peak": peak,
            "ratio": ratio,                 # 用户定义的回撤率 = 当前/峰值
            "drawdown": 1 - ratio,          # 真实从峰值回落比例
            "current_date": current_date,
        })
    return results


def main():
    sendkey = os.environ.get("SENDKEY")
    if not sendkey:
        sys.exit("❌ 未设置环境变量 SENDKEY（请在 GitHub 仓库 Settings -> Secrets 中配置）")
    code = os.environ.get("FUND_CODE")
    if not code:
        sys.exit("❌ 未设置环境变量 FUND_CODE（如 161725）")
    name = os.environ.get("FUND_NAME")
    nav_type = os.environ.get("FUND_NAV_TYPE") or "cumulative"
    periods_raw = os.environ.get("FUND_PERIODS") or "1,2,3"
    periods = []
    for x in periods_raw.split(","):
        x = x.strip()
        if not x:
            continue
        if x.lower() in ("all", "since", "成立以来", "成立来"):
            periods.append(0)              # 0 = 成立以来（全量历史）
        else:
            try:
                periods.append(int(x) * 365)
            except ValueError:
                sys.exit("❌ FUND_PERIODS 格式错误，应为逗号分隔的年份或 all，如 1,2,all")
    if not periods:
        sys.exit("❌ FUND_PERIODS 为空，请填写如 1,2,all")

    hist, api_name, used = fetch_nav_history(code, nav_type)
    name = name or api_name
    results = compute_drawdowns(hist, periods=periods)
    if not results:
        sys.exit("❌ 净值历史不足，无法计算回撤")

    cur = results[0]["current_date"].strftime("%Y-%m-%d")
    cur_nav = results[0]["current"]

    lines = [
        "📉 基金回撤播报",
        f"基金：**{name}** ({code})",
        f"日期：{cur}　当前净值：{cur_nav:.4f}（{used}）",
        "",
    ]
    for r in results:
        ratio_pct = r["ratio"] * 100
        dd_pct = r["drawdown"] * 100
        lines.append(
            f"- {r['label']}：峰值 {r['peak']:.4f} → "
            f"回撤率 **{ratio_pct:.1f}%**（较峰值回落 {dd_pct:.1f}%）"
        )
    # 用 Markdown 硬换行（行尾两空格）保证每行独立，Server 酱按 Markdown 渲染
    content = "  \n".join(lines)
    title = f"基金回撤 {name} 当前{cur_nav:.4f}"

    # 推送
    push_url = "https://sctapi.ftqq.com/" + sendkey + ".send"
    resp = requests.post(push_url, data={"title": title, "desp": content}, timeout=15)
    res = resp.json()
    if res.get("code") != 0:
        sys.exit("❌ 推送失败：" + json.dumps(res, ensure_ascii=False))

    print("✅ 已推送：" + title)
    print(content)


if __name__ == "__main__":
    main()
