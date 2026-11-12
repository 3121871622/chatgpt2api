<template>
  <div class="rounded-2xl border border-border bg-card p-5">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <p class="text-sm font-medium text-foreground">自定义首页</p>
        <p class="mt-1 text-xs text-muted-foreground">
          配置根路径 <code>/</code> 返回的 HTML。关闭或内容为空时，访问根路径返回 404；后台仍固定为
          <code>/admin</code>。
        </p>
      </div>
      <span class="rounded-full bg-muted px-2.5 py-1 text-xs text-muted-foreground">
        {{ enabled ? '已开启' : '未开启' }}
      </span>
    </div>

    <div class="mt-4 flex flex-wrap items-center gap-3">
      <label class="inline-flex items-center gap-2 text-sm text-foreground">
        <input v-model="enabled" type="checkbox" class="h-4 w-4" :disabled="saving" />
        开启自定义首页
      </label>
      <span class="text-xs text-muted-foreground">
        {{ updatedAt ? `最后更新：${updatedAt}` : '尚未保存过自定义首页' }}
      </span>
    </div>

    <textarea
      v-model="html"
      rows="10"
      class="ui-textarea-sm mt-4 w-full font-mono text-xs"
      :disabled="saving || !enabled"
      placeholder="<!doctype html><html>...</html>"
    ></textarea>

    <div class="mt-4 flex flex-wrap items-center justify-between gap-3">
      <a
        v-if="enabled"
        href="/"
        target="_blank"
        rel="noopener"
        class="text-xs text-primary underline-offset-4 hover:underline"
      >
        打开首页预览
      </a>
      <span v-else class="text-xs text-muted-foreground">
        当前访问 / 会返回 404
      </span>
      <Button size="sm" variant="primary" :disabled="saving" @click="save">
        {{ saving ? '保存中...' : '保存首页' }}
      </Button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { Button } from 'nanocat-ui'
import apiClient from '@/api/client'
import { useToast } from '@/composables/useToast'

interface HomepageConfig {
  enabled: boolean
  html: string
  updated_at: string
}

const toast = useToast()
const saving = ref(false)
const enabled = ref(false)
const html = ref('')
const updatedAt = ref('')

function apply(value: HomepageConfig) {
  enabled.value = value.enabled === true
  html.value = value.html || ''
  updatedAt.value = value.updated_at || ''
}

onMounted(async () => {
  try {
    apply(await apiClient.get<never, HomepageConfig>('/api/system/homepage', { timeout: 8000 }))
  } catch (error: any) {
    toast.error(error.message || '读取自定义首页失败')
  }
})

async function save() {
  saving.value = true
  try {
    const next = await apiClient.put<never, HomepageConfig>('/api/system/homepage', {
      enabled: enabled.value,
      html: html.value,
    }, { timeout: 30000 })
    apply(next)
    toast.success('自定义首页已保存')
  } catch (error: any) {
    toast.error(error.message || '保存自定义首页失败')
  } finally {
    saving.value = false
  }
}
</script>
