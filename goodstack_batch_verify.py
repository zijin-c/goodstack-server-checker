#!/usr/bin/env python3
"""Search Goodstack's public claim page with Chromium and save visible evidence.

The script operates the public web form. It does not make direct API calls,
create organisations, claim profiles, or submit any claim/application forms.
When the page itself loads a search result, its browser response is observed
to retain createdAt/claimedAt fields that are not rendered in the visible card.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import queue
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, ContextManager, Iterator

SEARCH_URL = "https://causes.goodstack.io/claim?utm_source=goodstack_org"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari"
BLOCK_RE = re.compile(r"verify you are human|checking your browser|unusual traffic|automated queries|验证您是真人|人机验证", re.I)
ERROR_RE = re.compile(r"unknown error|an error occurred|something went wrong|请刷新页面重试|请求失败|查询失败", re.I)
EMPTY_RE = re.compile(r"未找到组织|未找到任何组织|没有找到任何组织|没有匹配的组织|no (?:organizations?|organisations?|results) found|organization not found|organisation not found|0 个结果|0 results", re.I)
JOIN_RE = re.compile(r"申请加入|apply to join|request to join", re.I)
CLAIMED_RE = re.compile(r"已认领|已有管理员|已由管理员管理|claimed by|managed by an admin|has an admin", re.I)
CLAIM_ACTION_RE = re.compile(r"认领组织|claim organization|claim this organization", re.I)
COUNT_RE = re.compile(r"(?:找到|发现)\s*(\d+)\s*(?:个)?组织|(?:showing|found)\s*(\d+)\s*(?:organizations?|organisations?)|\b(\d+)\s*results?\b", re.I)


class SearchError(RuntimeError):
    pass


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


class ProxyRotator:
    """Optional provider gateway or list of HTTP(S) proxies for browser contexts."""

    def __init__(self) -> None:
        self.gateway = os.getenv("GOODSTACK_PROXY_URL", "").strip()
        self.list_url = os.getenv("GOODSTACK_PROXY_LIST_URL", "").strip()
        self.token = os.getenv("GOODSTACK_PROXY_TOKEN", "").strip()
        self.refresh_seconds = max(30, int(os.getenv("GOODSTACK_PROXY_REFRESH_SECONDS", "300")))
        self.proxies: list[str] = []
        self.refreshed_at = 0.0
        self.position = 0
        self.dashboard_config = Path(os.getenv("DATA_DIR", "/data")) / "proxy_settings.json"
        self.dashboard_proxies: list[str] = []
        self.dashboard_enabled = True
        self.dashboard_mtime = 0.0
        self.dashboard_config_present = False
        self.lock = threading.Lock()
        self._refresh_dashboard_settings()
        if self.gateway and self.list_url:
            raise SearchError("GOODSTACK_PROXY_URL 与 GOODSTACK_PROXY_LIST_URL 只能配置一个")

    def _refresh_dashboard_settings(self) -> None:
        try:
            modified = self.dashboard_config.stat().st_mtime_ns
            if modified != self.dashboard_mtime:
                settings = json.loads(self.dashboard_config.read_text(encoding="utf-8"))
                self.dashboard_proxies = [self.normalise_proxy(value) for value in settings.get("proxies", []) if isinstance(value, str)]
                self.dashboard_enabled = bool(settings.get("enabled", True))
                self.dashboard_mtime = modified
                self.dashboard_config_present = True
                self.position = 0
        except FileNotFoundError:
            self.dashboard_proxies = []
            self.dashboard_enabled = True
            self.dashboard_config_present = False
        except (OSError, json.JSONDecodeError, SearchError) as exc:
            raise SearchError(f"读取控制台代理设置失败：{exc}") from exc

    @property
    def mode(self) -> str:
        if self.dashboard_config_present:
            return "dashboard_proxy_pool" if self.dashboard_proxies and self.dashboard_enabled else "direct"
        if self.dashboard_proxies and self.dashboard_enabled:
            return "dashboard_proxy_pool"
        if self.gateway:
            return "rotating_gateway"
        if self.list_url:
            return "downloaded_proxy_pool"
        return "direct"

    @staticmethod
    def normalise_proxy(value: str) -> str:
        value = value.strip().strip("\"'")
        if not value:
            return ""
        if re.match(r"^[^:/\s]+:\d+:[^:\s]+:.+$", value):
            host, port, username, password = value.split(":", 3)
            value = f"http://{urllib.parse.quote(username)}:{urllib.parse.quote(password)}@{host}:{port}"
        elif "://" not in value:
            value = "http://" + value
        if urllib.parse.urlsplit(value).scheme.lower() not in {"http", "https"}:
            raise SearchError("代理只支持 HTTP/HTTPS；SOCKS 代理当前不支持")
        return value

    @classmethod
    def extract(cls, payload: Any) -> list[str]:
        values: list[str] = []

        def walk(value: Any) -> None:
            if isinstance(value, str):
                values.extend(cls.normalise_proxy(line) for line in value.splitlines() if line.strip())
            elif isinstance(value, list):
                for item in value:
                    walk(item)
            elif isinstance(value, dict):
                if value.get("ip") and value.get("port"):
                    user = urllib.parse.quote(str(value.get("username") or ""))
                    password = urllib.parse.quote(str(value.get("password") or ""))
                    auth = f"{user}:{password}@" if user else ""
                    values.append(f"http://{auth}{value['ip']}:{value['port']}")
                else:
                    for key in ("data", "proxies", "proxy", "result", "list", "items"):
                        if key in value:
                            walk(value[key])

        walk(payload)
        return list(dict.fromkeys(value for value in values if value))

    def refresh(self) -> None:
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
            proxies = self.extract(payload)
            if not proxies:
                raise SearchError("代理池响应中没有可识别的 HTTP(S) 代理")
            self.proxies = proxies
            self.refreshed_at = time.monotonic()
            self.position = 0
            print(f"代理池已更新：{len(proxies)} 个代理")
        except (urllib.error.URLError, TimeoutError, SearchError) as exc:
            raise SearchError(f"获取代理列表失败：{exc}") from exc

    def next_proxy(self) -> dict[str, str] | None:
        with self.lock:
            return self._next_proxy()

    def _next_proxy(self) -> dict[str, str] | None:
        self._refresh_dashboard_settings()
        if self.dashboard_config_present and (not self.dashboard_enabled or not self.dashboard_proxies):
            return None
        if not self.dashboard_enabled:
            return None
        if self.dashboard_proxies:
            raw = self.dashboard_proxies[self.position % len(self.dashboard_proxies)]
            self.position += 1
        elif self.gateway:
            raw = self.normalise_proxy(self.gateway)
        elif self.list_url:
            if not self.proxies or time.monotonic() - self.refreshed_at >= self.refresh_seconds:
                self.refresh()
            raw = self.proxies[self.position % len(self.proxies)]
            self.position += 1
        else:
            return None
        parsed = urllib.parse.urlsplit(raw)
        server = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port:
            server += f":{parsed.port}"
        result = {"server": server}
        if parsed.username:
            result["username"] = urllib.parse.unquote(parsed.username)
            result["password"] = urllib.parse.unquote(parsed.password or "")
        return result


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
        if not name and not registration:
            continue
        key = normalise_reg(registration) or f"{normalise(name)}|{domain or normalise(detected_domain)}"
        if key in seen:
            continue
        seen.add(key)
        targets.append({
            "inputKey": key, "inputName": name or legal_name, "legalName": legal_name,
            "registrationNumber": registration, "countryCode": country_code,
            "officialWebsite": get(row, "规范化官网", "官网登记原始网址", "website"),
            "officialDomain": domain, "detectedDomain": detected_domain, "sourceRecord": row,
        })
    return targets


def visible_results(text: str) -> dict[str, Any]:
    count_match = COUNT_RE.search(text)
    result_count = next((int(x) for x in count_match.groups() if x), None) if count_match else None
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]
    records = []
    for index, line in enumerate(lines):
        if not re.search(r"^(?:地址|address)$", line, re.I):
            continue
        name = lines[index - 1] if index else ""
        address = lines[index + 1] if index + 1 < len(lines) else ""
        reg_no = ""
        for pos in range(index + 2, min(index + 5, len(lines))):
            if re.search(r"^(?:注册编号|登记编号|registration (?:number|no\.?|#)|registry number)$", lines[pos], re.I):
                reg_no = lines[pos + 1] if pos + 1 < len(lines) else ""
                break
        if name and name.casefold() not in {"地址", "address"}:
            card_text = " ".join(lines[index - 1:min(len(lines), index + 5)])
            records.append({
                "name": name,
                "address": "" if re.search(r"无地址信息|no address", address, re.I) else address,
                "registryId": reg_no,
                "statusHint": "apply_to_join" if JOIN_RE.search(card_text) else "claimed_hint" if CLAIMED_RE.search(card_text) else "claim_action" if CLAIM_ACTION_RE.search(card_text) else "",
                "source": "visible_page",
            })
    return {"resultCount": result_count, "results": records}


def extract_browser_payload(response: Any) -> list[dict[str, Any]]:
    """Observe only the response generated by the page's own public search form."""
    try:
        payload = response.json()
    except Exception:
        return []
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return []
    rows = []
    for item in payload["data"]:
        if not isinstance(item, dict):
            continue
        rows.append({
            "id": item.get("id", ""), "name": item.get("name", ""),
            "displayName": item.get("displayName", ""), "countryCode": item.get("countryCode", ""),
            "registryName": item.get("registry", {}).get("englishName", "") if isinstance(item.get("registry"), dict) else "",
            "registryId": item.get("registryId", ""), "website": item.get("website", ""),
            "websites": item.get("websites", []), "address": item.get("address", ""),
            "addressLine1": item.get("addressLine1", ""), "addressLine2": item.get("addressLine2", ""),
            "city": item.get("city", ""), "state": item.get("state", ""), "postcode": item.get("postcode", ""),
            "registrationDate": item.get("registrationDate", ""),
            "createdAt": item.get("createdAt", ""), "claimedAt": item.get("claimedAt", ""),
            "source": "browser_page_search_response",
        })
    return rows


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
    candidate_names = {normalise(str(candidate.get(key) or "")) for key in ("name", "displayName")} - {""}
    if target_names & candidate_names:
        return "name_exact"
    return "review_candidate"


