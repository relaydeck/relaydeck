// conversations.js — pure helpers for the Telegram lens send-picker.
//
// Deliberately dependency-free (no '@relaydeck/ui', no 'lit', no DOM): the
// send-picker dedup logic is unit-tested by loading THIS module in bare node
// (tests/test_telegram_plugin.py::test_panel_unique_chats_preserves_topics).
// panel.js (a light-DOM Lit module that can't load outside the browser)
// imports and delegates to it, so the logic stays testable after the Lit
// migration.

/** Build the send-picker's chat rows, keeping a SEPARATE row per forum topic
 *  (connection + chat_id + thread_id) instead of collapsing by chat alone.
 *  Prefers the conversation registry (carries connection + title); falls back
 *  to chat-bearing routes for chats not seen inbound yet. */
export function uniqueChats(convs, routes) {
  const out = new Map();
  const add = ({ connection, chat_id, thread_id = null, label }) => {
    const key = `${connection || ''}|${chat_id}|${thread_id ?? ''}`;
    if (out.has(key)) return;
    out.set(key, { connection: connection || '', chat_id, thread_id, label });
  };
  for (const c of (convs || [])) {
    const base = c.title || c.last_user || c.chat_id;
    add({
      connection: c.connection_id,
      chat_id: c.chat_id,
      label: `${base} [${c.connection_id}:${c.chat_id}]`,
    });
    for (const tid of (c.thread_ids || [])) {
      add({
        connection: c.connection_id,
        chat_id: c.chat_id,
        thread_id: tid,
        label: `${base}#${tid} [${c.connection_id}:${c.chat_id}]`,
      });
    }
  }
  for (const r of ((routes && routes.routes) || [])) {
    if (r.chat_id == null) continue;
    const conn = r.connection || '';
    const topic = r.thread_id != null ? `#${r.thread_id}` : '';
    const target = r.agent ? `→ ${r.agent}` : `→ @${r.workspace}`;
    add({
      connection: conn, chat_id: r.chat_id, thread_id: r.thread_id,
      label: `${r.chat_id}${topic} (${target})${conn ? ' [' + conn + ']' : ''}`,
    });
  }
  return [...out.values()];
}

/** Resolve the chat_id a route should be saved with, from the route-form draft
 *  and the catalog row currently selected in the chat picker (null for the
 *  any/custom/empty cases). When a real chat is picked the picked row is
 *  AUTHORITATIVE — the draft's manual chat_id field is hidden in that case, so
 *  reading it would yield '' and silently save a chat_id=null wildcard.
 *  Returns { chat_id } normally, or { chat_id: null, missing: true } when a
 *  manual id was required but absent (caller should reject the submit). */
export function resolveRouteChatId(draft, pickedRow) {
  const pick = draft.chatPick;
  if (pick === '__any__') return { chat_id: null };
  if (pick === '__custom__' || !pick) {
    const v = parseInt(draft.chatId, 10);
    return Number.isFinite(v) ? { chat_id: v } : { chat_id: null, missing: true };
  }
  // A real chat is picked — prefer the picked catalog row's id over the (hidden,
  // and therefore usually empty) manual chat_id field.
  if (pickedRow && pickedRow.chat_id != null) return { chat_id: pickedRow.chat_id };
  // Real pick but its row vanished AND no manual id — reject rather than
  // silently saving a chat_id=null wildcard (parity with the __custom__ case).
  const v = parseInt(draft.chatId, 10);
  return Number.isFinite(v) ? { chat_id: v } : { chat_id: null, missing: true };
}
