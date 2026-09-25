export type PipelineState = "entwurf" | "laeuft" | "pausiert" | "erledigt" | "abgebrochen";

export type Attention =
  | "vielleicht" | "kann_nicht_pruefen" | "schritt_faellig" | "uebergabe"
  | "selbst_geantwortet" | "versand_unklar" | "person_aus";

export interface MailPayload {
  to?: string[];
  subject?: string;
  body?: string;
}

export interface Step {
  id: number;
  position: number;
  action: "mail_senden" | "uebergabe";
  after_days: number;
  payload: MailPayload;
  approved: boolean;
  approved_at: string | null;
  status: "offen" | "erledigt";
  done_at: string | null;
  due_at: string | null;
}

export interface Candidate {
  source: "mail";
  id: number;
  from: string;
  from_name: string | null;
  subject: string | null;
  snippet: string | null;
  date: string | null;
  folder: string | null;
  category: string | null;
  why: string[];
  strong: boolean;
}

export interface Features {
  addresses: string[];
  domains: string[];
  names: string[];
  numbers: string[];
  words?: string[];
  dismissed_mail_ids?: number[];
}

export interface Config {
  send_days: "alle" | "werktags";
  send_from_hour: number;
  send_to_hour: number;
}

export interface PipelineEvent {
  id: number;
  at: string;
  kind: "pruefung" | "aktion" | "mensch" | "status";
  text: string;
  data: any;
}

export interface Pipeline {
  id: number;
  kind: string;
  title: string;
  goal: string | null;
  state: PipelineState;
  mode: string;
  attention: Attention | null;
  attention_detail: any;
  origin: {
    mail_id?: number;
    subject?: string;
    to?: string[];
    sent_at?: string;
    account_email?: string;
    body_excerpt?: string;
  };
  features: Features;
  config: Config;
  result: any;
  since_at: string;
  next_run_at: string | null;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
  steps: Step[];
  next_step: { id: number; action: string; due_at: string | null; position: number } | null;
  events?: PipelineEvent[];
  last_check?: any;
  quote?: string;
}

export interface SentMail {
  id: number;
  subject: string | null;
  to: string[];
  date: string | null;
  snippet: string | null;
}

export interface PersonSwitch {
  id: string;
  name: string;
  role: string;
  enabled: boolean;
  changed_at: string | null;
  changed_by: string | null;
  can_change: boolean;
}
