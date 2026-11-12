import type { Account, AccountGroup, AccountLane } from '@/api/accounts'

export type AccountStatusFilter = 'all' | 'normal' | 'limited' | 'abnormal' | 'disabled'
export type AccountStatusTone = 'neutral' | 'success' | 'warning' | 'error'

export type AccountGroupRow = AccountGroup & {
  raw: AccountGroup
  account_count: number
}

function cleanString(value: unknown): string {
  return String(value || '').trim()
}

function signatureValue(value: unknown): string {
  return cleanString(value).replaceAll('|', '/')
}

export function boundedSignatureText(value: unknown, limit = 160): string {
  const text = signatureValue(value)
  if (text.length <= limit) return text
  return `${text.length}:${text.slice(0, limit)}:${text.slice(-24)}`
}

export function accountRowSignature(item: Account): string {
  return [
    item.id,
    item.source_plan_label,
    item.status_category,
    item.status_label,
    item.status_tone,
    item.memory_state,
    item.memory_label,
    item.memory_tone,
    item.memory_error,
    item.status_reason,
    boundedSignatureText(item.status_raw_error),
    item.display_name,
    item.email,
    item.user_id,
    item.created_at,
    item.quota_state,
    item.quota_label,
    item.quota_reset_at,
    item.group_id,
    item.proxy_label,
    item.success_count,
    item.failure_count,
    item.image_inflight,
    item.enabled ? 1 : 0,
    item.available ? 1 : 0,
    item.enabled_action_label,
    item.access_token_status,
    item.access_token_label,
    item.access_token_tone,
    item.access_token_issued_at,
    item.access_token_expires_at,
    item.refresh_token_status,
    item.refresh_token_label,
    item.refresh_token_tone,
    item.credential_availability,
    item.credential_availability_label,
    item.can_refresh_access_token ? 1 : 0,
    item.refresh_token_invalid_at,
    item.last_token_refresh_at,
    item.last_token_refresh_error,
    item.last_token_refresh_error_at,
  ].map(signatureValue).join('|')
}

/**
 * 快照刷新时按行签名复用旧对象：内容未变的行保持同一引用，
 * 行组件的 props 不变就不会产生 patch；返回 `changed=false` 表示整页无变化。
 */
export function reuseAccountRows(
  previous: Account[],
  incoming: readonly Account[],
): { rows: Account[]; changed: boolean } {
  const previousById = new Map(previous.map((item) => [item.id, item]))
  let changed = incoming.length !== previous.length
  const next = incoming.map((item, index) => {
    const prior = previousById.get(item.id)
    if (!prior) {
      changed = true
      return item
    }
    if (prior === item) return item
    if (accountRowSignature(prior) === accountRowSignature(item)) {
      if (previous[index] !== prior) changed = true
      return prior
    }
    changed = true
    return item
  })
  return { rows: changed ? next : previous, changed }
}

function formatDateTime(timestampSeconds: number): string {
  const date = new Date(timestampSeconds * 1000)
  if (Number.isNaN(date.getTime())) return '-'
  const yyyy = date.getFullYear()
  const mm = String(date.getMonth() + 1).padStart(2, '0')
  const dd = String(date.getDate()).padStart(2, '0')
  const hh = String(date.getHours()).padStart(2, '0')
  const mi = String(date.getMinutes()).padStart(2, '0')
  return `${yyyy}-${mm}-${dd} ${hh}:${mi}`
}

export function formatAccountDate(timestampSeconds?: number | null): string {
  const value = Number(timestampSeconds || 0)
  if (!Number.isFinite(value) || value <= 0) return '-'
  return formatDateTime(value)
}

export function statusText(item: Account): string {
  return item.memory_label ? `${item.status_label} / ${item.memory_label}` : item.status_label
}

export function statusTone(item: Account) {
  if (item.memory_state === 'failed') return 'danger' as const
  return item.status_tone
}

export function statusRawError(item: Account): string {
  return item.status_raw_error
}

