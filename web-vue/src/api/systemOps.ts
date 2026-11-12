import apiClient, { getAuthToken, handleUnauthorizedResponse } from './client'

export type SchedulerMode = 'auto' | 'manual'
export type HardwareTier = 'auto' | 'low' | 'medium' | 'high' | 'flagship'

export interface SchedulerWatermarks {
  cpu_high: number
  mem_high: number
  cpu_low: number
  mem_low: number
  restore_hold_seconds: number
  sample_interval_seconds: number
}

export interface SchedulerHardware {
  cpu_cores: number
  cpu_model: string
  memory_total_bytes: number
  memory_total_gb: number
  disk_total_bytes: number
  disk_free_bytes: number
  platform: string
  tier: string
  tier_label: string
  detected_tier: string
  hardware_override: HardwareTier
  effective_tier: string
  effective_tier_label: string
  recommended: Record<string, number>
}

export interface SchedulerLoad {
  cpu_percent: number | null
  memory_percent: number | null
  memory_available_bytes: number | null
  load_average: number | null
}

export interface SchedulerHistoryPoint {
  at: number
  cpu: number | null
  memory: number | null
  scale: number
  level: string
}

export interface SchedulerEvent {
  at: number
  event: string
  reason: string
  scale: number
}

export interface SchedulerSnapshot {
  enabled: boolean
  mode: SchedulerMode
  level: 'normal' | 'warning' | 'critical'
  scale: number
  scale_percent: number
  min_scale_percent: number
  watermarks: SchedulerWatermarks
  hardware: SchedulerHardware
  load: SchedulerLoad
  base: Record<string, number>
  effective: Record<string, number>
  overload_since: number | null
  healthy_since: number | null
  restore_in_seconds: number | null
  last_adjust_at: number | null
  last_evaluated_at: number | null
  adjustments: number
  history: SchedulerHistoryPoint[]
  events: SchedulerEvent[]
}

export interface SchedulerPolicy extends SchedulerWatermarks {
  enabled: boolean
  mode: SchedulerMode
  min_scale_percent: number
  hardware_override: HardwareTier
}

export interface DatabaseMaintenanceStatus {
  backend: string
  database: string
  busy: boolean
  size_bytes: number
  wal_size_bytes: number
  page_count: number
  free_pages: number
  fragmentation_percent: number | null
  last_run: DatabaseMaintenanceResult | null
  thresholds: { fragmentation_percent: number }
}

export interface DatabaseMaintenanceStep {
  operation: string
  statement?: string
  ok: boolean
  duration_ms: number
  detail?: string
}

export interface DatabaseMaintenanceResult {
  ok: boolean
  operations: string[]
  backend: string
  duration_ms: number
  integrity: { ok: boolean; detail: string } | null
  steps: DatabaseMaintenanceStep[]
  errors: string[]
  before: Record<string, number | null>
  after: Record<string, number | null>
  freed_bytes: number
  fragmentation_percent: number | null
}

export interface AccountJobView {
  job_id: string
  kind: string
  lane: string
  label: string
  state: string
  total: number
  processed: number
  failed: number
  skipped: number
  remaining: number
  percent: number
  elapsed_seconds: number
  rate_per_second: number
  eta_seconds: number | null
  errors: Array<{ token_hint: string; error: string }>
}

export interface AccountQueueView {
  stats: {
    workers: number
    worker_target: number
    active_jobs: number
    queued_tokens: number
    processed_total: number
    failed_total: number
    lanes: Record<string, number>
  }
  jobs: AccountJobView[]
}

export interface ProxyHealthNode {
  key: string
  label: string
  state: string
  consecutive_failures: number
  total_failures: number
  total_successes: number
  latency_ms: number | null
  last_error: string
  cooldown_remaining: number
}

export interface ProxyHealthView {
  health: {
    nodes: ProxyHealthNode[]
    total: number
    counts: Record<string, number>
    down_keys: string[]
  }
  fallback_reference: string
}

export interface ChangelogItem {
  type: string
  text: string
}

export interface ChangelogSection {
  title: string
  items: ChangelogItem[]
}

export interface ChangelogRelease {
  version: string
  date: string
  time?: string
  sections: ChangelogSection[]
  archived: boolean
}

