/** Backend shapes from backend/whatsapp.py routes. */

export interface WaStatus {
  connected: boolean;
  me?: { id: string; name?: string | null } | null;
  hasQr?: boolean;
  bridge_unreachable?: boolean;
  detail?: string;
  cachedMessages?: number;
}

export interface WaChat {
  jid: string;
  name?: string | null;
  is_group: number;
  last_message_ts?: number | null;
  last_message_text?: string | null;
  unread_count: number;
}

export interface WaMessage {
  msg_id: string;
  chat_jid: string;
  from_me: number;        // 0/1
  push_name?: string | null;
  timestamp: number;
  text?: string | null;
  media_kind?: string | null;
  mimetype?: string | null;
  filename?: string | null;
  transcript?: string | null;
  media_paperless_id?: number | null;
  media_immich_id?: string | null;
}

export interface WaDraftVariant {
  id: number;
  label: string;
  text: string;
}

export interface WaPendingDrafts {
  group_id: string | null;
  variants: WaDraftVariant[];
  sources: Array<{ kind?: string; snippet?: string; ref?: string }>;
}