def classify_visible(text: str, name: str, parsed: dict[str, Any]) -> tuple[str, str]:
    if BLOCK_RE.search(text):
        return "browser_challenge", "页面要求真人验证；已停止自动搜索，不自动处理验证"
    if ERROR_RE.search(text):
        return "query_error", "Goodstack 页面显示查询错误，不能据此判断是否收录"
    exact_cards = [row for row in parsed["results"] if normalise(row["name"]) == normalise(name)]
    if exact_cards and any(row["statusHint"] == "claimed_hint" for row in exact_cards):
        return "visible_claimed_hint", "页面文字明确提示已认领或已有管理员，请人工确认"
    if exact_cards and any(row["statusHint"] == "apply_to_join" for row in exact_cards):
        return "visible_apply_to_join", "页面显示申请加入入口；脚本不会点击或提交"
    if exact_cards:
        return "visible_exact_result", "页面显示同名结果；仍需对照登记编号确认"
    if EMPTY_RE.search(text) or parsed["resultCount"] == 0:
        return "not_found_on_page", "页面明确显示无结果；仅代表本次公开名称搜索"
    if parsed["resultCount"] is not None and parsed["resultCount"] > 0:
        return "candidates_need_review", f"页面返回 {parsed['resultCount']} 个候选，但没有完全同名卡片"
    return "query_error", "未能从页面识别出搜索完成状态，不能判断是否收录"