export interface ChangelogView {
  current_version: string
  source: string
  available: boolean
  releases: ChangelogRelease[]
  archive: ChangelogRelease[]
  totals: { releases: number; archive: number }
}

export interface LegacySyncProbe {
  path: string
  exists: boolean
  accounts: number
  fresh_accounts: number
  settings_keys: number
  account_groups: number
  call_records: number
  pending_call_records: number
  error: string
}

export interface LegacySyncConfig {
  enabled: boolean
  paths: string[]
  interval_seconds: number
  sync_accounts: boolean
  sync_settings: 'off' | 'fill' | 'overwrite'
  sync_account_groups: boolean
  sync_call_records: boolean
  max_call_records_per_run: number
  auto_check: boolean
  check_per_run: number
}

export interface LegacySyncResult {
  started_at?: string
  finished_at?: string
  duration_seconds?: number
  trigger?: string
  added_accounts?: number
  skipped_accounts?: number
  updated_settings?: string[]
  synced_groups?: number
  imported_call_records?: number
  queued_checks?: number
  errors?: string[]
}

export interface LegacySyncState {
  config: LegacySyncConfig
  last_result: LegacySyncResult
  probes: LegacySyncProbe[]
  running: boolean
  next_run_in_seconds: number
  result?: LegacySyncResult
}

export interface PoolAlertConfig {
  enabled: boolean
  webhook_url: string
  min_usable: number
  min_refresh_ratio_percent: number
  check_interval_seconds: number
  cooldown_seconds: number
}

export interface PoolAlertReport {
  checked_at?: string
  metrics?: {
    total: number
    usable: number
    recoverable: number
    unavailable: number
    refresh_valid: number
    refresh_ratio_percent: number
  }
  warnings?: string[]
  notified?: boolean
  cooldown_active?: boolean
}

export interface PoolAlertStatus {
  config: PoolAlertConfig
  last_report: PoolAlertReport
  last_ok_at: string
  last_alert_at: string
  running: boolean
  report?: PoolAlertReport
}

export interface GovernanceConfig {
  enabled: boolean
  mode: 'off' | 'report' | 'disable' | 'purge'
  run_every_hours: number
  account_log_retention_hours: number
  max_per_run: number
}

export interface GovernanceReport {
  started_at?: string
  finished_at?: string
  mode?: string
  effective_mode?: string
  dry_run?: boolean
  dead_accounts?: number
  disabled?: number
  removed?: number
  would_touch?: number
  account_logs_removed?: number
  account_logs_pending?: number
  warnings?: string[]
  duration_seconds?: number
}

export interface GovernanceStatus {
  config: GovernanceConfig
  last_report: GovernanceReport
  running: boolean
  report?: GovernanceReport
}

export interface LocalBackupConfig {
  enabled: boolean
  interval_hours: number
  keep: number
}

export interface LocalBackupFile {
  name: string
  size_bytes: number
  created_at: string
}

export interface LocalBackupStatus {
  config: LocalBackupConfig
  last_report: {
    file?: string
    size_bytes?: number
    pruned?: number
    kept?: number
    skipped?: string
    error?: string
    finished_at?: string
  }
  backups: LocalBackupFile[]
  running: boolean
  report?: LocalBackupStatus['last_report']
}

export interface AuditEntry {
  at: string
  actor: string
  action: string
  target: string
  detail?: Record<string, unknown>
}

export interface UpstreamDiagnostics {
  checks: {
    dns: Array<{ host: string; ok: boolean; addresses?: string[]; ipv4?: string[]; ipv6?: string[]; error?: string }>
    direct: Array<{ host: string; ok: boolean; tcp_ms?: number; tls_ms?: number; error?: string }>
    proxy: Record<string, unknown>
  }
  advice: string[]
}

export interface DebugLogConfig {
  enabled: boolean
  mode: 'time' | 'size'
  retention_days: number
  max_size_mb: number
  slack_mb: number
  level: 'debug' | 'info'
}

export interface DebugLogFile {
  name: string
  size_bytes: number
  modified_at: string
}

export interface DebugLogStatus {
  config: DebugLogConfig
  active: boolean
  files: DebugLogFile[]
  file_count: number
  total_bytes: number
  budget_bytes: number
  slack_bytes: number
  directory: string
}

