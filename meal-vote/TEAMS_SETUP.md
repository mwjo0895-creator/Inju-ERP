# Teams 워크플로 연결 (한 번만)

식수 서버는 알림 내용을 전부 만들어서 **Teams 워크플로 하나**에 보냅니다.
워크플로는 받은 대로 ① 직원 개인 메시지 ② 채널 게시 ③ 업체 메일을 보내는 배달만 합니다.
지금 쓰고 있는 "워크플로" 봇 메시지와 같은 방식이라 추가 비용이나 IT 신청이 필요 없습니다.

만드는 사람의 계정으로 메일이 나가고 채널 글이 올라갑니다. 공용 메일함이 있으면 그걸로 바꿔도 됩니다 (6단계).

## 1. 새 흐름 만들기

1. [make.powerautomate.com](https://make.powerautomate.com)에 회사 계정으로 들어갑니다.
2. **만들기 → 자동화된 클라우드 흐름 → 건너뛰기**를 누릅니다.
3. 트리거 검색창에 `webhook`을 입력하고 **When a Teams webhook request is received**를 고릅니다.
4. **Who can trigger the flow?** 를 **Anyone**으로 바꿉니다. (식수 서버가 로그인 없이 호출해야 해서입니다.)

## 2. 직원 개인 메시지 (`teamsChat`)

1. 트리거 아래 **+ → 컨트롤 → Apply to each(각각에 적용)**를 추가합니다.
2. 입력 칸에서 **식(fx)** 을 눌러 아래를 넣습니다.
   ```
   triggerBody()?['teamsChat']
   ```
3. 그 안에 **Microsoft Teams → 채팅 또는 채널에 카드 게시 (Post card in a chat or channel)** 를 추가합니다.
   | 칸 | 값 |
   |---|---|
   | 다음으로 게시 (Post as) | Flow bot |
   | 다음에 게시 (Post in) | Chat with Flow bot |
   | 받는 사람 (Recipient) | 식(fx): `item()?['to']` |
   | 적응형 카드 (Adaptive Card) | 식(fx): `item()?['card']` |

## 3. 채널 게시 (`teamsChannel`)

1. 2번 반복문 **밖에** Apply to each를 하나 더 추가하고 입력은 식 `triggerBody()?['teamsChannel']`.
2. 그 안에 **채팅 또는 채널에 카드 게시**:
   | 칸 | 값 |
   |---|---|
   | Post as | Flow bot |
   | Post in | Channel |
   | 팀 / 채널 | 결과를 올릴 팀과 채널을 고릅니다 |
   | Adaptive Card | 식(fx): `item()?['card']` |

## 4. 업체 메일 (`email`)

1. Apply to each를 하나 더, 입력은 식 `triggerBody()?['email']`.
2. 그 안에 **Office 365 Outlook → 전자 메일 보내기 (V2) (Send an email (V2))**:
   | 칸 | 값 |
   |---|---|
   | 받는 사람 (To) | 식(fx): `item()?['to']` |
   | 제목 (Subject) | 식(fx): `item()?['subject']` |
   | 본문 (Body) | 식(fx): `item()?['html']` |
   | 참조 (CC, 고급 옵션) | 참조가 필요할 때만 식(fx): `item()?['cc']` |

## 5. 저장하고 주소 복사

1. **저장**하면 트리거 카드에 **HTTP URL**이 생깁니다. 복사합니다.
2. 식수 서버 폴더의 `config.json`에서 `"webhookUrl": ""` 따옴표 안에 붙여 넣고 서버를 껐다 켭니다.
3. 시험해 봅니다.
   ```
   python server.py send-test 내주소@회사.com
   ```
   내 Teams 개인 메시지, 고른 채널, 내 메일함에 "식수 투표 연결 시험"이 각각 오면 끝입니다.

## 6. 선택 사항

- **공용 메일함에서 보내기**: 4번의 동작을 **공유 사서함에서 전자 메일 보내기 (V2)** 로 바꿉니다.
- **SharePoint 정산 엑셀에 자동으로 쌓기**: 확정 통보에는 분류별 줄이 `records`로 함께 옵니다.
  엑셀에 `날짜, 식사, 분류, 정기, 추가, 합계, 특이사항` 머리글로 **표**를 만든 뒤,
  Apply to each(식 `triggerBody()?['records']`) 안에 **Excel Online (Business) → 표에 행 추가**를 넣고
  각 열에 `item()?['date']`, `item()?['meal']`, `item()?['group']`, `item()?['regular']`, `item()?['extra']`,
  `item()?['total']`, `item()?['note']`를 넣습니다.
- **빨리 보내기**: 35명에게 한 명씩 보내느라 1분쯤 걸립니다. 2번 Apply to each의 설정에서 동시성을 켜고 10 정도로 두면 빨라집니다.

## 서버가 보내는 내용

목록 네 개는 비어 있어도 항상 옵니다.

```json
{
  "kind": "open | remind | tally | final | test",
  "date": "2026-10-22",
  "meal": "점심",
  "teamsChat":    [{ "to": "직원 메일", "card": { "type": "AdaptiveCard", "...": "..." } }],
  "teamsChannel": [{ "card": { "type": "AdaptiveCard", "...": "..." } }],
  "email":        [{ "to": "업체 메일;...", "cc": "", "subject": "...", "html": "..." }],
  "records":      [{ "date": "...", "meal": "...", "group": "...", "regular": 0, "extra": 0, "total": 0, "note": "" }]
}
```

워크플로 호출이 실패하면 서버가 2분마다 다시 시도합니다. 서버 창에 `[알림] ... 보내기 실패`가 계속 보이면
워크플로 실행 기록(Power Automate → 내 흐름 → 28일 실행 기록)을 확인하세요.