def configure_country(page: Any, country_name: str) -> None:
    country = page.locator('input[id^="headlessui-combobox-input-"]').first
    if not country.count():
        country = page.get_by_role("combobox").first
    current = country.input_value().strip()
    if current.casefold() == country_name.casefold():
        return
    country.fill(country_name)
    option = page.get_by_role("option", name=re.compile(re.escape(country_name), re.I)).first
    if option.count():
        option.click()
    else:
        page.get_by_text(country_name, exact=True).last.click()
    if country.input_value().strip().casefold() != country_name.casefold():
        raise SearchError(f"国家选择框未确认选中 {country_name}")


def search_once(page: Any, query_name: str, delay: float, browser_payloads: list[dict[str, Any]], browser_statuses: list[int]) -> tuple[dict[str, Any], str, int]:
    configure_country(page, "Canada")
    name_input = page.get_by_role("textbox", name=re.compile(r"组织名称|organization name|organisation name", re.I)).first
    if not name_input.count():
        name_input = page.locator('input:visible:not([id^="headlessui-combobox-input-"]):not([type="hidden"]):not([type="checkbox"]):not([type="radio"])').first
    search = page.get_by_role("button", name=re.compile(r"^(搜索|Search)$", re.I)).first
    name_input.fill(query_name)
    baseline_text = page.locator("body").inner_text()
    browser_payloads.clear()
    browser_statuses.clear()
    search.click()
    deadline = time.monotonic() + 30
    status_code = 0
    while time.monotonic() < deadline:
        page.wait_for_timeout(500)
        if browser_statuses:
            status_code = browser_statuses[-1]
            break
        text = page.locator("body").inner_text()
        if text != baseline_text and (BLOCK_RE.search(text) or ERROR_RE.search(text) or EMPTY_RE.search(text)):
            status_code = 200
            break
    if status_code == 0 and not browser_payloads:
        raise SearchError("等待网页搜索结果超时或浏览器未收到有效结果")
    if delay > 0:
        time.sleep(delay)
    visible = visible_results(page.locator("body").inner_text())
    rows_by_id: dict[str, dict[str, Any]] = {}
    for row in browser_payloads:
        rows_by_id[str(row.get("id") or json.dumps(row, sort_keys=True, ensure_ascii=False))] = row
    for row in visible["results"]:
        rows_by_id.setdefault(f"visible:{row['name']}:{row['registryId']}", row)
    return visible, page.locator("body").inner_text(), status_code