export interface LegacyImportSummary {
  import_id: string
  filename: string
  size_bytes: number
  uploaded_at: string
  accounts: number
  settings_keys: number
  account_groups: number
  call_records: number
}

export interface LegacyImportAccount {
  account_id: string
  email: string
  status: string
  plan_type: string
  type: string
  quota: number | null
  token_hint: string
}

export interface LegacyImportResult {
  import_id: string
  dry_run: boolean
  accounts_added: number
  accounts_skipped: number
  settings_updated: string[]
  groups_added: number
  call_records_imported: number
}

export interface LegacyImportOptions {
  accounts: { enabled: boolean; mode: 'all' | 'selected'; account_ids: string[]; emails: string[] }
  settings: { enabled: boolean; mode: 'fill' | 'overwrite'; keys: string[] }
  account_groups: boolean
  call_records: boolean
  max_call_records: number
}

export interface LegacyUploadProgress {
  /** 已发出的字节数 */
  loaded: number
  /** 请求体总字节数（浏览器拿不到时回退为文件大小） */
  total: number
  /** 0-100；总量未知时为 -1 */
  percent: number
  /** 从开始上传统计的平均速度，字节/秒 */
  bytes_per_second: number
}

export interface LegacyUploadOptions {
  onProgress?: (progress: LegacyUploadProgress) => void
  /** 传入 AbortSignal 即可取消，内部会调用 xhr.abort() */
  signal?: AbortSignal
}

/**
 * 上传相关的失败原因。旧代码把状态码和响应体都压成一句 message，
 * 结果 413（Nginx 拦的 HTML 错误页）在界面上看不出任何线索。
 */
export class LegacyUploadError extends Error {
  readonly status: number
  readonly response_text: string
  readonly aborted: boolean
  readonly timed_out: boolean

  constructor(
    message: string,
    options: { status?: number; responseText?: string; aborted?: boolean; timedOut?: boolean } = {},
  ) {
    super(message)
    this.name = 'LegacyUploadError'
    this.status = options.status ?? 0
    this.response_text = options.responseText ?? ''
    this.aborted = options.aborted ?? false
    this.timed_out = options.timedOut ?? false
  }
}

/** 从 JSON / 纯文本 / Nginx HTML 错误页里抠出可读的错误信息。 */
function readUploadErrorMessage(responseText: string): string {
  const text = (responseText || '').trim()
  if (!text) return ''
  try {
    const data = JSON.parse(text) as Record<string, unknown>
    const detail = data.detail
    if (typeof detail === 'string') return detail
    if (detail && typeof detail === 'object') {
      const nested = detail as Record<string, unknown>
      if (typeof nested.error === 'string') return nested.error
      if (typeof nested.message === 'string') return nested.message
    }
    if (typeof data.error === 'string') return data.error
    if (typeof data.message === 'string') return data.message
  } catch {
    // 不是 JSON：通常是反代直接返回的 HTML 错误页，原样交给调用方截断展示。
  }
  return text
}

const LEGACY_UPLOAD_ENDPOINT = '/api/system/legacy-sync/upload'

