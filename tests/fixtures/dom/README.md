# Synthetic Zalo Web DOM fixtures

These HTML files are **synthetic and sanitized**: hand-written from `specs/notes/zalo-dom.md` and
`selectors.json`, with invented names, texts, uids and group ids. They contain no real chat data.

They are not copies of the live DOM. Real-DOM fixtures (task T2b: 1-1 chat, album, voice, link card,
recalled message, real unread-count title) need a human Zalo login and are out of scope here; when they
are captured, add them next to these files and extend `tests/test_reader_js.py`.

| File | Expected state |
|---|---|
| `login.html` | LOGIN (title `Zalo - Đăng nhập Zalo`) |
| `login_unread.html` | LOGIN (title `(3) Zalo - Đăng nhập Zalo`) |
| `no_chat.html` | NO_CHAT (logged in, no message list) |
| `empty_conversation.html` | NO_CHAT (message list without bubbles) |
| `group_chat.html` | READING: text, image, sticker, file, other, self, name header only on the first bubble of a run, unread-count title `(3) Zalo - …` |
| `no_qid.html` | NO_CHAT with `rendered_bubbles > 0` (selector drift signal) |
| `mixed_conversations.html` | READING with `drift` (bubbles disagree on the conversation key) |
