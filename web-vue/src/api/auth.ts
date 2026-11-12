import apiClient, { clearAuthToken, setAuthToken } from './client'

export type AuthRole = 'admin' | 'user' | 'unknown'
export type AuthCapability = 'admin_console' | 'studio'

export interface AuthSubject {
  id: string
  name: string
  role: AuthRole
}

export interface AuthCapabilities {
  admin_console: boolean
  studio: boolean
}

export interface AuthView {
  schema_version: 1
  authenticated: boolean
  version: string
  subject: AuthSubject | null
  capabilities: AuthCapabilities
  home_route: '/login' | '/admin' | '/studio'
  session_token?: string | null
}

export interface LoginRequest {
  password: string
}

export interface PasswordLoginRequest {
  username: string
  password: string
}

export interface LoginConfig {
  mode: 'key' | 'password'
  username: string
  has_password: boolean
  password_login_enabled: boolean
  recovery_key_enabled: boolean
  admin_path?: string
}

export interface LoginSettingsPatch {
  mode: 'key' | 'password'
  username: string
  password?: string
  admin_path?: string
}

export const authApi = {
  async login(data: LoginRequest) {
    setAuthToken(data.password)
    try {
      return await apiClient.post<never, AuthView>('/auth/login')
    } catch (error) {
      clearAuthToken()
      throw error
    }
  },

  async loginWithPassword(data: PasswordLoginRequest) {
    clearAuthToken()
    const view = await apiClient.post<PasswordLoginRequest, AuthView>('/auth/login', data)
    if (view.session_token) {
      setAuthToken(view.session_token)
    }
    return view
  },

  getLoginConfig: () => apiClient.get<never, LoginConfig>('/auth/config', { timeout: 8000 }),

  async resetPassword(key: string, newPassword: string) {
    setAuthToken(key)
    try {
      return await apiClient.post<{ new_password: string }, LoginConfig>('/auth/password/reset', {
        new_password: newPassword,
      })
    } finally {
      clearAuthToken()
    }
  },

  getLoginSettings: () =>
    apiClient.get<never, LoginConfig>('/api/system/login-settings', { timeout: 8000 }),

  saveLoginSettings: (data: LoginSettingsPatch) =>
    apiClient.put<LoginSettingsPatch, LoginConfig>('/api/system/login-settings', data),

  logout: () => {
    clearAuthToken()
    return Promise.resolve({ ok: true })
  },

  checkAuth: () => apiClient.get<never, AuthView>('/auth/status', { timeout: 8000 }),
}
