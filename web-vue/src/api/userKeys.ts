import apiClient from './client'

export interface UserKey {
  id: string
  name: string
  role: 'user' | 'admin'
  enabled: boolean
  created_at?: string | null
  last_used_at?: string | null
  image_daily_limit: number
  image_monthly_limit: number
  image_daily_used: number
  image_monthly_used: number
  image_quota_day: string
  image_quota_month: string
  expires_at?: string | null
}

export interface UserKeyCreatePayload {
  name: string
  image_daily_limit: number
  image_monthly_limit: number
  expires_at?: string | null
}

export interface UserKeyUpdatePayload {
  name?: string
  enabled?: boolean
  key?: string
  image_daily_limit?: number
  image_monthly_limit?: number
  expires_at?: string | null
}

export interface UserKeyBatchPayload extends UserKeyUpdatePayload {
  ids: string[]
  delete?: boolean
}

export interface UserKeyBatchResponse {
  updated: UserKey[]
  deleted: string[]
}

export interface UserKeysResponse {
  items: UserKey[]
}

export interface UserKeyCreateResponse {
  item: UserKey
  raw_key: string
}

export interface UserKeyUpdatePayload {
  name?: string
  enabled?: boolean
  key?: string
  image_daily_limit?: number
  image_monthly_limit?: number
  expires_at?: string | null
}

export interface UserKeyUpdateResponse {
  item: UserKey
}

export interface UserKeyDeleteResponse {
  deleted_id: string
}

export const userKeysApi = {
  list: () => apiClient.get<never, UserKeysResponse>('/api/auth/users'),

  create: (payload: UserKeyCreatePayload) =>
    apiClient.post<UserKeyCreatePayload, UserKeyCreateResponse>('/api/auth/users', payload),

  update: (keyId: string, updates: UserKeyUpdatePayload) =>
    apiClient.post<UserKeyUpdatePayload, UserKeyUpdateResponse>(`/api/auth/users/${keyId}`, updates),

  batch: (payload: UserKeyBatchPayload) =>
    apiClient.post<UserKeyBatchPayload, UserKeyBatchResponse>('/api/auth/users/batch', payload),

  delete: (keyId: string) =>
    apiClient.delete<never, UserKeyDeleteResponse>(`/api/auth/users/${keyId}`),
}
