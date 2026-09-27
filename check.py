"""캠프1361 취소분(빈자리) 알림 프로그램.

네이버 예약에서 원하는 사이트의 원하는 날짜가 비면 이메일을 보낸다.
GitHub Actions에서 24시간 돌아가도록 만들었다. 예약은 하지 않고 알림만 보낸다.

실행 모드
  python check.py          # 감시 모드: LOOP_MINUTES 동안 CHECK_INTERVAL_SEC마다 확인
  python check.py --test   # 점검 모드: 예약 번호 찾기 + 현재 상태 확인 + 테스트 메일 1통
"""

import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.header import Header

import requests

# ─────────────────────────── 설정 ───────────────────────────
PLACE_ID = "1162550635"                      # 네이버 지도 장소 번호 (캠프1361)
PLACE_NAME = "캠프1361"
SITE_KEYWORDS = ["A-1", "A-2", "두가족"]      # 상품 이름에 이 단어가 많이 들어갈수록 우선
# 원하는 일정: (체크인, 체크아웃). 2박이므로 두 밤이 모두 비어야 알림.
WANTED_STAYS = [
    ("2026-10-23", "2026-10-25"),
    ("2026-10-30", "2026-11-01"),
]

# 자동으로 못 찾을 때를 대비한 수동 지정 (GitHub 저장소 Variables로 넣을 수 있음)
BIZ_ID = os.getenv("BIZ_ID", "").strip()
ITEM_ID = os.getenv("ITEM_ID", "").strip()
SERVICE_ID = os.getenv("SERVICE_ID", "").strip()

CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "90"))
LOOP_MINUTES = float(os.getenv("LOOP_MINUTES", "330"))     # 한 번 실행에 5시간 30분
REMIND_MINUTES = int(os.getenv("REMIND_MINUTES", "30"))     # 계속 비어 있으면 30분마다 다시 알림
ERROR_ALERT_MINUTES = int(os.getenv("ERROR_ALERT_MINUTES", "60"))  # 이만큼 연속 실패하면 오류 메일

MAIL_USER = os.getenv("NAVER_MAIL_ID", "").strip()          # 예: nalim1310 (또는 nalim1310@naver.com)
MAIL_PASSWORD = os.getenv("NAVER_MAIL_PASSWORD", "").strip()
MAIL_TO = os.getenv("MAIL_TO", "").strip()
# ────────────────────────────────────────────────────────────

KST = timezone(timedelta(hours=9))
GRAPHQL = "https://m.booking.naver.com/graphql"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADERS = {"Content-Type": "application/json", "User-Agent": UA,
           "Referer": "https://m.booking.naver.com/"}
PAGE_HEADERS = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"}
SERVICE_CANDIDATES = ["3", "5", "6", "12", "13"]

session = requests.Session()


def now() -> datetime:
    return datetime.now(KST)


def log(*a) -> None:
    print(now().strftime("[%m-%d %H:%M:%S]"), *a, flush=True)


# ───────────────────── 1. 예약 번호 찾기 ─────────────────────
def _fetch(url: str) -> str:
    try:
        r = session.get(url, headers=PAGE_HEADERS, timeout=15)
        log(f"  페이지 {r.status_code} {url}")
        return r.text if r.status_code == 200 else ""
    except Exception as e:
        log(f"  페이지 실패 {url}: {e}")
        return ""


def gql(op: str, query: str, variables: dict) -> dict:
    r = session.post(f"{GRAPHQL}?opName={op}", headers=HEADERS, timeout=15,
                     json={"operationName": op, "variables": variables, "query": query})
    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    if data.get("errors"):
        msg = "; ".join(str(e.get("message", e))[:150] for e in data["errors"])
        raise RuntimeError(f"GraphQL 오류: {msg}")
    r.raise_for_status()
    return data["data"]


