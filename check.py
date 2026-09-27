"""캠핑장 취소분(빈자리) 알림 프로그램. 예약은 하지 않고 이메일만 보낸다.
  python check.py          # 감시 모드: LOOP_MINUTES 동안 CHECK_INTERVAL_SEC마다 확인
  python check.py --test   # 점검 모드: 현재 상태 확인 + 테스트 메일 1통

감시 대상 (TARGETS)
  - naver  : 네이버 예약 상품. 캠프1361 A-1,2는 10/30~31이 '예약 수 0 + 판매 닫힘'으로
             보이므로, 예약 가능이 되면 빈자리 알림, 조금이라도 바뀌면 변화 알림을 보낸다.
  - thankq : 땡큐캠핑 사이트 목록에서 이름이 같은 사이트의 '예약가능/예약완료' 표시를 본다.
"""

import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText

import requests

# ─────────────────────────── 설정 ───────────────────────────
WANTED_STAYS = [                            # (체크인, 체크아웃) — 두 밤이 모두 비어야 알림
    ("2026-10-23", "2026-10-25"),
    ("2026-10-30", "2026-11-01"),
]
TARGETS = [
    {"kind": "naver", "label": "캠프1361 · A-1,2 두가족 사이트 (네이버)",
     "biz": "1372898", "item": "6632655", "service": 3},
    {"kind": "thankq", "label": "용인휴캠핑장 · A구역 2가족 사이트 (땡큐캠핑)",
     "camp": "4256", "site": "A구역 2가족 사이트"},
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
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
session = requests.Session()


def now() -> datetime:
    return datetime.now(KST)


def log(*a) -> None:
    print(now().strftime("[%m-%d %H:%M:%S]"), *a, flush=True)


def nights(ci: str, co: str) -> list:
    d0, d1 = datetime.strptime(ci, "%Y-%m-%d"), datetime.strptime(co, "%Y-%m-%d")
    return [(d0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((d1 - d0).days)]


ALL_NIGHTS = sorted({n for s in WANTED_STAYS for n in nights(*s)})


# ─────────────────────────── 네이버 ───────────────────────────
def gql(op: str, query: str, variables: dict) -> dict:
    r = session.post(f"https://m.booking.naver.com/graphql?opName={op}", timeout=15,
                     headers={"Content-Type": "application/json", "User-Agent": UA,
                              "Referer": "https://m.booking.naver.com/"},
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
    """네이버 업체의 예약 상품 목록 {상품번호: 이름} (debug.py에서 사용)."""
    q = "query bizItems($input: BizItemsParams) { bizItems(input: $input) { bizItemId name __typename } }"
    items = gql("bizItems", q, {"input": {"businessId": biz_id}}).get("bizItems") or []
    return {str(i["bizItemId"]): i.get("name") or "" for i in items if i.get("bizItemId")}


def naver_state(t: dict) -> dict:
    """{날짜: (재고, 예약수, 판매중)}"""
    q = ("query schedule($scheduleParams: ScheduleParams) { schedule(input: $scheduleParams) { "
         "bizItemSchedule { daily { date summary { dateKey stock bookingCount isSaleDay "
         "__typename } __typename } __typename } __typename } }")
    d = gql("schedule", q, {"scheduleParams": {
        "businessId": t["biz"], "bizItemId": t["item"], "businessTypeId": t["service"],
        "startDateTime": f"{ALL_NIGHTS[0]}T00:00:00+09:00",
        "endDateTime": f"{ALL_NIGHTS[-1]}T23:59:59+09:00"}})
    out = {}
    for x in d["schedule"]["bizItemSchedule"]["daily"]["summary"] or []:
        out[x["dateKey"][:10]] = (x.get("stock") or 0, x.get("bookingCount") or 0, bool(x.get("isSaleDay")))
    return out


def naver_open(v) -> bool:
    return bool(v) and v[2] and v[0] - v[1] > 0


def naver_text(v) -> str:
    if not v:
        return "?"
    if naver_open(v):
        return f"예약가능 {v[0] - v[1]}"
    return "예약완료" if v[1] >= v[0] > 0 else "판매닫힘"


# ─────────────────────────── 땡큐캠핑 ───────────────────────────
def thankq_sites(camp: str, ci: str, co: str) -> dict:
    """{사이트 이름: 상태 글자 ('예약가능 2', '예약완료', ...)}"""
    days = len(nights(ci, co))
    r = session.post("https://m.thankqcamping.com/resv/axResCampSite.hbb", timeout=15,
                     headers={"User-Agent": UA, "X-Requested-With": "XMLHttpRequest",
                              "Referer": f"https://m.thankqcamping.com/resv/view.hbb?cseq={camp}"},
                     data={"campseq": camp, "res_dt": ci.replace("-", ""), "res_edt": co.replace("-", ""),
                           "res_days": str(days), "site_tp": "", "only_able_yn": ""})
    r.raise_for_status()
    out = {}
    for block in r.text.split('class="site_div')[1:]:
        name = re.search(r'class="na">\s*([^<]+?)\s*<', block)
        tip = re.search(r'class="q_tip[^"]*">\s*([^<]+?)\s*<', block)
        if name and tip:
            out[name.group(1)] = re.sub(r"\s+", " ", tip.group(1))
    if not out:
        raise RuntimeError(f"땡큐캠핑 응답에서 사이트 목록을 찾지 못함 (HTTP {r.status_code}, {len(r.text)}자)")
    return out


def thankq_open(v) -> bool:
    return bool(v) and v.startswith("예약가능")


# ─────────────────────────── 공통 ───────────────────────────
def booking_url(t: dict, ci: str) -> str:
    if t["kind"] == "naver":
        return (f"https://m.booking.naver.com/booking/{t['service']}/bizes/{t['biz']}/items/{t['item']}"
                f"?startDate={ci}")
    return (f"https://m.thankqcamping.com/resv/view.hbb?cseq={t['camp']}"
            f"&res_dt={ci.replace('-', '')}#calendar")


def check_once() -> tuple:
    """(열린 목록 [(대상번호, 체크인, 체크아웃)], 상태 {(대상번호, 키): 글자}, 오류 목록)
    한 사이트가 실패해도 나머지는 계속 확인한다."""
    opens, state, errors = [], {}, []
    for idx, t in enumerate(TARGETS):
        try:
            check_target(idx, t, opens, state)
        except Exception as e:
            log(f"  [조회 실패] {t['label']}: {e}")
            errors.append(f"{t['label']}: {e}")
    if len(errors) == len(TARGETS):
        raise RuntimeError(" / ".join(errors))
    return opens, state, errors


def check_target(idx: int, t: dict, opens: list, state: dict) -> None:
    if t["kind"] == "naver":
        days = naver_state(t)
        for ci, co in WANTED_STAYS:
            ns = nights(ci, co)
            for n in ns:
                state[(idx, n)] = naver_text(days.get(n))
            log(f"  {t['label'][:24]} {ci[5:]}~{co[5:]} → " +
                ", ".join(f"{n[5:]}:{naver_text(days.get(n))}" for n in ns))
            if all(naver_open(days.get(n)) for n in ns):
                opens.append((idx, ci, co))
    else:
        for ci, co in WANTED_STAYS:
            v = thankq_sites(t["camp"], ci, co).get(t["site"])
            state[(idx, ci)] = v or "?"
            log(f"  {t['label'][:24]} {ci[5:]}~{co[5:]} → {v or '사이트 없음'}")
            if thankq_open(v):
                opens.append((idx, ci, co))


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
    return ["", f"확인 시각: {now():%Y-%m-%d %H:%M:%S} (한국시간)"]


def alert_open(opens: list, reminder: bool) -> None:
    head = "[다시 알림] " if reminder else ""
    first = TARGETS[opens[0][0]]["label"].split(" (")[0]
    subject = f"{head}🏕️ 빈자리! {first} {fmt_stay(opens[0][1], opens[0][2])}" + \
              (f" 외 {len(opens) - 1}건" if len(opens) > 1 else "")
    lines = ["예약 가능한 자리가 생겼습니다. 취소분은 금방 나가니 서둘러 주세요!", ""]
    for idx, ci, co in opens:
        t = TARGETS[idx]
        lines += [f"■ {t['label']}", f"  {fmt_stay(ci, co)}", f"  바로 예약: {booking_url(t, ci)}", ""]
    send_mail(subject, "\n".join(lines + footer()))


def alert_change(changes: list) -> None:
    lines = ["원하는 날짜의 예약 상태가 바뀌었습니다. 취소가 났을 수 있으니 예약 화면을 확인해 보세요.", ""]
    for idx, key, before, after in changes:
        t = TARGETS[idx]
        lines += [f"■ {t['label']} {key[5:]}: {before} → {after}", f"  {booking_url(t, key)}"]
    send_mail("🔔 캠핑장 예약 상태 변화 감지", "\n".join(lines + footer()))


# ─────────────────────────── 실행 ───────────────────────────
def run_test() -> int:
    log("=== 점검 모드 ===")
    try:
        opens, _, errors = check_once()
        status = ("지금 예약 가능: " + ", ".join(f"{TARGETS[i]['label']} {fmt_stay(c, o)}" for i, c, o in opens)) \
            if opens else "현재 원하는 날짜는 모두 마감 상태입니다 (정상)."
        ok = not errors
        if errors:
            status += "\n일부 조회 실패: " + " / ".join(errors)
    except Exception as e:
        status, ok = f"조회 실패: {e}", False
        log(f"❌ {status}")
    body = ("테스트 메일입니다. 이 메일이 보이면 메일 설정은 정상입니다.\n\n감시 대상:\n" +
            "\n".join(f"- {t['label']}" for t in TARGETS) +
            "\n원하는 일정: " + " / ".join(fmt_stay(*s) for s in WANTED_STAYS) + f"\n\n상태: {status}\n")
    send_mail(f"[캠핑장 알림] 테스트 메일 ({'정상' if ok else '확인 필요'})", body)
    return 0 if ok else 1


def run_monitor() -> int:
    if now().strftime("%Y-%m-%d") > max(ci for ci, _ in WANTED_STAYS):
        log("원하는 날짜가 모두 지났습니다. 감시를 종료합니다.")
        return 0
    log(f"감시 시작: {', '.join(t['label'] for t in TARGETS)}, {CHECK_INTERVAL_SEC}초 간격")
    end = time.time() + LOOP_MINUTES * 60
    last_sent: dict = {}
    baseline = None
    fail_since, error_mailed = None, False
    while time.time() < end:
        try:
            opens, state, errors = check_once()
            if errors:
                raise_later = RuntimeError(" / ".join(errors))
            else:
                fail_since, error_mailed, raise_later = None, False, None
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
                changes = [(i, k, baseline[(i, k)], v) for (i, k), v in state.items()
                           if v != "?" and baseline.get((i, k), "?") != "?" and v != baseline[(i, k)]]
                if changes:
                    alert_change(changes)
            baseline = {**(baseline or {}), **state}
            if raise_later:
                raise raise_later
        except Exception as e:
            log(f"  [조회 실패] {e}")
            fail_since = fail_since or time.time()
            if not error_mailed and time.time() - fail_since >= ERROR_ALERT_MINUTES * 60:
                send_mail(f"[캠핑장 알림] 조회가 {ERROR_ALERT_MINUTES}분째 실패 중",
                          f"빈자리 확인이 계속 실패하고 있습니다. 예약 사이트 쪽 변경일 수 있습니다.\n\n마지막 오류: {e}")
                error_mailed = True
        time.sleep(CHECK_INTERVAL_SEC)
    log("이번 실행 종료 (다음 실행이 이어서 감시합니다)")
    return 0


if __name__ == "__main__":
    sys.exit(run_test() if "--test" in sys.argv else run_monitor())