export const systemOpsApi = {
  currentVersion: () => apiClient.get<never, { version: string }>('/version'),

  changelog: (limit = 10) =>
    apiClient.get<never, ChangelogView>('/api/system/changelog', { params: { limit } }),

  legacySync: () => apiClient.get<never, LegacySyncState>('/api/system/legacy-sync'),

  saveLegacySync: (payload: Partial<LegacySyncConfig>) =>
    apiClient.post<never, LegacySyncState>('/api/system/legacy-sync', payload),

  runLegacySync: (dryRun = false) =>
    apiClient.post<never, LegacySyncState>('/api/system/legacy-sync/run', { dry_run: dryRun }),

  poolAlerts: () => apiClient.get<never, PoolAlertStatus>('/api/system/pool-alerts'),

  savePoolAlerts: (payload: Partial<PoolAlertConfig>) =>
    apiClient.post<never, PoolAlertStatus>('/api/system/pool-alerts', payload),

  runPoolAlerts: (dryRun = false) =>
    apiClient.post<never, PoolAlertStatus>('/api/system/pool-alerts/run', { dry_run: dryRun }),

  governance: () => apiClient.get<never, GovernanceStatus>('/api/system/account-governance'),

  saveGovernance: (payload: Partial<GovernanceConfig>) =>
    apiClient.post<never, GovernanceStatus>('/api/system/account-governance', payload),

  runGovernance: (dryRun = true, mode?: GovernanceConfig['mode']) =>
    apiClient.post<never, GovernanceStatus>('/api/system/account-governance/run', {
      dry_run: dryRun,
      mode: mode || null,
    }),

  localBackup: () => apiClient.get<never, LocalBackupStatus>('/api/system/local-backup'),

  saveLocalBackup: (payload: Partial<LocalBackupConfig>) =>
    apiClient.post<never, LocalBackupStatus>('/api/system/local-backup', payload),

  runLocalBackup: () => apiClient.post<never, LocalBackupStatus>('/api/system/local-backup/run', {}),

  audit: (limit = 50) =>
    apiClient.get<never, AuditEntry[]>('/api/system/audit', { params: { limit } }),

  upstreamDiagnostics: () =>
    apiClient.get<never, UpstreamDiagnostics>('/api/system/upstream-diagnostics'),

  debugLogs: () => apiClient.get<never, DebugLogStatus>('/api/system/debug-logs'),

  saveDebugLogs: (payload: Partial<DebugLogConfig>) =>
    apiClient.post<never, DebugLogStatus>('/api/system/debug-logs', payload),

  clearDebugLogs: () =>
    apiClient.post<{ confirm: boolean }, DebugLogStatus>('/api/system/debug-logs/clear', { confirm: true }),

  async exportDebugLogs(): Promise<{ blob: Blob; filename: string }> {
    const token = getAuthToken()
    const response = await fetch('/api/system/debug-logs/export', {
      method: 'POST',
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    const blob = await response.blob()
    const disposition = response.headers.get('Content-Disposition') || ''
    const match = /filename="?([^";]+)"?/i.exec(disposition)
    return { blob, filename: match?.[1] || 'qingmeng-debug.zip' }
  },

  scheduler: () => apiClient.get<never, SchedulerSnapshot>('/api/system/scheduler'),

  setSchedulerMode: (mode: SchedulerMode, enabled?: boolean) =>
    apiClient.post<{ mode: SchedulerMode; enabled?: boolean }, SchedulerSnapshot>(
      '/api/system/scheduler/mode',
      enabled === undefined ? { mode } : { mode, enabled },
    ),

  saveSchedulerPolicy: (policy: SchedulerPolicy) =>
    apiClient.post<SchedulerPolicy, SchedulerSnapshot>('/api/system/scheduler/policy', policy),

  database: () => apiClient.get<never, DatabaseMaintenanceStatus>('/api/system/database'),

  runDatabaseMaintenance: (operations: string[]) =>
    apiClient.post<{ operations: string[] }, DatabaseMaintenanceResult>(
      '/api/system/database/maintenance',
      { operations },
    ),

  queue: () => apiClient.get<never, AccountQueueView>('/api/system/queue'),

  cancelJob: (jobId: string) =>
    apiClient.post<never, { cancelled: boolean; job_id: string }>(
      `/api/system/queue/${encodeURIComponent(jobId)}/cancel`,
    ),

  proxyHealth: () => apiClient.get<never, ProxyHealthView>('/api/system/proxy-health'),

  probeProxyHealth: () =>
    apiClient.post<never, { probed: number; results: Array<Record<string, unknown>> }>(
      '/api/system/proxy-health/probe',
    ),

  /**
   * 上传旧库数据库文件。
   *
   * 故意用 XMLHttpRequest 而不是 fetch：只有 xhr.upload.onprogress 能在请求体
   * 发送过程中给出真实进度（fetch 需要 duplex + 自己包 ReadableStream，浏览器
   * 兼容性差且拿不到「已发出多少」）。200–300MB 的文件必须让用户看到百分比、
   * 速度和剩余时间，并且能中途取消。
   */
  uploadLegacyDatabase(file: File, options: LegacyUploadOptions = {}): Promise<LegacyImportSummary> {
    return new Promise<LegacyImportSummary>((resolve, reject) => {
      const xhr = new XMLHttpRequest()
      const startedAt = Date.now()
      let settled = false

      function abortUpload() {
        xhr.abort()
      }

      const cleanup = () => {
        options.signal?.removeEventListener('abort', abortUpload)
      }

      const succeed = (value: LegacyImportSummary) => {
        if (settled) return
        settled = true
        cleanup()
        resolve(value)
      }

      const fail = (error: LegacyUploadError) => {
        if (settled) return
        settled = true
        cleanup()
        reject(error)
      }

      xhr.upload.onprogress = (event: ProgressEvent) => {
        if (!options.onProgress) return
        // 反代/浏览器偶尔给不出 total，用文件大小兜底，保证界面上的分母稳定。
        const total = event.lengthComputable && event.total > 0 ? event.total : file.size
        const loaded = total > 0 ? Math.min(event.loaded, total) : event.loaded
        const elapsedSeconds = Math.max((Date.now() - startedAt) / 1000, 0.001)
        const percent = total > 0 ? (loaded / total) * 100 : -1
        options.onProgress({
          loaded,
          total,
          percent: percent < 0 ? -1 : Math.min(100, Math.round(percent * 10) / 10),
          bytes_per_second: Math.max(0, Math.round(loaded / elapsedSeconds)),
        })
      }

      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            succeed(JSON.parse(xhr.responseText) as LegacyImportSummary)
          } catch {
            fail(
              new LegacyUploadError('服务端返回了无法解析的响应', {
                status: xhr.status,
                responseText: xhr.responseText,
              }),
            )
          }
          return
        }
        if (xhr.status === 401) handleUnauthorizedResponse()
        const detail = readUploadErrorMessage(xhr.responseText)
        fail(
          new LegacyUploadError(detail || `上传失败（HTTP ${xhr.status}）`, {
            status: xhr.status,
            responseText: xhr.responseText,
          }),
        )
      }
      xhr.onerror = () => fail(new LegacyUploadError('网络中断，上传失败（连接被重置或代理断开）', { status: 0 }))
      xhr.ontimeout = () => fail(new LegacyUploadError('上传超时', { status: 0, timedOut: true }))
      xhr.onabort = () => fail(new LegacyUploadError('上传已取消', { aborted: true }))

      xhr.open('POST', LEGACY_UPLOAD_ENDPOINT)
      const token = getAuthToken()
      if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)
      // 不手动设 Content-Type：让浏览器自己带 multipart boundary。

      if (options.signal) {
        if (options.signal.aborted) {
          fail(new LegacyUploadError('上传已取消', { aborted: true }))
          return
        }
        options.signal.addEventListener('abort', abortUpload, { once: true })
      }

      const form = new FormData()
      form.append('file', file)
      xhr.send(form)
    })
  },

  listLegacyImports: () =>
    apiClient.get<never, { items: LegacyImportSummary[] }>('/api/system/legacy-sync/imports'),

  deleteLegacyImport: (importId: string) =>
    apiClient.delete<never, { deleted: string }>(
      `/api/system/legacy-sync/imports/${encodeURIComponent(importId)}`,
    ),

  deleteAllLegacyImports: () =>
    apiClient.delete<never, { removed: number }>('/api/system/legacy-sync/imports'),

  previewLegacyAccounts: (importId: string, params: { page?: number; page_size?: number; keyword?: string }) => {
    const query = new URLSearchParams()
    query.set('page', String(params.page ?? 1))
    query.set('page_size', String(params.page_size ?? 20))
    if (params.keyword) query.set('keyword', params.keyword)
    return apiClient.get<never, {
      items: LegacyImportAccount[]
      total: number
      page: number
      page_size: number
      import_id: string
    }>(`/api/system/legacy-sync/imports/${encodeURIComponent(importId)}/accounts?${query.toString()}`)
  },

  importLegacySelection: (
    importId: string,
    options: LegacyImportOptions,
    dryRun: boolean,
    confirm = true,
  ) =>
    apiClient.post<unknown, { result: LegacyImportResult }>(
      `/api/system/legacy-sync/imports/${encodeURIComponent(importId)}/import`,
      { options, dry_run: dryRun, confirm },
    ),
}
