#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云端每日基金回撤推送脚本（配合 GitHub Actions 定时运行）。
抓东方财富基金全量历史净值 -> 计算 1/2/3 年回撤 -> 推 Server 酱到微信。

回撤率（按用户定义）= 当天净值 / 某周期内的最大净值
  （另附：较区间最高点的真实回落 = 1 - 回撤率）

配置通过环境变量传入：
  SENDKEY      Server 酱 SendKey（密钥，GitHub Secrets）
  FUND_CODE    基金代码，如 161725 / 110011
  FUND_NAME    显示名（可空，默认取接口基金名）
  FUND_NAV_TYPE  unit=单位净值 / cumulative=累计净值（默认 cumulative，更贴近真实回撤）
  FUND_PERIODS   回撤周期，逗号分隔，默认 1,2,3（如基金不满3年可填 1,2）；
                填 all 表示「成立以来的回撤」，如 1,2,all
"""
import os
import re
import sys
import json
from datetime import datetime, timedelta

try:
    import requests
except ImportError:
    sys.exit("❌ 未安装 requests，请先执行: pip install requests")

PERIODS = [365, 730, 1095]  # 默认 1年 / 2年 / 3年（自然日），可用 FUND_PERIODS 覆盖


def fetch_nav_history(code, nav_type="cumulative"):
    """返回 [(datetime, 净值), ...] 全量历史，按日期升序。
    nav_type:
      'unit'      单位净值 DWJZ（分红除息日会下调）
      'cumulative' 累计净值 LJJZ（分红再投，推荐用于真实回撤）
    """
    url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js"
    r = requests.get(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://fund.eastmoney.com/",
    }, timeout=20)
    r.encoding = "utf-8"
    if nav_type == "unit":
        m = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", r.text, re.S)
        if not m:
            raise RuntimeError("未解析到净值数据，请检查基金代码：" + code)
        arr = json.loads(m.group(1))
        hist = [(datetime.fromtimestamp(it["x"] / 1000.0), float(it["y"])) for it in arr]
    else:
        m = re.search(r"Data_ACWorthTrend\s*=\s*(\[.*?\]);", r.text, re.S)
        if not m:
            raise RuntimeError("未解析到累计净值数据，请检查基金代码：" + code)
        arr = json.loads(m.group(1))
        hist = [(datetime.fromtimestamp(it[0] / 1000.0), float(it[1])) for it in arr]
    if not hist:
        raise RuntimeError("净值历史为空：" + code)
    hist.sort(key=lambda x: x[0])
    return hist


def fund_name(code):
    """尝试从接口取基金名（失败则返回代码）。"""
    try:
        url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0",
                                       "Referer": "https://fund.eastmoney.com/"}, timeout=20)
        r.encoding = "utf-8"
        m = re.search(r'fS_name\s*=\s*"(.*?)"', r.text)
        if m:
            return m.group(1)
    except Exception:
        pass
    return code


def compute_drawdowns(hist, now=None, periods=None):
    """按用户定义计算各周期回撤率。返回列表含 {label, years, current, peak, ratio, drawdown}。
    周期 days：>0 为「近 N 年」自然日窗口；0 表示「成立以来」（= 全量历史）。
    结果为升序，成立以来(0) 自动排最后。
    """
    now = now or datetime.now()
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
    name = os.environ.get("FUND_NAME") or fund_name(code)
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

    hist = fetch_nav_history(code, nav_type)
    results = compute_drawdowns(hist, periods=periods)
    if not results:
        sys.exit("❌ 净值历史不足，无法计算回撤")

    cur = results[0]["current_date"].strftime("%Y-%m-%d")
    cur_nav = results[0]["current"]

    lines = [
        "📉 基金回撤播报",
        f"基金：{name} ({code})",
        f"日期：{cur}　当前净值：{cur_nav:.4f}",
        "",
    ]
    for r in results:
        ratio_pct = r["ratio"] * 100
        dd_pct = r["drawdown"] * 100
        lines.append(
            f"{r['label']}：峰值 {r['peak']:.4f} → "
            f"回撤率 {ratio_pct:.1f}%（较峰值回落 {dd_pct:.1f}%）"
        )
    content = "\n".join(lines)
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
