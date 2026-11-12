<template>
  <div class="rounded-2xl border border-border bg-card p-5">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <p class="text-sm font-medium text-foreground">后台登录方式</p>
        <p class="mt-1 text-xs text-muted-foreground">
          密钥登录默认开启。开启账号密码后，前台只显示账号密码登录；恢复密钥仍可用于重置密码和接口调用。
        </p>
      </div>
      <span class="rounded-full bg-muted px-2.5 py-1 text-xs text-muted-foreground">
        {{ config.mode === 'password' ? '账号密码登录' : '密钥登录' }}
      </span>
    </div>

    <div class="mt-4 grid gap-4 md:grid-cols-2">
      <div class="space-y-2">
        <p class="ui-field-label text-sm font-medium text-foreground">登录方式</p>
        <div class="flex gap-2">
          <Button
            size="sm"
            :variant="config.mode === 'key' ? 'primary' : 'outline'"
            :disabled="saving"
            @click="config.mode = 'key'"
          >
            密钥登录
          </Button>
          <Button
            size="sm"
            :variant="config.mode === 'password' ? 'primary' : 'outline'"
            :disabled="saving"
            @click="config.mode = 'password'"
          >
            账号密码登录
          </Button>
        </div>
      </div>

      <div class="space-y-2">
        <p class="ui-field-label text-sm font-medium text-foreground">管理员账号</p>
        <Input
          v-model="config.username"
          size="md"
          block
          placeholder="admin"
          :disabled="saving || config.mode === 'key'"
        />
      </div>

      <div class="space-y-2">
        <p class="ui-field-label text-sm font-medium text-foreground">
          {{ config.has_password ? '修改密码（留空不修改）' : '设置密码' }}
        </p>
        <Input
          v-model="newPassword"
          type="password"
          size="md"
          block
          placeholder="至少 8 位"
          :disabled="saving || config.mode === 'key'"
        />
      </div>

      <div class="space-y-2">
        <p class="ui-field-label text-sm font-medium text-foreground">确认密码</p>
        <Input
          v-model="confirmPassword"
          type="password"
          size="md"
          block
          placeholder="再次输入密码"
          :disabled="saving || config.mode === 'key'"
        />
      </div>
    </div>

    <div class="mt-4 space-y-2">
      <p class="ui-field-label text-sm font-medium text-foreground">后台路径</p>
      <Input
        v-model="config.admin_path"
        size="md"
        block
        placeholder="/admin"
        :disabled="saving"
      />
      <p class="text-xs leading-5 text-muted-foreground">
        修改后旧路径立即失效，请先保存书签。忘记路径时打开
        <a href="/admin-recover" target="_blank" rel="noopener" class="text-primary underline-offset-4 hover:underline">/admin-recover</a>
        用恢复密钥重新设置。
      </p>
    </div>

    <div class="mt-4 flex flex-wrap items-center justify-between gap-3">
      <p class="text-xs text-muted-foreground">
        恢复密钥：{{ config.recovery_key_enabled ? '已配置，可用于忘记密码时重置' : '未配置' }}
      </p>
      <Button size="sm" variant="primary" :disabled="saving || !dirty" @click="save">
        {{ saving ? '保存中...' : '保存登录方式' }}
      </Button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Button, Input } from 'nanocat-ui'
import { authApi, type LoginConfig } from '@/api/auth'
import { useToast } from '@/composables/useToast'

const toast = useToast()
const saving = ref(false)
const newPassword = ref('')
const confirmPassword = ref('')
const initial = ref<LoginConfig | null>(null)
const config = reactive<LoginConfig>({
  mode: 'key',
  username: 'admin',
  has_password: false,
  password_login_enabled: false,
  recovery_key_enabled: true,
  admin_path: '/admin',
})

const dirty = computed(() => {
  const original = initial.value
  if (!original) return false
  return config.mode !== original.mode
    || config.username !== original.username
    || (config.admin_path || '') !== (original.admin_path || '')
    || Boolean(newPassword.value)
})

function apply(next: LoginConfig) {
  Object.assign(config, next)
  initial.value = { ...next }
}

onMounted(async () => {
  try {
    apply(await authApi.getLoginSettings())
  } catch (error: any) {
    toast.error(error.message || '读取登录设置失败')
  }
})

async function save() {
  if (newPassword.value !== confirmPassword.value) {
    toast.error('两次输入的密码不一致。')
    return
  }
  if (config.mode === 'password' && !config.has_password && !newPassword.value) {
    toast.error('开启账号密码登录前请先设置密码。')
    return
  }
  if (newPassword.value && newPassword.value.length < 8) {
    toast.error('密码至少需要 8 位。')
    return
  }
  saving.value = true
  try {
    const previousPath = initial.value?.admin_path || '/admin'
    const next = await authApi.saveLoginSettings({
      mode: config.mode,
      username: config.username,
      password: newPassword.value || undefined,
      admin_path: config.admin_path || '/admin',
    })
    apply(next)
    newPassword.value = ''
    confirmPassword.value = ''
    if (next.admin_path && next.admin_path !== previousPath) {
      window.history.replaceState(null, '', next.admin_path + window.location.hash)
    }
    toast.success('登录方式已保存')
  } catch (error: any) {
    toast.error(error.message || '保存失败')
  } finally {
    saving.value = false
  }
}
</script>
