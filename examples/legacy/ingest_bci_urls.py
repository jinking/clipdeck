from __future__ import annotations

import asyncio
import re
from pathlib import Path
import httpx


def parse_items() -> list[dict[str, str]]:
    md_path = Path("_knowledge_base/research-脑机接口六公司脊髓损伤核查-20260828.md")
    content = md_path.read_text(encoding="utf-8")

    lines = content.splitlines()
    current_section = "未分类"

    items: list[dict[str, str]] = []
    seen: set[str] = set()

    for line in lines:
        line_s = line.strip()
        if line_s.startswith("### "):
            current_section = line_s.strip("# ").split("——")[0].split("（")[0].strip()

        urls = re.findall(r"https?://[^\s\|)]+", line_s)
        if not urls:
            continue

        for u in urls:
            u = u.rstrip(".,;!>")
            if u in seen:
                continue
            seen.add(u)

            parts = [p.strip() for p in line_s.split("|") if p.strip()]
            if len(parts) >= 4 and (parts[1].startswith("http") or parts[0].isdigit()):
                level = parts[2]
                note = parts[3]
                desc = f"【{current_section}】{level}: {note[:35]}"
            else:
                desc = f"【{current_section}】{line_s[:45]}"

            items.append({
                "section": current_section,
                "url": u,
                "display_name": desc,
            })

    return items


async def main() -> None:
    items = parse_items()
    print(f"[*] 解析出 {len(items)} 条独立待采集链接\n", flush=True)

    async with httpx.AsyncClient(base_url="http://127.0.0.1:8765", timeout=120.0, trust_env=False) as client:
        submitted: list[dict] = []
        for i, item in enumerate(items, 1):
            try:
                resp = await client.post(
                    "/api/v1/acquisitions",
                    json={"url": item["url"], "display_name": item["display_name"]},
                )
                if resp.status_code == 201:
                    tid = resp.json()["task_id"]
                    submitted.append({
                        "index": i,
                        "task_id": tid,
                        "url": item["url"],
                        "name": item["display_name"],
                        "section": item["section"],
                    })
                    print(f"[{i:02d}/{len(items):02d}] 提交成功: {item['display_name']} -> {tid}", flush=True)
                else:
                    print(f"[{i:02d}/{len(items):02d}] 提交失败 (HTTP {resp.status_code}): {item['url']}", flush=True)
            except Exception as exc:
                print(f"[{i:02d}/{len(items):02d}] 提交异常: {exc}", flush=True)

        print(f"\n[*] 全部 {len(submitted)} 个任务已进入队列，开始轮询等待完成与质检校验...\n", flush=True)

        # Poll until finished
        max_ticks = 100  # up to 300s
        for tick in range(max_ticks):
            await asyncio.sleep(3)
            pending = 0
            for t in submitted:
                resp = await client.get(f"/api/v1/acquisitions/{t['task_id']}")
                data = resp.json()
                t["status"] = data.get("status")
                t["error_code"] = data.get("last_error_code")
                t["error_message"] = data.get("last_error_message")
                t["asset_id"] = data.get("latest_asset_id")
                if t["status"] in ("pending", "running"):
                    pending += 1
            if pending == 0:
                print(f"[*] 全部采集任务执行完毕！（耗时 {(tick+1)*3} 秒）", flush=True)
                break
            if tick % 4 == 0:
                print(f"[处理中] 剩余运行中任务: {pending}/{len(submitted)} ...", flush=True)

        # Fetch evidence records
        ev_resp = await client.get("/api/v1/evidence?limit=200")
        evidence_list = ev_resp.json() if ev_resp.status_code == 200 else []
        ev_by_asset = {e["asset_id"]: e for e in evidence_list}

        print("\n" + "=" * 90)
        print("=== 国内脑机接口六家公司/团队脊髓损伤权威来源 采集与摄入详细报表 ===")
        print("=" * 90)

        current_sec = ""
        success_total = 0
        blocked_total = 0

        for t in submitted:
            if t["section"] != current_sec:
                current_sec = t["section"]
                print(f"\n### {current_sec}")
                print("-" * 90)

            st = t.get("status")
            aid = t.get("asset_id")
            ev = ev_by_asset.get(aid) if aid else None

            if st == "success" and ev:
                success_total += 1
                eid = ev.get("evidence_id")
                print(f"✔ [{t['index']:02d}] 【入库成功】 {t['name']}")
                print(f"     URL: {t['url']}")
                print(f"     Evidence: {eid}")
            else:
                blocked_total += 1
                err = f"{t.get('error_code')}: {t.get('error_message')}"
                print(f"❌ [{t['index']:02d}] 【采集拦截/失败】 {t['name']}")
                print(f"     URL: {t['url']}")
                print(f"     失败原因: {err}")

        print("\n" + "=" * 90)
        print(f"【最终汇总】提交总数: {len(submitted)} | 成功生成 Evidence: {success_total} | 质量门禁熔断/拦截: {blocked_total}")
        print("=" * 90)


if __name__ == "__main__":
    asyncio.run(main())