export function accountSurfaceClass(
  item: Account,
  selected: boolean,
  surface: 'row' | 'card',
): string {
  if (selected) {
    return surface === 'card'
      ? 'border-primary/45 bg-primary/[0.02]'
      : 'bg-primary/5'
  }

  let statusBackground = ''
  if (item.status_tone === 'neutral') statusBackground = 'bg-muted/50'
  if (item.status_tone === 'warning') statusBackground = 'bg-amber-500/5'
  if (item.status_tone === 'error') statusBackground = 'bg-rose-500/5'

  if (surface === 'card') {
    return `${statusBackground} hover:border-primary/30`.trim()
  }
  if (item.status_tone === 'neutral') return 'bg-muted/50 hover:bg-muted/70'
  if (item.status_tone === 'warning') return 'bg-amber-500/5 hover:bg-amber-500/[0.08]'
  if (item.status_tone === 'error') return 'bg-rose-500/5 hover:bg-rose-500/[0.08]'
  return 'hover:bg-muted/30'
}

export function accountPrimaryText(item: Account): string {
  return item.display_name
}

export function accountSecondaryText(item: Account): string {
  return item.email && item.user_id ? item.user_id : item.id
}

export function accountSourceText(item: Account): string {
  return item.source_plan_label
}

export function accountProxyText(item: Account): string {
  return item.proxy_label
}

export function accountQuotaText(item: Account): string {
  return item.quota_label
}

export function accountCreatedText(item: Account): string {
  return formatAccountDate(item.created_at)
}

export function accountRestoreText(item: Account): string {
  return formatAccountDate(item.quota_reset_at)
}

export function accountStatusDetailText(
  item: Account,
  groupLabel: (groupId: string | undefined) => string,
  proxyText: (account: Account) => string = accountProxyText,
): string {
  return [
    item.status_reason,
    `账号组：${groupLabel(item.group_id)}`,
    `代理：${proxyText(item)}`,
  ].filter(Boolean).join('\n')
}

export function accountDetailItems(item: Account) {
  return [
    { label: '创建时间', value: accountCreatedText(item) },
    { label: '恢复时间', value: accountRestoreText(item) },
    { label: '图片额度', value: accountQuotaText(item) },
    { label: '成功 / 失败', value: `${item.success_count || 0} / ${item.failure_count || 0}` },
  ]
}

export function accountGroupNameMap(groups: readonly AccountGroup[]): Map<string, string> {
  return new Map(groups.map((group) => [group.id, group.name || group.id]))
}

export function accountGroupLabel(groupId: string | undefined, groupNames: ReadonlyMap<string, string>): string {
  const id = cleanString(groupId)
  if (!id) return '未分组'
  return groupNames.get(id) || id
}

export function buildAccountGroupRows(groups: readonly AccountGroup[]): AccountGroupRow[] {
  return groups.map((group) => ({
    ...group,
    raw: group,
    name: group.name || group.id,
    account_count: Number(group.account_count || 0),
  }))
}

const laneOrder: AccountLane[] = ['fast', 'thinking', 'pro']

export function laneEnabled(lanes: AccountLane[], lane: AccountLane): boolean {
  return lanes.includes(lane)
}

function laneCount(lanes: AccountLane[]): number {
  return laneOrder.filter((lane) => lanes.includes(lane)).length
}

export function laneSummaryTone(lanes: AccountLane[]): AccountStatusTone {
  const enabledCount = laneCount(lanes)
  if (enabledCount === laneOrder.length) return 'success'
  if (enabledCount === 0) return 'neutral'
  return 'warning'
}

export function laneSummaryText(lanes: AccountLane[]): string {
  return `${laneCount(lanes)}/${laneOrder.length}`
}

export function laneLineClass(lane: AccountLane, lanes: AccountLane[]): string {
  if (!laneEnabled(lanes, lane)) return 'text-muted-foreground'
  if (lane === 'fast') return 'bg-emerald-500/10 text-emerald-700'
  if (lane === 'thinking') return 'bg-cyan-500/10 text-cyan-700'
  return 'bg-blue-500/10 text-blue-700'
}
