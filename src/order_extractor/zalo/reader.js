// In-page reader for chat.zalo.me (design §4.2). Read-only: it never clicks, types or scrolls.
//
// `reader.py` wraps this file as `() => { const SEL = <selectors.json>; <this file>; return JSON.stringify(readState(SEL)); }`
// and passes it to `playwright-cli --raw eval` as one argv element. Every selector comes from `SEL`.
//
// Output (all keys always present):
//   state           "LOGIN" | "NO_CHAT" | "READING"
//   conversation    {key, title, is_group} or null      key = data-qid suffix [R2.13]
//   at_bottom       scrollTop + clientHeight >= scrollHeight - 5 (true when no scroller is found)
//   scroll_top, client_height   of the scroller, for backfill
//   senders         {uid: display name} for bubbles that render a name header [R2.7]
//   messages        [{message_key, sent_at_ms, sender_uid, sender, is_self, kind, text}] in DOM order
//   rendered_bubbles  number of bubbles in the DOM (also when none could be parsed)
//   drift           null, or why the DOM no longer matches the selectors [R2.9]

function readState(SEL) {
  const PLACEHOLDER_KINDS = ["image", "sticker", "file", "voice", "video", "link", "recalled"];
  const result = {
    state: "NO_CHAT",
    conversation: null,
    at_bottom: true,
    scroll_top: 0,
    client_height: 0,
    senders: {},
    messages: [],
    rendered_bubbles: 0,
    drift: null,
  };

  // 1. LOGIN first: both the login title and a logged-in title start with "Zalo - " [R1.2].
  const title = (document.title || "").replace(/^\(\d+\)\s*/, "").trim();
  const loginDom = SEL.login_dom ? document.querySelector(SEL.login_dom) : null;
  if (title === SEL.login_title || loginDom) {
    result.state = "LOGIN";
    return result;
  }

  // 2. NO_CHAT: no message list, or no bubble carries a data-qid (an empty conversation has no key yet) [R1.3].
  const container = document.querySelector(SEL.chat_container);
  if (!container) return result;
  const bubbles = Array.from(container.querySelectorAll(SEL.message_item));
  result.rendered_bubbles = bubbles.length;

  const scroller = document.querySelector(SEL.scroller);
  if (scroller) {
    result.scroll_top = scroller.scrollTop;
    result.client_height = scroller.clientHeight;
    result.at_bottom = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 5;
  }

  const qidRe = new RegExp(SEL.qid_regex);
  const textOf = (node) => (node ? (node.innerText || node.textContent || "").trim() : "");
  const hash = (s) => {
    let h = 0x811c9dc5; // FNV-1a, 32 bit
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 0x01000193) >>> 0;
    }
    return h.toString(16);
  };

  const convKeys = new Set();
  const seen = new Set();
  const sameContent = new Map(); // "<ts>|<name>|<text>" -> qid-less bubbles with that content rendered so far
  bubbles.forEach((bubble) => {
    const item = bubble.closest(SEL.chat_item);
    const qidNode = bubble.matches(SEL.qid_node) ? bubble : bubble.querySelector(SEL.qid_node);
    const match = qidNode ? qidRe.exec(qidNode.getAttribute("data-qid") || "") : null;
    const nameNode = item ? item.querySelector(SEL.sender_name) : null;
    const name = textOf(nameNode) || null;

    let kind = "other";
    let text = "[other]";
    const textNode = bubble.querySelector(SEL.message_text);
    if (textNode) {
      kind = "text";
      text = textOf(textNode);
    } else {
      for (const k of PLACEHOLDER_KINDS) {
        const sel = SEL["kind_" + k];
        if (sel && bubble.querySelector(sel)) {
          kind = k;
          text = "[" + k + "]";
          break;
        }
      }
    }

    let sentAt;
    let uid;
    let key;
    if (match) {
      sentAt = Number(match.groups.ts);
      uid = match.groups.uid;
      key = sentAt + "_" + uid;
      convKeys.add(match.groups.conv);
    } else {
      // No qid on this bubble: time from the `bb_msg_id_<ts>` bubble id, key from a hash of sender + text +
      // ordinal [R2.3]. The ordinal is NOT the bubble's position in the DOM (that shifts when the virtualized
      // list mounts/unmounts bubbles above it, which would store the same message twice, R4.3). It is the
      // number of EARLIER rendered bubbles with the same timestamp and the same sender + text, which stays
      // the same across scrolls and still tells apart identical messages sent in the same millisecond.
      const idMatch = /(\d+)$/.exec(bubble.id || "");
      sentAt = idMatch ? Number(idMatch[1]) : 0;
      uid = "unknown";
      const content = (name || "") + "|" + text;
      const contentKey = sentAt + "|" + content;
      const ordinal = sameContent.get(contentKey) || 0;
      sameContent.set(contentKey, ordinal + 1);
      key = sentAt + "_h" + hash(content + "|" + ordinal);
    }
    if (seen.has(key)) return; // album parts may repeat a key; keep the first
    seen.add(key);

    const isSelf = uid === "0" || (!!item && item.classList.contains(SEL.self_item_class));
    if (name && !isSelf && uid !== "unknown") result.senders[uid] = name;
    result.messages.push({
      message_key: key,
      sent_at_ms: sentAt,
      sender_uid: uid,
      sender: name,
      is_self: isSelf,
      kind: kind,
      text: text,
    });
  });

  if (convKeys.size === 0) {
    // No bubble carries a qid: no chat yet. ZaloWeb raises drift if rendered_bubbles > 0 persists [R2.9].
    result.messages = [];
    result.senders = {};
    return result;
  }

  // 3. READING
  result.state = "READING";
  const keys = Array.from(convKeys);
  if (keys.length > 1) result.drift = "bubbles disagree on the conversation key: " + keys.join(", ");
  const key = keys[0];
  result.conversation = {
    key: key,
    title: textOf(document.querySelector(SEL.conversation_title)) || null,
    is_group: !!document.querySelector(SEL.group_subtitle) || key.startsWith("g"),
  };
  return result;
}