def discover_from_place() -> dict:
    """지도 장소 페이지에서 업체 번호(bizId), 서비스 종류, 상품 후보를 긁어낸다."""
    found = {"biz_ids": [], "service_id": "", "items": {}}
    pages = [
        f"https://m.place.naver.com/accommodation/{PLACE_ID}/room",
        f"https://pcmap.place.naver.com/accommodation/{PLACE_ID}/room",
        f"https://m.place.naver.com/place/{PLACE_ID}/booking",
        f"https://m.place.naver.com/place/{PLACE_ID}/home",
    ]
    for url in pages:
        html = _fetch(url)
        if not html:
            continue
        for svc, biz, item in re.findall(r"booking/(\d+)/bizes/(\d+)(?:/items/(\d+))?", html):
            found["service_id"] = found["service_id"] or svc
            if biz not in found["biz_ids"]:
                found["biz_ids"].append(biz)
            if item:
                found["items"].setdefault(item, "")
        for biz in re.findall(r'"(?:bookingBusinessId|businessId)"\s*:\s*"?(\d{5,})', html):
            if biz not in found["biz_ids"]:
                found["biz_ids"].append(biz)
        # "bizItemId":"123", ... "name":"..." 쌍
        for m in re.finditer(r'"bizItemId"\s*:\s*"?(\d+)"?', html):
            item = m.group(1)
            window = html[m.start(): m.start() + 1500]
            name = re.search(r'"(?:name|title)"\s*:\s*"([^"]{2,80})"', window)
            if name and not found["items"].get(item):
                found["items"][item] = name.group(1)
            else:
                found["items"].setdefault(item, "")
        if found["biz_ids"] and found["items"]:
            break
    return found


def list_items(biz_id: str) -> dict:
    """업체의 예약 상품 목록 {상품번호: 이름}."""
    tries = [
        ("bizItems", "query bizItems($input: BizItemsParams) { bizItems(input: $input) "
                     "{ bizItemId name __typename } }", {"input": {"businessId": biz_id}}),
        ("bizItems", "query bizItems($businessId: String) { bizItems(input: { businessId: $businessId }) "
                     "{ bizItemId name __typename } }", {"businessId": biz_id}),
    ]
    for op, q, v in tries:
        try:
            items = gql(op, q, v).get("bizItems") or []
            out = {str(i["bizItemId"]): i.get("name") or "" for i in items if i.get("bizItemId")}
            if out:
                return out
        except Exception as e:
            log(f"  상품 목록 조회 실패: {e}")
    return {}


def item_name(biz_id: str, item_id: str) -> str:
    try:
        d = gql("bizItem", "query bizItem($businessId: String!, $bizItemId: String!) { "
                "bizItem(input: { businessId: $businessId, bizItemId: $bizItemId }) { name __typename } }",
                {"businessId": biz_id, "bizItemId": item_id})
        return (d.get("bizItem") or {}).get("name") or ""
    except Exception as e:
        log(f"  상품 이름 조회 실패({item_id}): {e}")
        return ""


def score(name: str) -> int:
    n = name.replace(" ", "")
    return sum(1 for k in SITE_KEYWORDS if k.replace(" ", "") in n)


def resolve_target() -> dict:
    """감시할 {service_id, biz_id, item_id, name}을 정한다."""
    svc, biz, item = SERVICE_ID, BIZ_ID, ITEM_ID
    items: dict = {}
    if not (biz and item):
        log("예약 번호 자동 찾기 시작")
        f = discover_from_place()
        log(f"  찾은 업체 번호: {f['biz_ids']}, 서비스: {f['service_id'] or '?'}, 상품: {f['items']}")
        svc = svc or f["service_id"]
        biz = biz or (f["biz_ids"][0] if f["biz_ids"] else "")
        items = dict(f["items"])
    if not biz:
        raise RuntimeError("업체 번호(bizId)를 찾지 못했습니다. README의 '수동 지정' 방법을 따라 주세요.")
    if not item:
        more = list_items(biz)
        items.update({k: v or items.get(k, "") for k, v in more.items()})
        for k in list(items):
            if not items[k]:
                items[k] = item_name(biz, k)
        log("  상품 목록:")
        for k, v in items.items():
            log(f"    {k}  {v}")
        if not items:
            raise RuntimeError("상품 목록을 찾지 못했습니다. README의 '수동 지정' 방법을 따라 주세요.")
        item = max(items, key=lambda k: score(items[k]))
        if score(items[item]) == 0:
            raise RuntimeError("'A-1' 사이트 상품을 이름으로 찾지 못했습니다. 위 목록을 보고 ITEM_ID를 지정해 주세요.")
    name = items.get(item) or item_name(biz, item) or item
    if not svc:
        svc = "3"
    return {"service_id": svc, "biz_id": biz, "item_id": item, "name": name}