def check_target(browser: Any, proxy: ProxyRotator, target: dict[str, Any], delay: float, timeout: int) -> dict[str, Any]:
    context_options: dict[str, Any] = {"locale": "zh-CN", "viewport": {"width": 1365, "height": 900}, "user_agent": USER_AGENT}
    browser_proxy = proxy.next_proxy()
    if browser_proxy:
        context_options["proxy"] = browser_proxy
    context = browser.new_context(**context_options)
    page = context.new_page()
    page.set_default_timeout(timeout * 1000)
    seen_payloads: list[dict[str, Any]] = []
    browser_statuses: list[int] = []
    response_errors: list[str] = []

    def observe_response(response: Any) -> None:
        # The browser's public form initiates this request; the script never issues it.
        if "/v1/organisations" not in response.url or response.request.method.upper() != "GET":
            return
        browser_statuses.append(response.status)
        if response.status >= 400:
            response_errors.append(f"浏览器搜索响应 HTTP {response.status}")
            return
        seen_payloads.extend(extract_browser_payload(response))

    page.on("response", observe_response)
    try:
        page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=timeout * 1000)
        # Goodstack page defaults to Canada in its public search form.
        queries = [target["inputName"]] if target["inputName"] else []
        if target.get("legalName") and normalise(target["legalName"]) != normalise(target["inputName"]):
            queries.append(target["legalName"])
        if not queries and target.get("registrationNumber"):
            queries.append(target["registrationNumber"])
        merged_visible: list[dict[str, Any]] = []
        full_text = ""
        for query_name in queries:
            visible, text, _ = search_once(page, query_name, delay, seen_payloads, browser_statuses)
            merged_visible.extend(visible["results"])
            full_text = text
            if BLOCK_RE.search(text) or ERROR_RE.search(text):
                break
            candidates = list(seen_payloads) + merged_visible
            if any(candidate_grade(target, row) != "review_candidate" for row in candidates):
                break
        visible = {"results": merged_visible, "resultCount": visible.get("resultCount")}
        by_id: dict[str, dict[str, Any]] = {}
        for row in seen_payloads + merged_visible:
            identifier = str(row.get("id") or f"{row.get('name')}|{row.get('registryId')}")
            by_id[identifier] = row
        candidates = [{"matchGrade": candidate_grade(target, row), "bothDatesPresent": bool(row.get("createdAt") and row.get("claimedAt")), "organisation": row} for row in by_id.values()]
        candidates.sort(key=lambda item: (item["matchGrade"] == "review_candidate", not item["bothDatesPresent"], str(item["organisation"].get("name") or "").casefold()))
        status, detail = classify_visible(full_text, target["inputName"], visible)
        exact = [item for item in candidates if item["matchGrade"] != "review_candidate"]
        both = [item for item in exact if item["bothDatesPresent"]]
        if response_errors:
            status, detail = "query_error", "; ".join(response_errors)
        elif status not in {"browser_challenge", "query_error"}:
            if exact and both:
                status, detail = "exact_match_both_dates", "浏览器搜索精确匹配，页面搜索结果包含 createdAt 与 claimedAt"
            elif exact:
                status, detail = "exact_match_dates_incomplete", "浏览器搜索精确匹配，但加载的结果没有同时提供两个日期字段"
            elif candidates:
                status, detail = "candidates_need_review", "浏览器搜索返回候选，但未与名单信息精确匹配"
        return {
            "inputKey": target["inputKey"], "inputName": target["inputName"], "legalName": target["legalName"],
            "registrationNumber": target["registrationNumber"], "countryCode": target["countryCode"],
            "officialWebsite": target["officialWebsite"], "officialDomain": target["officialDomain"],
            "detectedDomain": target["detectedDomain"], "status": status, "detail": detail,
            "visibleResultCount": visible.get("resultCount"), "visibleResults": merged_visible,
            "candidateCount": len(candidates), "exactCandidateCount": len(exact),
            "bothDatesCandidateCount": len(both), "candidates": candidates,
            "searchMethod": "chromium_public_page", "proxyMode": proxy.mode,
            "errors": response_errors, "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sourceRecord": target["sourceRecord"],
        }
    finally:
        context.close()


