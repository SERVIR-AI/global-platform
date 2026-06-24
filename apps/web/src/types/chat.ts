export type ChatProvider = 'claude' | 'gemini' | 'openai';

export type ChatRole = 'system' | 'user' | 'assistant';

export interface ChatMessage {
  role: ChatRole;
  content: string;
}

export interface Usage {
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
}

export interface ChatRequest {
  /** Conversation so far; at least one message is required. */
  messages: ChatMessage[];
  /** LLM provider override; defaults to the server's DEFAULT_PROVIDER. */
  provider?: ChatProvider | null;
  /** Model override; defaults to the provider's configured model. */
  model?: string | null;
  /** Stable id to continue a prior conversation; history is kept server-side by this id. */
  thread_id?: string | null;
  /** When true, the response includes `trace` — a step-by-step narration of the run. */
  verbose?: boolean;
}

export interface ChatResponse {
  id: string;
  thread_id: string;
  message: ChatMessage;
  provider: ChatProvider;
  model: string;
  usage?: Usage | null;
  /** Step-by-step narration; present only when the request set verbose=true. */
  trace?: string[] | null;
  created_at?: string;
}

export interface HealthResponse {
  status: string;
  app: string;
  default_provider: string;
  [key: string]: unknown;
}

export interface ValidationError {
  loc: (string | number)[];
  msg: string;
  type: string;
  input?: unknown;
  ctx?: Record<string, unknown>;
}

export interface HTTPValidationError {
  detail?: ValidationError[];
}
