# 캠프1361 빈자리 알림

네이버 예약에서 **캠프1361 · [9,10월 주말] A-1,2 두가족 사이트**의 아래 일정이 비면 이메일을 보냅니다.

- 2026-10-23(금) ~ 10-25(일)
- 2026-10-30(금) ~ 11-01(일)

GitHub Actions에서 24시간 돌아가며 90초마다 확인합니다. 예약은 하지 않고 알림만 보냅니다.
자리가 계속 비어 있으면 30분마다 다시 알려 주고, 10/31이 지나면 스스로 멈춥니다.

## 사용법
- **점검**: Actions → 점검 (테스트 메일) → Run workflow. 1분 뒤 테스트 메일이 옵니다.
- **감시 시작**: Actions → 빈자리 감시 → Run workflow.
- **멈추기**: Actions → 빈자리 감시 → 오른쪽 위 ··· → Disable workflow. 예약에 성공했거나 10/31 이후에는 꺼 주세요.
- **날짜 바꾸기**: check.py 위쪽의 WANTED_STAYS를 고칩니다.
- **오류 알림**: 조회가 1시간 넘게 계속 실패하면 오류 메일이 옵니다.

## 설정값 (Settings → Secrets and variables → Actions)
- Secrets: NAVER_MAIL_ID, NAVER_MAIL_PASSWORD, MAIL_TO
- Variables (자동 찾기가 실패할 때만): SERVICE_ID, BIZ_ID, ITEM_ID
