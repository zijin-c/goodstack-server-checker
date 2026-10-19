#!/usr/bin/env python3
"""Read-only Goodstack organisation search for the provided charity list.

Uses Goodstack's documented GET /v1/organisations endpoint. It never claims,
creates, edits, or submits organisations.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

API_URL = os.getenv("GOODSTACK_API_URL", "https://api.goodstack.io/v1/organisations")
USER_AGENT = "GoodstackReadOnlyBatchChecker/1.0"


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class ProxyRotator:
    """Optional HTTP(S) proxy gateway or rotating list supplied by the operator."""
    def __init__(self) -> None:
        self.gateway = os.getenv("GOODSTACK_PROXY_URL", "").strip()
        self.list_url = os.getenv("GOODSTACK_PROXY_LIST_URL", "").strip()
        self.token = os.getenv("GOODSTACK_PROXY_TOKEN", "").strip()
        self.refresh_seconds = max(30, int(os.getenv("GOODSTACK_PROXY_REFRESH_SECONDS", "300")))
        self.proxies: list[str] = []
        self.refreshed_at = 0.0
        self.position = 0
        if self.gateway and self.list_url:
            raise ApiError("GOODSTACK_PROXY_URL 与 GOODSTACK_PROXY_LIST_URL 只能配置一个")

    @property
    def mode(self) -> str:
        if self.gateway:
            return "rotating_gateway"
        if self.list_url:
            return "downloaded_proxy_pool"
        return "direct"

    @staticmethod
    def _normalise_proxy(value: str) -> str:
        value = value.strip().strip("\"'")
        if not value:
            return ""
        if re.match(r"^[^:/\s]+:\d+:[^:\s]+:.+$", value):
            host, port, username, password = value.split(":", 3)
            value = f"http://{urllib.parse.quote(username)}:{urllib.parse.quote(password)}@{host}:{port}"
        elif "://" not in value:
            value = "http://" + value
        scheme = urllib.parse.urlsplit(value).scheme.lower()
        if scheme not in {"http", "https"}:
            raise ApiError("动态代理只支持 HTTP/HTTPS 代理；SOCKS 代理请换用 HTTP(S) 接口")
        return value

    @classmethod
    def _extract_proxies(cls, payload: Any) -> list[str]:
        found: list[str] = []
        def walk(value: Any) -> None:
            if isinstance(value, str):
                for line in value.splitlines():
                    candidate = cls._normalise_proxy(line)
                    if candidate:
                        found.append(candidate)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
            elif isinstance(value, dict):
                if value.get("ip") and value.get("port"):
                    host, port = str(value["ip"]), str(value["port"])
                    user, password = str(value.get("username") or ""), str(value.get("password") or "")
                    auth = f"{urllib.parse.quote(user)}:{urllib.parse.quote(password)}@" if user else ""
                    found.append(f"http://{auth}{host}:{port}")
                else:
                    for key in ("data", "proxies", "proxy", "result", "list", "items"):
                        if key in value:
                            walk(value[key])
        walk(payload)
        return list(dict.fromkeys(found))

    def _refresh(self) -> None:
        headers = {"Accept": "application/json,text/plain,*/*", "User-Agent": USER_AGENT}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.list_url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = response.read().decode("utf-8", "replace")
            try:
                payload: Any = json.loads(body)
            except json.JSONDecodeError:
                payload = body
            proxies = self._extract_proxies(payload)
            if not proxies:
                raise ApiError("代理服务返回内容中没有可识别的 HTTP(S) 代理")
            self.proxies = proxies
            self.refreshed_at = time.monotonic()
            self.position = 0
            print(f"动态代理池已更新：{len(proxies)} 个代理")
        except (urllib.error.URLError, TimeoutError, ApiError) as exc:
            raise ApiError(f"获取动态代理失败：{exc}") from exc

    def next_proxy(self) -> str:
        if self.gateway:
            return self._normalise_proxy(self.gateway)
        if not self.list_url:
            return ""
        if not self.proxies or time.monotonic() - self.refreshed_at >= self.refresh_seconds:
            self._refresh()
        proxy = self.proxies[self.position % len(self.proxies)]
        self.position += 1
        return proxy


def get(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def normalise(value: str) -> str:
    value = unicodedata.normalize("NFKD", (value or "").casefold())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]", "", value)


def normalise_reg(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").casefold())


def host_of(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = "https://" + value
    try:
        host = urllib.parse.urlsplit(value).hostname or ""
        return host.encode("idna").decode("ascii").lower().removeprefix("www.")
    except (ValueError, UnicodeError):
        return ""


def load_records(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            return [dict(row) for row in csv.DictReader(stream)]
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = payload.get("机构记录") or payload.get("records") or payload.get("data") or []
        if isinstance(records, dict):
            records = records.get("data", [])
    else:
        records = []
    if not isinstance(records, list):
        raise ValueError("输入 JSON 中没有可识别的记录数组（机构记录 / records / data）")
    return [dict(row) for row in records if isinstance(row, dict)]


def prepare_targets(records: list[dict[str, Any]], country_code: str) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in records:
        name = get(row, "CRA公开名称", "CRA英文法定名称", "机构名称", "organizationName", "name")
        legal_name = get(row, "CRA英文法定名称", "legalName", "name")
        registration = get(row, "CRA登记号", "registrationNumber", "registryId")
        domain = host_of(get(row, "主机名", "官网登记原始网址", "规范化官网", "website", "domain"))
        detected_domain = get(row, "检测域名", "detectedDomain")
        raw_country = get(row, "countryCode") or get(row, "国家/地区") or country_code
        country = {"canada": "CAN"}.get(raw_country.casefold(), raw_country.upper() if len(raw_country) == 3 else country_code)
        if not name and not registration:
            continue
        key = normalise_reg(registration) or f"{normalise(name)}|{domain or normalise(detected_domain)}"
        if key in seen:
            continue
        seen.add(key)
        targets.append({
            "inputKey": key,
            "inputName": name or legal_name,
            "legalName": legal_name,
            "registrationNumber": registration,
            "countryCode": country,
            "officialWebsite": get(row, "规范化官网", "官网登记原始网址", "website"),
            "officialDomain": domain,
            "detectedDomain": detected_domain,
            "sourceRecord": row,
        })
    return targets


class GoodstackClient:
    def __init__(self, api_key: str, min_delay: float, retries: int, proxy: ProxyRotator):
        self.api_key = api_key
        self.min_delay = min_delay
        self.retries = retries
        self.proxy = proxy
        self.last_request = 0.0

    def _wait_turn(self) -> None:
        remaining = self.min_delay - (time.monotonic() - self.last_request)
        if remaining > 0:
            time.sleep(remaining)
        self.last_request = time.monotonic()

    def search(self, params: dict[str, str], page_cap: int = 10) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        collected: list[dict[str, Any]] = []
        requests: list[dict[str, Any]] = []
        for page in range(1, page_cap + 1):
            query = dict(params)
            query.update({"page": str(page), "pageSize": "100"})
            url = API_URL + "?" + urllib.parse.urlencode(query)
            last_error: Exception | None = None
            for attempt in range(self.retries + 1):
                self._wait_turn()
                proxy_url = self.proxy.next_proxy()
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
                ) if proxy_url else urllib.request.build_opener()
                request = urllib.request.Request(url, headers={
                    "Authorization": self.api_key,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                })
                try:
                    with opener.open(request, timeout=30) as response:
                        status = response.status
                        body = response.read()
                    requests.append({"url": url, "httpStatus": status, "proxyMode": self.proxy.mode})
                    payload = json.loads(body.decode("utf-8")) if body else {}
                    if status >= 400:
                        raise ApiError(f"Goodstack HTTP {status}", status)
                    if not isinstance(payload, dict) or not isinstance(payload.get("data", []), list):
                        raise ApiError("Goodstack 返回格式不符合预期")
                    page_rows = payload.get("data", [])
                    collected.extend(item for item in page_rows if isinstance(item, dict))
                    if not page_rows or not payload.get("_links", {}).get("next"):
                        return collected, requests
                    last_error = None
                    break
                except urllib.error.HTTPError as exc:
                    body = exc.read(2000).decode("utf-8", "replace")
                    requests.append({"url": url, "httpStatus": exc.code, "proxyMode": self.proxy.mode})
                    if exc.code in (401, 403):
                        raise ApiError(f"Goodstack HTTP {exc.code}: API key 无效或无此接口权限；{body[:500]}", exc.code) from exc
                    if exc.code not in (429, 500, 502, 503, 504) or attempt >= self.retries:
                        raise ApiError(f"Goodstack HTTP {exc.code}: {body[:500]}", exc.code) from exc
                    retry_after = exc.headers.get("Retry-After", "")
                    try:
                        wait = max(float(retry_after), min(60.0, 2.0 ** attempt))
                    except ValueError:
                        wait = min(60.0, 2.0 ** attempt)
                    time.sleep(wait)
                    last_error = exc
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ApiError) as exc:
                    if isinstance(exc, ApiError) and exc.status in (401, 403):
                        raise
                    if isinstance(exc, ApiError) and exc.status is not None and exc.status not in (429, 500, 502, 503, 504):
                        raise
                    if attempt >= self.retries:
                        raise ApiError(str(exc), getattr(exc, "status", None)) from exc
                    time.sleep(min(60.0, 2.0 ** attempt))
                    last_error = exc
            if last_error:
                raise ApiError(str(last_error))
        return collected, requests


def candidate_grade(target: dict[str, Any], candidate: dict[str, Any]) -> str:
    target_reg = normalise_reg(target.get("registrationNumber", ""))
    candidate_reg = normalise_reg(str(candidate.get("registryId") or ""))
    if target_reg and candidate_reg and target_reg == candidate_reg:
        return "registration_id_exact"
    target_domain = target.get("officialDomain", "")
    candidate_hosts = {host_of(str(candidate.get("website") or ""))}
    websites = candidate.get("websites") or []
    if isinstance(websites, list):
        candidate_hosts.update(host_of(str(value)) for value in websites)
    candidate_hosts.discard("")
    if target_domain and target_domain in candidate_hosts:
        return "website_domain_exact"
    target_names = {normalise(target.get("inputName", "")), normalise(target.get("legalName", ""))} - {""}
    candidate_names = {normalise(str(candidate.get(k) or "")) for k in ("name", "displayName")} - {""}
    if target_names & candidate_names:
        return "name_exact"
    return "review_candidate"


def make_queries(target: dict[str, Any]) -> list[tuple[str, dict[str, str]]]:
    base = {"countryCode": target.get("countryCode") or "CAN"}
    queries: list[tuple[str, dict[str, str]]] = []
    reg = target.get("registrationNumber", "")
    if reg:
        queries.append(("registryId", {**base, "registryId": reg}))
    for label, name in (("publicName", target.get("inputName", "")), ("legalName", target.get("legalName", ""))):
        if name and all(normalise(name) != normalise(q[1].get("query", "")) for q in queries if "query" in q[1]):
            queries.append((label, {**base, "query": name}))
    if target.get("officialDomain"):
        queries.append(("officialDomain", {**base, "domain": target["officialDomain"]}))
    return queries


def check_target(client: GoodstackClient, target: dict[str, Any], page_cap: int) -> dict[str, Any]:
    by_id: dict[str, dict[str, Any]] = {}
    query_log: list[dict[str, Any]] = []
    query_errors: list[str] = []
    for method, params in make_queries(target):
        try:
            rows, requests = client.search(params, page_cap=page_cap)
            query_log.extend({"method": method, **item} for item in requests)
            for candidate in rows:
                identifier = str(candidate.get("id") or json.dumps(candidate, sort_keys=True, ensure_ascii=False))
                by_id[identifier] = candidate
            # Stop as soon as an exact identifier, domain, or name match is found.
            # This keeps the batch small while still falling back across aliases.
            if any(candidate_grade(target, row) != "review_candidate" for row in rows):
                break
        except ApiError as exc:
            query_errors.append(f"{method}: {exc}")
            if exc.status in (401, 403):
                raise
    candidates = []
    for candidate in by_id.values():
        candidates.append({"matchGrade": candidate_grade(target, candidate), "bothDatesPresent": bool(candidate.get("createdAt") and candidate.get("claimedAt")), "organisation": candidate})
    candidates.sort(key=lambda item: (item["matchGrade"] == "review_candidate", not item["bothDatesPresent"], str(item["organisation"].get("name") or "").casefold()))
    exact = [item for item in candidates if item["matchGrade"] != "review_candidate"]
    both = [item for item in candidates if item["bothDatesPresent"]]
    if exact and any(item["bothDatesPresent"] for item in exact):
        status = "exact_match_both_dates"
    elif exact:
        status = "exact_match_dates_incomplete"
    elif candidates:
        status = "candidates_need_review"
    elif query_errors:
        status = "query_error"
    else:
        status = "not_found"
    return {
        "inputKey": target["inputKey"],
        "inputName": target["inputName"],
        "legalName": target["legalName"],
        "registrationNumber": target["registrationNumber"],
        "countryCode": target["countryCode"],
        "officialWebsite": target["officialWebsite"],
        "officialDomain": target["officialDomain"],
        "detectedDomain": target["detectedDomain"],
        "status": status,
        "candidateCount": len(candidates),
        "exactCandidateCount": len(exact),
        "bothDatesCandidateCount": len(both),
        "candidates": candidates,
        "queryLog": query_log,
        "errors": query_errors,
        "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sourceRecord": target["sourceRecord"],
    }


def save_outputs(out_dir: Path, checkpoint: Path, input_count: int) -> None:
    latest_by_key: dict[str, dict[str, Any]] = {}
    if checkpoint.exists():
        with checkpoint.open("r", encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    try:
                        item = json.loads(line)
                        if item.get("inputKey"):
                            latest_by_key[item["inputKey"]] = item
                    except json.JSONDecodeError:
                        continue
    results = list(latest_by_key.values())
    results.sort(key=lambda row: (row.get("inputName", "").casefold(), row.get("inputKey", "")))
    both_date_rows = []
    for result in results:
        for candidate in result.get("candidates", []):
            org = candidate.get("organisation", {})
            if org.get("createdAt") and org.get("claimedAt"):
                both_date_rows.append({
                    "target": {key: result.get(key) for key in ("inputName", "legalName", "registrationNumber", "officialDomain", "detectedDomain")},
                    "matchGrade": candidate.get("matchGrade"),
                    "createdAt": org.get("createdAt"),
                    "claimedAt": org.get("claimedAt"),
                    "organisation": org,
                })
    summary = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "inputRecordCount": input_count,
        "processedRecordCount": len(results),
        "exactMatchCount": sum(1 for row in results if row.get("exactCandidateCount", 0)),
        "exactMatchWithBothDatesCount": sum(1 for row in results if row.get("status") == "exact_match_both_dates"),
        "needsReviewCount": sum(1 for row in results if row.get("status") == "candidates_need_review"),
        "notFoundCount": sum(1 for row in results if row.get("status") == "not_found"),
        "queryErrorCount": sum(1 for row in results if row.get("status") == "query_error"),
        "bothDatesCandidateCount": len(both_date_rows),
        "dateFields": ["createdAt", "claimedAt"],
        "results": "results.jsonl",
        "bothDatesResults": "both_dates.json",
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "both_dates.json").write_text(json.dumps({"summary": summary, "results": both_date_rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = ["inputName", "legalName", "registrationNumber", "countryCode", "officialDomain", "detectedDomain", "status", "candidateCount", "exactCandidateCount", "bothDatesCandidateCount", "matchedNames", "matchedIds", "matchedCreatedAt", "matchedClaimedAt", "checkedAt", "errors"]
    with (out_dir / "summary.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for result in results:
            exact = [c["organisation"] for c in result.get("candidates", []) if c.get("matchGrade") != "review_candidate"]
            writer.writerow({
                **{key: result.get(key, "") for key in fields if key in result},
                "matchedNames": "; ".join(str(item.get("name") or item.get("displayName") or "") for item in exact),
                "matchedIds": "; ".join(str(item.get("id") or "") for item in exact),
                "matchedCreatedAt": "; ".join(str(item.get("createdAt") or "") for item in exact),
                "matchedClaimedAt": "; ".join(str(item.get("claimedAt") or "") for item in exact),
                "errors": "; ".join(result.get("errors", [])),
            })


def main() -> int:
    parser = argparse.ArgumentParser(description="只读批量查询 Goodstack 机构目录，并筛选 createdAt 和 claimedAt 均有值的结果")
    parser.add_argument("--input", type=Path, default=Path("/data/input.json"), help="JSON 或 CSV 机构名单")
    parser.add_argument("--output-dir", type=Path, default=Path("/data/results"), help="结果目录；复用可断点续跑")
    parser.add_argument("--country-code", default="CAN", help="ISO 三位国家代码，默认 CAN")
    parser.add_argument("--delay", type=float, default=1.0, help="每次请求最小间隔秒数，默认 1.0")
    parser.add_argument("--retries", type=int, default=5, help="429/5xx/网络错误重试次数")
    parser.add_argument("--page-cap", type=int, default=10, help="每个查询最多读取页数，每页 100 条")
    parser.add_argument("--max-records", type=int, default=0, help="只处理前 N 条；0 表示全部")
    parser.add_argument("--no-resume", action="store_true", help="忽略已有 checkpoint，重新查询全部")
    args = parser.parse_args()
    api_key = os.getenv("GOODSTACK_API_KEY", "").strip()
    if not api_key:
        parser.error("请先通过环境变量 GOODSTACK_API_KEY 设置 Goodstack API key（pk_ 或 sk_）")
    if args.delay < 0 or args.retries < 0 or args.page_cap < 1:
        parser.error("--delay、--retries、--page-cap 参数无效")
    if not args.input.exists():
        parser.error(f"输入文件不存在：{args.input}")
    all_records = load_records(args.input)
    targets = prepare_targets(all_records, args.country_code)
    if args.max_records > 0:
        targets = targets[:args.max_records]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = args.output_dir / "checkpoint.jsonl"
    completed: set[str] = set()
    if checkpoint.exists() and not args.no_resume:
        with checkpoint.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                    if item.get("inputKey") and item.get("status") != "query_error":
                        completed.add(item["inputKey"])
                except json.JSONDecodeError:
                    pass
    pending = [target for target in targets if target["inputKey"] not in completed]
    print(f"输入记录 {len(all_records)} 条；去重后目标 {len(targets)} 家；已完成 {len(completed)} 家；本轮待查 {len(pending)} 家。")
    print(f"筛选字段：Goodstack createdAt 与 claimedAt 均非空。输出目录：{args.output_dir}")
    proxy = ProxyRotator()
    print(f"网络模式：{proxy.mode}")
    client = GoodstackClient(api_key, args.delay, args.retries, proxy)
    try:
        with checkpoint.open("a", encoding="utf-8") as stream:
            for index, target in enumerate(pending, start=1):
                try:
                    result = check_target(client, target, args.page_cap)
                except ApiError as exc:
                    result = {
                        "inputKey": target["inputKey"], "inputName": target["inputName"],
                        "legalName": target["legalName"], "registrationNumber": target["registrationNumber"],
                        "countryCode": target["countryCode"], "officialWebsite": target["officialWebsite"],
                        "officialDomain": target["officialDomain"], "detectedDomain": target["detectedDomain"],
                        "status": "query_error", "candidateCount": 0, "exactCandidateCount": 0,
                        "bothDatesCandidateCount": 0, "candidates": [], "queryLog": [],
                        "errors": [str(exc)], "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "sourceRecord": target["sourceRecord"],
                    }
                    print(f"错误：{target['inputName']} — {exc}", file=sys.stderr)
                    if exc.status in (401, 403):
                        raise
                stream.write(json.dumps(result, ensure_ascii=False) + "\n")
                stream.flush()
                if index % 25 == 0 or index == len(pending):
                    print(f"进度 {index}/{len(pending)}；最近：{target['inputName']} → {result['status']}")
                    save_outputs(args.output_dir, checkpoint, len(targets))
    except KeyboardInterrupt:
        print("收到中断信号，已保留 checkpoint；重新运行同一命令可续跑。", file=sys.stderr)
        save_outputs(args.output_dir, checkpoint, len(targets))
        return 130
    save_outputs(args.output_dir, checkpoint, len(targets))
    print(f"完成。结果：{args.output_dir / 'summary.csv'}；双时间筛选：{args.output_dir / 'both_dates.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