def booking_url(t: dict, checkin: str = "") -> str:
    u = f"https://m.booking.naver.com/booking/{t['service_id']}/bizes/{t['biz_id']}/items/{t['item_id']}"
    return u + (f"?startDate={checkin}" if checkin else "")


# ───────────────────── 2. 빈자리 확인 ─────────────────────
def nights(checkin: str, checkout: str) -> list:
    d0 = datetime.strptime(checkin, "%Y-%m-%d")
    d1 = datetime.strptime(checkout, "%Y-%m-%d")
    return [(d0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((d1 - d0).days)]


def fetch_daily(t: dict) -> dict:
    """{날짜: {'free': 남은수, 'sale': 판매일여부, 'raw': ...}}"""
    all_nights = sorted({n for s in WANTED_STAYS for n in nights(*s)})
    params = {
        "businessId": t["biz_id"], "bizItemId": t["item_id"],
        "businessTypeId": int(t["service_id"]),
        "startDateTime": f"{all_nights[0]}T00:00:00+09:00",
        "endDateTime": f"{all_nights[-1]}T23:59:59+09:00",
    }
    q = ("query schedule($scheduleParams: ScheduleParams) { schedule(input: $scheduleParams) { "
         "bizItemSchedule { daily { date summary { dateKey stock bookingCount hasBookableSlots "
         "isSaleDay __typename } __typename } __typename } __typename } }")
    d = gql("schedule", q, {"scheduleParams": params})
    summary = d["schedule"]["bizItemSchedule"]["daily"]["summary"] or []
    out = {}
    for day in summary:
        key = (day.get("dateKey") or "")[:10]
        free = (day.get("stock") or 0) - (day.get("bookingCount") or 0)
        out[key] = {"free": free, "sale": bool(day.get("isSaleDay")),
                    "bookable": day.get("hasBookableSlots"), "raw": day}
    return out


def night_open(info: dict | None) -> bool:
    if not info or not info["sale"]:
        return False
    if info["bookable"] is False:
        return False
    return info["free"] > 0


def check_once(t: dict) -> list:
    """예약 가능한 일정 목록 [(체크인, 체크아웃, 남은수)]."""
    daily = fetch_daily(t)
    result = []
    for ci, co in WANTED_STAYS:
        ns = nights(ci, co)
        infos = [daily.get(n) for n in ns]
        status = ", ".join(f"{n[5:]}:{'가능' if night_open(i) else '마감'}"
                           f"({i['free'] if i else '?'})" for n, i in zip(ns, infos))
        log(f"  {ci[5:]}~{co[5:]} → {status}")
        if all(night_open(i) for i in infos):
            result.append((ci, co, min(i["free"] for i in infos)))
    return result


# ───────────────────── 3. 이메일 ─────────────────────
def send_mail(subject: str, body: str) -> bool:
    if not (MAIL_USER and MAIL_PASSWORD and MAIL_TO):
        log("  [메일 생략] NAVER_MAIL_ID / NAVER_MAIL_PASSWORD / MAIL_TO 설정이 비어 있습니다.")
        return False
    sender = MAIL_USER if "@" in MAIL_USER else f"{MAIL_USER}@naver.com"
    login = sender.split("@")[0]
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = sender
    msg["To"] = MAIL_TO
    for attempt in range(3):
        try:
            with smtplib.SMTP_SSL("smtp.naver.com", 465, timeout=30) as s:
                s.login(login, MAIL_PASSWORD)
                s.sendmail(sender, [MAIL_TO], msg.as_string())
            log(f"  [메일 발송] {subject}")
            return True
        except Exception as e:
            log(f"  [메일 실패 {attempt + 1}/3] {e}")
            time.sleep(5)
    return False


def fmt_stay(ci: str, co: str) -> str:
    wd = "월화수목금토일"
    a = datetime.strptime(ci, "%Y-%m-%d")
    b = datetime.strptime(co, "%Y-%m-%d")
    return f"{a.month}/{a.day}({wd[a.weekday()]}) ~ {b.month}/{b.day}({wd[b.weekday()]})"


def alert_open(t: dict, opens: list, reminder: bool) -> None:
    stays = " / ".join(fmt_stay(ci, co) for ci, co, _ in opens)
    head = "[다시 알림] " if reminder else ""
    subject = f"{head}🏕️ {PLACE_NAME} 빈자리! {stays}"
    lines = [f"{PLACE_NAME} '{t['name']}' 예약 가능한 자리가 생겼습니다.", ""]
    for ci, co, free in opens:
        lines.append(f"■ {fmt_stay(ci, co)}  (남은 자리 {free})")
        lines.append(f"  바로 예약: {booking_url(t, ci)}")
        lines.append("")
    lines += [f"지도에서 보기: https://map.naver.com/p/entry/place/{PLACE_ID}",
              "", f"확인 시각: {now():%Y-%m-%d %H:%M:%S} (한국시간)",
              "취소분은 금방 나가니 서둘러 주세요!"]
    send_mail(subject, "\n".join(lines))


# ───────────────────── 실행 ─────────────────────
def last_checkout_passed() -> bool:
    last = max(ci for ci, _ in WANTED_STAYS)
    return now().strftime("%Y-%m-%d") > last


def run_test() -> int:
    log("=== 점검 모드 ===")
    try:
        t = resolve_target()
    except Exception as e:
        log(f"❌ {e}")
        send_mail(f"[{PLACE_NAME} 알림] 점검 실패", f"예약 번호를 찾지 못했습니다.\n\n{e}\n\n"
                  "GitHub Actions 실행 기록(로그)을 확인해 주세요.")
        return 1
    log(f"✅ 감시 대상: {t['name']}  ({booking_url(t)})")
    try:
        opens = check_once(t)
        status = ("지금 예약 가능: " + ", ".join(fmt_stay(c, o) for c, o, _ in opens)) if opens \
            else "현재 원하는 날짜는 모두 마감 상태입니다 (정상)."
        ok = True
    except Exception as e:
        status, ok = f"빈자리 조회 실패: {e}", False
        log(f"❌ {status}")
    body = (f"테스트 메일입니다. 이 메일이 보이면 메일 설정은 정상입니다.\n\n"
            f"감시 대상: {t['name']}\n예약 링크: {booking_url(t)}\n"
            f"원하는 일정: " + " / ".join(fmt_stay(*s) for s in WANTED_STAYS) + f"\n\n상태: {status}\n")
    send_mail(f"[{PLACE_NAME} 알림] 테스트 메일 ({'정상' if ok else '확인 필요'})", body)
    return 0 if ok else 1


def run_monitor() -> int:
    if last_checkout_passed():
        log("원하는 날짜가 모두 지났습니다. 감시를 종료합니다.")
        return 0
    end = time.time() + LOOP_MINUTES * 60
    t = None
    while t is None:
        try:
            t = resolve_target()
        except Exception as e:
            log(f"❌ 예약 번호 찾기 실패: {e}")
            if time.time() + 600 > end:
                return 1
            time.sleep(600)            # 10분 뒤 다시 시도 (오류 메일은 점검 모드에서만)
    log(f"감시 시작: {t['name']}  ({booking_url(t)}), {CHECK_INTERVAL_SEC}초 간격")
    last_sent: dict = {}          # 일정 → 마지막 알림 시각
    fail_since = None
    error_mailed = False
    while time.time() < end:
        try:
            opens = check_once(t)
            fail_since, error_mailed = None, False
            open_keys = {(c, o) for c, o, _ in opens}
            due = [x for x in opens if (x[0], x[1]) not in last_sent
                   or time.time() - last_sent[(x[0], x[1])] >= REMIND_MINUTES * 60]
            if due:
                reminder = all((c, o) in last_sent for c, o, _ in due)
                alert_open(t, due, reminder)
                for c, o, _ in due:
                    last_sent[(c, o)] = time.time()
            for k in list(last_sent):          # 다시 마감되면 기록 삭제 → 다음에 새 알림
                if k not in open_keys:
                    del last_sent[k]
        except Exception as e:
            log(f"  [조회 실패] {e}")
            fail_since = fail_since or time.time()
            if not error_mailed and time.time() - fail_since >= ERROR_ALERT_MINUTES * 60:
                send_mail(f"[{PLACE_NAME} 알림] 조회가 {ERROR_ALERT_MINUTES}분째 실패 중",
                          f"빈자리 확인이 계속 실패하고 있습니다. 네이버 쪽 변경일 수 있습니다.\n\n마지막 오류: {e}")
                error_mailed = True
        time.sleep(CHECK_INTERVAL_SEC)
    log("이번 실행 종료 (다음 실행이 이어서 감시합니다)")
    return 0


if __name__ == "__main__":
    sys.exit(run_test() if "--test" in sys.argv else run_monitor())
