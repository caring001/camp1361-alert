"""캠프1361 취소분(빈자리) 알림 프로그램.

네이버 예약에서 원하는 사이트의 원하는 날짜가 비면 이메일을 보낸다. 예약은 하지 않는다.
  python check.py          # 감시 모드: LOOP_MINUTES 동안 CHECK_INTERVAL_SEC마다 확인
  python check.py --test   # 점검 모드: 현재 상태 확인 + 테스트 메일 1통

10/30~31은 예약 수 0인데 '판매 닫힘'으로 나온다(예약 방식 차이로 보임). 그래서
'예약 가능'이 되면 빈자리 알림을, 예약 수나 판매 여부가 조금이라도 바뀌면 변화 알림을 보낸다.
"""

import json
import os
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText

import requests

# ─────────────────────────── 설정 ───────────────────────────
PLACE_ID = "1162550635"
PLACE_NAME = "캠프1361"
BIZ_ID = "1372898"
SERVICE_ID = 3
ITEMS = {                                   # 상품번호: (이름, 주 감시 대상 여부)
    "6632655": ("[9,10월 주말] A-1,2 두가족 사이트(2박우선)", True),
}
WANTED_STAYS = [                            # (체크인, 체크아웃) — 두 밤이 모두 비어야 알림
    ("2026-10-23", "2026-10-25"),
    ("2026-10-30", "2026-11-01"),
]
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "90"))
LOOP_MINUTES = float(os.getenv("LOOP_MINUTES", "330"))
REMIND_MINUTES = 30
ERROR_ALERT_MINUTES = 60

MAIL_USER = os.getenv("NAVER_MAIL_ID", "").strip()
MAIL_PASSWORD = os.getenv("NAVER_MAIL_PASSWORD", "").strip()
MAIL_TO = os.getenv("MAIL_TO", "").strip()
# ────────────────────────────────────────────────────────────

KST = timezone(timedelta(hours=9))
GRAPHQL = "https://m.booking.naver.com/graphql"
HEADERS = {"Content-Type": "application/json", "Referer": "https://m.booking.naver.com/",
           "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")}
session = requests.Session()


def now() -> datetime:
    return datetime.now(KST)


def log(*a) -> None:
    print(now().strftime("[%m-%d %H:%M:%S]"), *a, flush=True)


def gql(op: str, query: str, variables: dict) -> dict:
    r = session.post(f"{GRAPHQL}?opName={op}", headers=HEADERS, timeout=15,
                     json={"operationName": op, "variables": variables, "query": query})
    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    if data.get("errors"):
        raise RuntimeError("GraphQL 오류: " + "; ".join(str(e.get("message", e))[:150] for e in data["errors"]))
    r.raise_for_status()
    return data["data"]


def list_items(biz_id: str) -> dict:
    """업체의 예약 상품 목록 {상품번호: 이름} (debug.py에서 사용)."""
    q = ("query bizItems($input: BizItemsParams) { bizItems(input: $input) "
         "{ bizItemId name __typename } }")
    items = gql("bizItems", q, {"input": {"businessId": biz_id}}).get("bizItems") or []
    return {str(i["bizItemId"]): i.get("name") or "" for i in items if i.get("bizItemId")}


def booking_url(item_id: str, checkin: str = "") -> str:
    u = f"https://m.booking.naver.com/booking/{SERVICE_ID}/bizes/{BIZ_ID}/items/{item_id}"
    return u + (f"?startDate={checkin}" if checkin else "")


