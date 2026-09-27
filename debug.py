"""점검용: 원하는 날짜의 네이버 원본 응답을 그대로 출력한다."""
import json
import check as c

t = {"service_id": "3", "biz_id": "1372898", "item_id": "6632655"}
params = {"businessId": t["biz_id"], "bizItemId": t["item_id"], "businessTypeId": 3,
          "startDateTime": "2026-10-20T00:00:00+09:00", "endDateTime": "2026-11-02T23:59:59+09:00"}
for fields in ["saleStartDate saleEndDate", ""]:
    q = ("query schedule($scheduleParams: ScheduleParams) { schedule(input: $scheduleParams) { "
         "bizItemSchedule { " + fields + " daily { date summary { dateKey stock bookingCount "
         "hasBookableSlots isSaleDay isBusinessDay minBookingCount maxBookingCount __typename } __typename } __typename } __typename } }")
    try:
        d = c.gql("schedule", q, {"scheduleParams": params})
        s = d["schedule"]["bizItemSchedule"]
        print("saleStart/End:", s.get("saleStartDate"), s.get("saleEndDate"))
        for day in s["daily"]["summary"]:
            print(json.dumps(day, ensure_ascii=False))
        break
    except Exception as e:
        print("실패:", fields, e)

q2 = ("query bizItem($businessId: String!, $bizItemId: String!) { bizItem(input: { businessId: $businessId, "
      "bizItemId: $bizItemId }) { name minBookingCount maxBookingCount bookingCountSettingJson "
      "bookableSettingJson __typename } }")
try:
    print(json.dumps(c.gql("bizItem", q2, {"businessId": t["biz_id"], "bizItemId": t["item_id"]}),
                     ensure_ascii=False)[:2000])
except Exception as e:
    print("bizItem 실패:", e)
