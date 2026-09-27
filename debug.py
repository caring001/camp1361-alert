"""점검용: 모든 사이트의 10/23·24·30·31 원본 상태를 출력한다."""
import check as c

BIZ = "1372898"
DAYS = ["2026-10-23", "2026-10-24", "2026-10-30", "2026-10-31"]
q = ("query schedule($scheduleParams: ScheduleParams) { schedule(input: $scheduleParams) { "
     "bizItemSchedule { daily { date summary { dateKey stock bookingCount isSaleDay isBusinessDay "
     "__typename } __typename } __typename } __typename } }")
items = c.list_items(BIZ)
print("형식: 날짜=재고/예약수/판매중(S)·영업일(B)")
for item_id, name in items.items():
    try:
        d = c.gql("schedule", q, {"scheduleParams": {
            "businessId": BIZ, "bizItemId": item_id, "businessTypeId": 3,
            "startDateTime": "2026-10-23T00:00:00+09:00", "endDateTime": "2026-10-31T23:59:59+09:00"}})
        rows = {x["dateKey"][:10]: x for x in d["schedule"]["bizItemSchedule"]["daily"]["summary"]}
        parts = []
        for day in DAYS:
            x = rows.get(day) or {}
            parts.append(f"{day[5:]}={x.get('stock')}/{x.get('bookingCount')}/"
                         f"{'S' if x.get('isSaleDay') else '-'}{'B' if x.get('isBusinessDay') else '-'}")
        print(f"{item_id} {name[:28]:<28} " + "  ".join(parts))
    except Exception as e:
        print(item_id, name, "실패", e)
