export interface Module {
  id: string;
  module_name: string;
  repo: string;
  module_path: string;
  version?: string;
  description?: string;
  tags?: string[];
  resources?: string[];
  variables?: Record<string, { type?: string; required?: boolean }>;
  outputs?: Record<string, { description?: string }>;
  indexed_at?: string;
  commit_sha?: string;
  license?: string;
}

export interface Source {
  module_name: string;
  repo: string;
  module_path: string;
  version?: string;
  similarity: number;
  tags?: string[];
  description?: string;
}

export interface QueryResponse {
  answer: string;
  sources: Source[];
  latency_ms: number;
}

export interface Job {
  id: string;
  repo: string;
  repo_url?: string;
  branch?: string;
  commit_sha?: string;
  status: 'pending' | 'running' | 'done' | 'failed';
  triggered_by?: string;
  stats?: { added?: number; updated?: number; failed?: number; total?: number; modules?: number; versions?: number };
  started_at?: string;
  finished_at?: string;
  error?: string;
}

export interface PaginatedJobs {
  total: number;
  limit: number;
  offset: number;
  items: Job[];
}

export interface Stats {
  total_modules?: number;
  total_repos?: number;
  unique_tags?: number;
  unique_resource_types?: number;
  total_versions?: number;
  total_conventions?: number;
  total_usages?: number;
  last_indexed?: string;
  top_tags?: { tag: string; count: number }[];
  top_resources?: { resource: string; count: number }[];
}

export type QueryType = 'compose' | 'optimize' | 'audit' | 'search';

export interface ModuleRefSummary {
  module_ref: string;
  usage_count: number;
  convention_count: number;
  kinds: string[];
}

export interface Snippet {
  id: string;
  kind: string;
  summary: string;
  evidence_count: number;
  source_locator?: string;
  related_refs?: string[];
  scope?: string;
  consumer_repo?: string;
  updated_at?: string;
}

export interface SnippetModuleDetail {
  module_ref: string;
  conventions: Record<string, Snippet>;
  usages: Snippet[];
}

export interface AuditLog {
  id: string;
  created_at: string;
  category: string;          // api, mcp, worker, llm
  action: string;
  status: string;            // success, error
  duration_ms?: number;
  request_data?: unknown;
  response_data?: unknown;
  error?: string;
  metadata?: Record<string, unknown>;
}

export interface AuditLogsResponse {
  total: number;
  limit: number;
  offset: number;
  items: AuditLog[];
}

export interface AuthInfo {
  auth_mode: 'disabled' | 'local' | 'sso';
}

export interface UserInfo {
  id: string;
  email: string;
  role: string;
  display_name?: string;
  auth_method: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
}

export interface ConsumerJob {
  id: string;
  repo: string;
  repo_url?: string;
  branch?: string;
  commit_sha?: string;
  status: 'pending' | 'running' | 'done' | 'failed';
  triggered_by?: string;
  stats?: {
    parsed?: number;
    resolved?: number;
    embedded?: number;
    affected_modules?: string[];
    distillation?: { modules?: number; dimensions?: number; skipped?: number; stale_marked?: number; kept_existing?: number; llm_failed?: number };
  };
  started_at?: string;
  finished_at?: string;
  error?: string;
}

export interface PaginatedConsumerJobs {
  total: number;
  limit: number;
  offset: number;
  items: ConsumerJob[];
}