@contextmanager
def browser_worker_context(proxy: ProxyRotator, delay: float, timeout: int) -> Iterator[Callable[[dict[str, Any]], dict[str, Any]]]:
    """Give each worker thread its own Playwright driver and Chromium instance."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            yield lambda target: check_target(browser, proxy, target, delay, timeout)
        finally:
            browser.close()


def error_result(target: dict[str, Any], exc: Exception) -> dict[str, Any]:
    return {
        "inputKey": target["inputKey"], "inputName": target["inputName"], "legalName": target["legalName"],
        "registrationNumber": target["registrationNumber"], "countryCode": target["countryCode"],
        "officialWebsite": target["officialWebsite"], "officialDomain": target["officialDomain"],
        "detectedDomain": target["detectedDomain"], "status": "query_error", "candidateCount": 0,
        "exactCandidateCount": 0, "bothDatesCandidateCount": 0, "candidates": [],
        "errors": [f"{type(exc).__name__}: {exc}"], "searchMethod": "chromium_public_page",
        "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "sourceRecord": target["sourceRecord"],
    }


def run_parallel(
    targets: list[dict[str, Any]],
    worker_count: int,
    worker_context_factory: Callable[[], ContextManager[Callable[[dict[str, Any]], dict[str, Any]]]],
    on_result: Callable[[dict[str, Any]], None],
) -> None:
    """Process targets concurrently while keeping each browser on its owning thread."""
    if not targets:
        return
    jobs: queue.Queue[dict[str, Any] | None] = queue.Queue()
    messages: queue.Queue[tuple[str, Any]] = queue.Queue()
    stop_event = threading.Event()
    for target in targets:
        jobs.put(target)
    for _ in range(worker_count):
        jobs.put(None)

    def worker_main(worker_id: int) -> None:
        try:
            with worker_context_factory() as process_target:
                while not stop_event.is_set():
                    target = jobs.get()
                    if target is None:
                        break
                    print(f"线程 {worker_id} 正在搜索：{target.get('inputName', '')}", flush=True)
                    try:
                        result = process_target(target)
                    except Exception as exc:
                        result = error_result(target, exc)
                        print(f"错误：{target['inputName']} — {exc}", file=sys.stderr, flush=True)
                    messages.put(("result", result))
                    if result.get("status") == "browser_challenge":
                        stop_event.set()
        except Exception as exc:
            messages.put(("fatal", f"工作线程 {worker_id} 启动失败：{type(exc).__name__}: {exc}"))
            stop_event.set()
        finally:
            messages.put(("done", worker_id))

    threads = [threading.Thread(target=worker_main, args=(index + 1,), name=f"goodstack-worker-{index + 1}") for index in range(worker_count)]
    for thread in threads:
        thread.start()
    finished_workers = 0
    fatal_error = ""
    try:
        while finished_workers < worker_count:
            try:
                kind, payload = messages.get(timeout=0.5)
            except queue.Empty:
                continue
            if kind == "result":
                on_result(payload)
            elif kind == "fatal":
                fatal_error = fatal_error or str(payload)
                print(str(payload), file=sys.stderr, flush=True)
            elif kind == "done":
                finished_workers += 1
    except KeyboardInterrupt:
        stop_event.set()
        raise
    finally:
        stop_event.set() if fatal_error else None
        for thread in threads:
            thread.join()
    if fatal_error:
        raise RuntimeError(fatal_error)


def save_outputs(out_dir: Path, checkpoint: Path, input_count: int) -> None:
    latest: dict[str, dict[str, Any]] = {}
    if checkpoint.exists():
        with checkpoint.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if row.get("inputKey"):
                        latest[row["inputKey"]] = row
                except json.JSONDecodeError:
                    continue
    results = sorted(latest.values(), key=lambda row: (row.get("inputName", "").casefold(), row.get("inputKey", "")))
    both_rows = []
    for result in results:
        for candidate in result.get("candidates", []):
            org = candidate.get("organisation", {})
            if candidate.get("matchGrade") != "review_candidate" and org.get("createdAt") and org.get("claimedAt"):
                both_rows.append({"target": {key: result.get(key) for key in ("inputName", "legalName", "registrationNumber", "officialWebsite", "officialDomain", "detectedDomain")}, "matchGrade": candidate["matchGrade"], "createdAt": org["createdAt"], "claimedAt": org["claimedAt"], "checkedAt": result.get("checkedAt", ""), "organisation": org})
    summary = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "inputRecordCount": input_count,
        "processedRecordCount": len(results), "exactMatchCount": sum(bool(r.get("exactCandidateCount")) for r in results),
        "exactMatchWithBothDatesCount": sum(r.get("status") == "exact_match_both_dates" for r in results),
        "visiblePageMatchCount": sum(r.get("status", "").startswith("visible_") for r in results),
        "needsReviewCount": sum(r.get("status") == "candidates_need_review" for r in results),
        "notFoundCount": sum(r.get("status") == "not_found_on_page" for r in results),
        "queryErrorCount": sum(r.get("status") in {"query_error", "browser_challenge"} for r in results),
        "bothDatesCandidateCount": len(both_rows), "dateFields": ["createdAt", "claimedAt"],
        "method": "Playwright Chromium operating Goodstack public search page; no direct API requests",
        "filteredCsv": "both_dates.csv", "bothDatesResults": "both_dates.json",
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "both_dates.json").write_text(json.dumps({"summary": summary, "results": both_rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = [
        "inputName", "legalName", "inputRegistrationNumber", "inputOfficialWebsite", "inputDomain",
        "matchGrade", "goodstackId", "goodstackName", "displayName", "countryCode", "registryName",
        "registryId", "website", "websites", "address", "addressLine1", "addressLine2", "city",
        "state", "postcode", "registrationDate", "createdAt", "claimedAt", "checkedAt",
    ]
    with (out_dir / "both_dates.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for match in both_rows:
            target = match["target"]
            org = match["organisation"]
            writer.writerow({
                "inputName": target.get("inputName", ""),
                "legalName": target.get("legalName", ""),
                "inputRegistrationNumber": target.get("registrationNumber", ""),
                "inputOfficialWebsite": target.get("officialWebsite", ""),
                "inputDomain": target.get("officialDomain", "") or target.get("detectedDomain", ""),
                "matchGrade": match.get("matchGrade", ""),
                "goodstackId": org.get("id", ""),
                "goodstackName": org.get("name", ""),
                "displayName": org.get("displayName", ""),
                "countryCode": org.get("countryCode", ""),
                "registryName": org.get("registryName", ""),
                "registryId": org.get("registryId", ""),
                "website": org.get("website", ""),
                "websites": "; ".join(org.get("websites", [])) if isinstance(org.get("websites"), list) else org.get("websites", ""),
                **{key: org.get(key, "") for key in ("address", "addressLine1", "addressLine2", "city", "state", "postcode", "registrationDate", "createdAt", "claimedAt")},
                "checkedAt": match.get("checkedAt", ""),
            })


def main() -> int:
    parser = argparse.ArgumentParser(description="用 Chromium 自动操作 Goodstack 公开搜索页面批量核验机构")
    parser.add_argument("--input", type=Path, default=Path("/data/input.json"), help="JSON 或 CSV 机构名单")
    parser.add_argument("--output-dir", type=Path, default=Path("/data/results"), help="输出目录；复用 checkpoint 可续跑")
    parser.add_argument("--country-code", default="CAN", help="输入数据国家代码，默认 CAN")
    parser.add_argument("--delay", type=float, default=2.0, help="每次页面搜索后的等待秒数")
    parser.add_argument("--timeout", type=int, default=45, help="页面加载/搜索超时秒数")
    parser.add_argument("--max-records", type=int, default=0, help="只处理前 N 家；0 表示全部")
    parser.add_argument("--workers", type=int, default=3, help="并发浏览器线程数，范围 1–6")
    parser.add_argument("--no-resume", action="store_true", help="忽略已有 checkpoint")
    args = parser.parse_args()
    if args.delay < 0 or args.timeout < 10 or args.max_records < 0 or not 1 <= args.workers <= 6:
        parser.error("--delay、--timeout、--max-records 或 --workers 参数无效（workers 范围 1–6）")
    if not args.input.exists():
        parser.error(f"输入文件不存在：{args.input}")
    records = load_records(args.input)
    targets = prepare_targets(records, args.country_code)
    if args.max_records:
        targets = targets[:args.max_records]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = args.output_dir / "checkpoint.jsonl"
    completed: set[str] = set()
    if checkpoint.exists() and not args.no_resume:
        with checkpoint.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if row.get("inputKey") and row.get("status") not in {"query_error", "browser_challenge"}:
                        completed.add(row["inputKey"])
                except json.JSONDecodeError:
                    continue
    pending = [row for row in targets if row["inputKey"] not in completed]
    print(f"名单 {len(records)} 条；去重后目标 {len(targets)} 家；已完成 {len(completed)} 家；本轮 {len(pending)} 家。")
    print("搜索方式：Chromium 浏览器自动填写 Goodstack 公开页面；不需要 Goodstack API key。")
    print("日期筛选：both_dates.csv 和 both_dates.json 只输出与名单精确匹配且 createdAt、claimedAt 均有值的记录。")
    proxy = ProxyRotator()
    print(f"代理模式：{proxy.mode}；并发数：{args.workers}")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        parser.error("Playwright 未安装；容器部署会自动安装。")
    completed_this_run = 0
    with checkpoint.open("a", encoding="utf-8") as stream:
        def record_result(result: dict[str, Any]) -> None:
            nonlocal completed_this_run
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
            stream.flush()
            completed_this_run += 1
            print(f"进度 {completed_this_run}/{len(pending)}；最近：{result.get('inputName', '')} → {result.get('status', '')}", flush=True)
            if completed_this_run % 10 == 0:
                save_outputs(args.output_dir, checkpoint, len(targets))
            if result.get("status") == "browser_challenge":
                print("检测到真人验证，停止后续自动查询。请稍后手动处理，脚本不会尝试绕过验证。", file=sys.stderr, flush=True)

        try:
            run_parallel(
                pending,
                min(args.workers, max(1, len(pending))),
                lambda: browser_worker_context(proxy, args.delay, args.timeout),
                record_result,
            )
        except KeyboardInterrupt:
            print("已中断；正在等待进行中的页面搜索收尾，并保留已完成断点。", file=sys.stderr, flush=True)
            save_outputs(args.output_dir, checkpoint, len(targets))
            return 130
        except RuntimeError as exc:
            print(f"任务启动失败：{exc}", file=sys.stderr, flush=True)
            save_outputs(args.output_dir, checkpoint, len(targets))
            return 1
    save_outputs(args.output_dir, checkpoint, len(targets))
    print(f"完成：双日期筛选结果 {args.output_dir / 'both_dates.csv'} 与 {args.output_dir / 'both_dates.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
