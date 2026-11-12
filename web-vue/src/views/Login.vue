<template>
  <div class="min-h-screen px-4">
    <div class="flex min-h-screen items-center justify-center">
      <div class="w-full max-w-md rounded-[2.5rem] border border-border bg-card p-10 shadow-2xl shadow-black/10">
        <div class="text-center">
          <h1 class="text-3xl font-semibold text-foreground">倾梦GPT</h1>
          <p class="mt-2 text-sm text-muted-foreground">
            {{ resetMode ? '重置管理员密码' : loginMode === 'password' ? '账号密码登录' : '控制台登录' }}
          </p>
        </div>

        <div v-if="configLoading" class="mt-8 py-6 text-center text-sm text-muted-foreground">
          正在读取登录方式...
        </div>

        <form v-else-if="resetMode" class="mt-8 space-y-5" @submit.prevent="handleReset">
          <div class="space-y-2">
            <label for="reset-key" class="ui-field-label text-sm font-medium text-foreground">
              恢复密钥
            </label>
            <Input
              id="reset-key"
              v-model="recoveryKey"
              type="password"
              size="md"
              block
              placeholder="输入 QINGMENG_AUTH_KEY"
              :disabled="isLoading"
            />
          </div>
          <div class="space-y-2">
            <label for="new-password" class="ui-field-label text-sm font-medium text-foreground">
              新密码
            </label>
            <Input
              id="new-password"
              v-model="newPassword"
              type="password"
              size="md"
              block
              placeholder="至少 8 位"
              :disabled="isLoading"
            />
          </div>
          <div class="space-y-2">
            <label for="confirm-password" class="ui-field-label text-sm font-medium text-foreground">
              确认新密码
            </label>
            <Input
              id="confirm-password"
              v-model="confirmPassword"
              type="password"
              size="md"
              block
              placeholder="再次输入新密码"
              :disabled="isLoading"
            />
          </div>
          <Button
            type="submit"
            size="md"
            variant="primary"
            block
            :disabled="isLoading || !recoveryKey || !newPassword"
          >
            {{ isLoading ? '重置中...' : '重置密码' }}
          </Button>
          <button
            type="button"
            class="w-full text-center text-xs text-muted-foreground underline-offset-4 hover:underline"
            :disabled="isLoading"
            @click="resetMode = false"
          >
            返回登录
          </button>
        </form>

        <form v-else-if="loginMode === 'password'" class="mt-8 space-y-5" @submit.prevent="handleLogin">
          <div class="space-y-2">
            <label for="username" class="ui-field-label text-sm font-medium text-foreground">
              管理员账号
            </label>
            <Input
              id="username"
              v-model="username"
              type="text"
              size="md"
              block
              placeholder="管理员用户名"
              :disabled="isLoading"
            />
          </div>
          <div class="space-y-2">
            <label for="password" class="ui-field-label text-sm font-medium text-foreground">
              密码
            </label>
            <Input
              id="password"
              v-model="password"
              type="password"
              size="md"
              block
              placeholder="管理员密码"
              :disabled="isLoading"
            />
          </div>
          <Button
            type="submit"
            size="md"
            variant="primary"
            block
            :disabled="isLoading || !username || !password"
          >
            {{ isLoading ? '登录中...' : '登录' }}
          </Button>
          <button
            type="button"
            class="w-full text-center text-xs text-muted-foreground underline-offset-4 hover:underline"
            :disabled="isLoading"
            @click="resetMode = true"
          >
            忘记密码？使用密钥重置
          </button>
        </form>

        <form v-else class="mt-8 space-y-6" @submit.prevent="handleLogin">
          <div class="space-y-2">
            <label for="password" class="ui-field-label text-sm font-medium text-foreground">
              管理密钥
            </label>
            <Input
              id="password"
              v-model="password"
              type="password"
              size="md"
              block
              placeholder="输入 Bearer key"
              :disabled="isLoading"
            />
          </div>

          <Button
            type="submit"
            size="md"
            variant="primary"
            block
            :disabled="isLoading || !password"
          >
            {{ isLoading ? '登录中...' : '登录' }}
          </Button>
        </form>

        <div class="mt-8 flex items-center justify-center gap-3 text-xs text-muted-foreground">
          <span class="inline-flex items-center gap-1.5">
            <span class="h-1.5 w-1.5 rounded-full bg-emerald-500"></span>
            倾梦GPT · 企业级百万号池系统
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Button, Input } from 'nanocat-ui'
import { authApi, type LoginConfig } from '@/api/auth'
import { useToast } from '@/composables/useToast'
import { resolveLoginRedirect } from '@/router/routes'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()
const toast = useToast()

const configLoading = ref(true)
const loginMode = ref<'key' | 'password'>('key')
const username = ref('')
const password = ref('')
const recoveryKey = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const resetMode = ref(false)
const isLoading = ref(false)

function applyConfig(config: LoginConfig) {
  loginMode.value = config.mode
  username.value = config.username || 'admin'
}

onMounted(async () => {
  try {
    applyConfig(await authApi.getLoginConfig())
  } catch {
    loginMode.value = 'key'
  } finally {
    configLoading.value = false
  }
})

async function afterLogin() {
  await router.replace(resolveLoginRedirect(route.query.redirect, authStore.homeRoute))
}

async function handleLogin() {
  isLoading.value = true
  try {
    const loggedIn = loginMode.value === 'password'
      ? await authStore.loginWithPassword(username.value, password.value)
      : await authStore.login(password.value)
    if (!loggedIn) {
      toast.error(loginMode.value === 'password' ? '用户名或密码错误。' : '密钥无效或已失效。')
      return
    }
    await afterLogin()
  } catch (error: any) {
    toast.error(error.message || '登录失败，请检查登录信息。')
  } finally {
    isLoading.value = false
  }
}

async function handleReset() {
  if (newPassword.value !== confirmPassword.value) {
    toast.error('两次输入的新密码不一致。')
    return
  }
  if (newPassword.value.length < 8) {
    toast.error('新密码至少需要 8 位。')
    return
  }
  isLoading.value = true
  try {
    const config = await authApi.resetPassword(recoveryKey.value, newPassword.value)
    applyConfig(config)
    resetMode.value = false
    toast.success('密码已重置，请使用新密码登录。')
    password.value = ''
  } catch (error: any) {
    toast.error(error.message || '重置失败，请检查恢复密钥。')
  } finally {
    isLoading.value = false
  }
}
</script>