def nights(ci: str, co: str) -> list:
    d0, d1 = datetime.strptime(ci, "%Y-%m-%d"), datetime.strptime(co, "%Y-%m-%d")
    return [(d0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((d1 - d0).days)]


ALL_NIGHTS = sorted({n for s in WANTED_STAYS for n in nights(*s)})


def fetch_item(item_id: str) -> dict:
    """{날짜: (재고, 예약수, 판매중)}"""
    q = ("query schedule($scheduleParams: ScheduleParams) { schedule(input: $scheduleParams) { "
         "bizItemSchedule { daily { date summary { dateKey stock bookingCount isSaleDay "
         "__typename } __typename } __typename } __typename } }")
    d = gql("schedule", q, {"scheduleParams": {
        "businessId": BIZ_ID, "bizItemId": item_id, "businessTypeId": SERVICE_ID,
        "startDateTime": f"{ALL_NIGHTS[0]}T00:00:00+09:00",
        "endDateTime": f"{ALL_NIGHTS[-1]}T23:59:59+09:00"}})
    out = {}
    for x in d["schedule"]["bizItemSchedule"]["daily"]["summary"] or []:
        out[x["dateKey"][:10]] = (x.get("stock") or 0, x.get("bookingCount") or 0, bool(x.get("isSaleDay")))
    return out


def is_open(v) -> bool:
    return bool(v) and v[2] and v[0] - v[1] > 0


def fmt_state(v) -> str:
    if not v:
        return "?"
    if is_open(v):
        return f"가능({v[0] - v[1]})"
    return "예약됨" if v[1] >= v[0] > 0 else "판매닫힘"


def check_once() -> tuple:
    """(열린 목록 [(상품번호, 체크인, 체크아웃)], 원본 상태 {(상품, 날짜): 값})"""
    opens, state = [], {}
    for item_id, (name, _) in ITEMS.items():
        days = fetch_item(item_id)
        for n in ALL_NIGHTS:
            state[(item_id, n)] = days.get(n)
        for ci, co in WANTED_STAYS:
            ns = nights(ci, co)
            log(f"  {name[:22]} {ci[5:]}~{co[5:]} → " + ", ".join(f"{n[5:]}:{fmt_state(days.get(n))}" for n in ns))
            if all(is_open(days.get(n)) for n in ns):
                opens.append((item_id, ci, co))
    return opens, state


# ─────────────────────────── 메일 ───────────────────────────
def send_mail(subject: str, body: str) -> bool:
    if not (MAIL_USER and MAIL_PASSWORD and MAIL_TO):
        log("  [메일 생략] 메일 설정이 비어 있습니다.")
        return False
    sender = MAIL_USER if "@" in MAIL_USER else f"{MAIL_USER}@naver.com"
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = Header(subject, "utf-8"), sender, MAIL_TO
    for attempt in range(3):
        try:
            with smtplib.SMTP_SSL("smtp.naver.com", 465, timeout=30) as s:
                s.login(sender.split("@")[0], MAIL_PASSWORD)
                s.sendmail(sender, [MAIL_TO], msg.as_string())
            log(f"  [메일 발송] {subject}")
            return True
        except Exception as e:
            log(f"  [메일 실패 {attempt + 1}/3] {e}")
            time.sleep(5)
    return False


def fmt_stay(ci: str, co: str) -> str:
    wd = "월화수목금토일"
    a, b = datetime.strptime(ci, "%Y-%m-%d"), datetime.strptime(co, "%Y-%m-%d")
    return f"{a.month}/{a.day}({wd[a.weekday()]}) ~ {b.month}/{b.day}({wd[b.weekday()]})"


def footer() -> list:
    return ["", f"지도에서 보기: https://map.naver.com/p/entry/place/{PLACE_ID}",
            f"확인 시각: {now():%Y-%m-%d %H:%M:%S} (한국시간)"]


def alert_open(opens: list, reminder: bool) -> None:
    main = [o for o in opens if ITEMS[o[0]][1]]
    head = "[다시 알림] " if reminder else ""
    if main:
        subject = f"{head}🏕️ {PLACE_NAME} A-1,2 빈자리! " + " / ".join(fmt_stay(c, o) for _, c, o in main)
    else:
        subject = f"{head}🏕️ {PLACE_NAME} 단체동 자리 남 (A-1,2와 묶인 상품) " + \
                  " / ".join(fmt_stay(c, o) for _, c, o in opens)
    lines = [f"{PLACE_NAME}에 예약 가능한 자리가 생겼습니다. 취소분은 금방 나가니 서둘러 주세요!", ""]
    for item_id, ci, co in opens:
        lines += [f"■ {ITEMS[item_id][0]}", f"  {fmt_stay(ci, co)}",
                  f"  바로 예약: {booking_url(item_id, ci)}", ""]
    if not main:
        lines.append("※ A-1,2가 이 상품과 묶여 있어 곧 A-1,2도 열릴 수 있습니다. 예약 화면을 확인해 주세요.")
    send_mail(subject, "\n".join(lines + footer()))


def alert_change(changes: list) -> None:
    lines = [f"{PLACE_NAME} 원하는 날짜의 상태가 바뀌었습니다. 취소가 났을 수 있으니 예약 화면을 확인해 보세요.", ""]
    for item_id, n, before, after in changes:
        lines.append(f"■ {ITEMS[item_id][0]} {n[5:]}: {fmt_state(before)} → {fmt_state(after)}"
                     f"  (재고/예약수 {before and before[:2]} → {after and after[:2]})")
        lines.append(f"  {booking_url(item_id, n)}")
    send_mail(f"🔔 {PLACE_NAME} 예약 상태 변화 감지", "\n".join(lines + footer()))


# ─────────────────────────── 실행 ───────────────────────────
def run_test() -> int:
    log("=== 점검 모드 ===")
    try:
        opens, _ = check_once()
        status = ("지금 예약 가능: " + ", ".join(f"{ITEMS[i][0][:20]} {fmt_stay(c, o)}" for i, c, o in opens)) \
            if opens else "현재 원하는 날짜는 모두 마감 상태입니다 (정상)."
        ok = True
    except Exception as e:
        status, ok = f"조회 실패: {e}", False
        log(f"❌ {status}")
    body = ("테스트 메일입니다. 이 메일이 보이면 메일 설정은 정상입니다.\n\n감시 대상:\n" +
            "\n".join(f"- {n} ({booking_url(i)})" for i, (n, _) in ITEMS.items()) +
            "\n원하는 일정: " + " / ".join(fmt_stay(*s) for s in WANTED_STAYS) + f"\n\n상태: {status}\n")
    send_mail(f"[{PLACE_NAME} 알림] 테스트 메일 ({'정상' if ok else '확인 필요'})", body)
    return 0 if ok else 1


def run_monitor() -> int:
    if now().strftime("%Y-%m-%d") > max(ci for ci, _ in WANTED_STAYS):
        log("원하는 날짜가 모두 지났습니다. 감시를 종료합니다.")
        return 0
    log(f"감시 시작: {', '.join(n for n, _ in ITEMS.values())}, {CHECK_INTERVAL_SEC}초 간격")
    end = time.time() + LOOP_MINUTES * 60
    last_sent: dict = {}
    baseline = None
    fail_since, error_mailed = None, False
    while time.time() < end:
        try:
            opens, state = check_once()
            fail_since, error_mailed = None, False
            # 1) 예약 가능 → 빈자리 알림 (계속 비어 있으면 30분마다 다시)
            keys = set(opens)
            due = [o for o in opens if o not in last_sent or time.time() - last_sent[o] >= REMIND_MINUTES * 60]
            if due:
                alert_open(due, all(o in last_sent for o in due))
                for o in due:
                    last_sent[o] = time.time()
            for k in list(last_sent):
                if k not in keys:
                    del last_sent[k]
            # 2) 예약 가능까지는 아니어도 상태가 바뀌면 → 변화 알림
            if baseline is not None and not due:
                changes = [(i, n, baseline.get((i, n)), v) for (i, n), v in state.items()
                           if v is not None and baseline.get((i, n)) is not None and v != baseline[(i, n)]]
                if changes:
                    alert_change(changes)
            baseline = state
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
