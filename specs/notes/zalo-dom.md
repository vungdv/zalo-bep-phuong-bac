# Zalo Web DOM notes (discovered 2026-11-19, live session, group chat)

Discovered with `playwright-cli -s=zalo --raw eval` on chat.zalo.me. Re-run task T2 if reading breaks.

## Page state
| State | Signal |
|---|---|
| Logged in | `document.title` = `Zalo - <account name>` (login page: `Zalo - Đăng nhập Zalo`). Both start with `Zalo - `, so check the login title first; an unread prefix like `(3) ` is unverified (T2b) |
| Chat open | `#messageViewContainer` / `[id^="bb_msg_id_"]` present |
| URL | stays `https://chat.zalo.me/` for every conversation → don't use the URL as the conversation key |

## Conversation
- Title: `#header .header-title`
- Group subtitle: `#header .subtitle__groupmember__content` ("4 thành viên"), present only for groups.
- **Key:** the `g<groupId>` suffix of any message `data-qid` (see below). 1-1 chat format is still unverified (expected `u<uid>` or no suffix). Check it in T2b.

## Message bubble
```
div.block-date                                   ← one per day; date label "T5 05/11/2026"
  div.chat-item[.me]                             ← .me = sent by the logged-in account
    div.message-sender-name-content              ← sender display name, ONLY on the first bubble of a run
    div.chat-content
      div.chat-message#bb_msg_id_<tsMs>[data-component=bubble-message]
        div.message-wrapper[--me]
          … [data-qid="<msgRef>[_NNN]@<tsMs>_<senderUid>_g<groupId>"]
              [data-component=message-text-content]   ← text (absent for media)
              .chatImageMessage--audit                 ← image message
              span.card-send-time__sendTime            ← "HH:MM", ONLY on the last bubble of a run
```

### `data-qid` anatomy (the most useful finding)
`8483498480084@1794544726909_3808620345438131900_g1366548983169703056`
- `1794544726909`: **send time in epoch ms** (also equals the `bb_msg_id_` suffix) → exact timestamps, no label parsing needed
- `3808620345438131900`: **sender uid** (stable). `0` = the logged-in account (self)
- `g1366548983169703056`: group id → conversation key
- The `msgRef` prefix is NOT unique for self messages (it reuses the previous message's ref plus `_000/_001`). Don't use it as the id.

**message_key = `<tsMs>_<senderUid>`**

The qid sits on `[data-component=message-content-view]` for text and on a nested node for images. Use `el.querySelector('[data-qid]')`.

## Sender resolution
The name appears only at the start of a run, and that first bubble often scrolls out of the virtualized list.
`reader.js` returns `uid → name` for the bubbles that render a `.message-sender-name-content`; ZaloWeb stores it in
the `senders` table and uses it for bubbles without a header (requirements R2.7). Senders are keyed by uid, so two
members with the same display name stay distinct.

## Scrolling
- Scroll container: `#chatView .transform-gpu` (overflow auto). `#messageViewScroll` is NOT the scroller.
- At bottom when `scrollTop + clientHeight ≥ scrollHeight - 5`.
- About 29 bubbles were rendered at once (the list is virtualized or paged). Backfill scrolls `.transform-gpu` up.
