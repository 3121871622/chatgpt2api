<template>
  <div class="space-y-4">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <p class="ui-section-title">用户密钥管理</p>
        <p class="mt-1 text-xs text-muted-foreground">
          创建给普通用户使用的调用密钥；普通用户登录后只进入对话画图页。
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="outline" :disabled="userKeysLoading" @click="$emit('load')">
          {{ userKeysLoading ? '刷新中...' : '刷新密钥' }}
        </Button>
        <Button size="sm" variant="primary" :disabled="userKeyBusy === 'create'" @click="$emit('create')">
          创建用户密钥
        </Button>
      </div>
    </div>

    <div v-if="userKeys.length" class="flex flex-wrap items-center gap-2 rounded-xl border border-border bg-muted/20 px-3 py-2">
      <label class="flex items-center gap-1 text-xs text-muted-foreground">
        <input type="checkbox" :checked="allSelected" @change="toggleAll(($event.target as HTMLInputElement).checked)" />
        全选
      </label>
      <span class="text-xs text-muted-foreground">已选 {{ selectedIds.length }}</span>
      <Input v-model.number="batchDailyLimit" type="number" min="0" placeholder="日限" class="w-20" />
      <Input v-model.number="batchMonthlyLimit" type="number" min="0" placeholder="月限" class="w-20" />
      <Button size="xs" variant="outline" :disabled="!selectedIds.length || batchBusy" @click="applyLimits">应用额度</Button>
      <Button size="xs" variant="outline" :disabled="!selectedIds.length || batchBusy" @click="setEnabled(true)">启用</Button>
      <Button size="xs" variant="outline" :disabled="!selectedIds.length || batchBusy" @click="setEnabled(false)">禁用</Button>
      <Button size="xs" variant="ghost" root-class="text-rose-600" :disabled="!selectedIds.length || batchBusy" @click="deleteSelected">删除</Button>
    </div>

    <div
      v-if="newUserKey"
      class="rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-900"
    >
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div class="min-w-0">
          <p class="font-medium">新密钥只展示一次，请现在复制保存。</p>
          <p class="mt-2 break-all font-mono text-xs">{{ newUserKey }}</p>
        </div>
        <Button
          size="xs"
          variant="outline"
          root-class="shrink-0 border-emerald-200 bg-white text-emerald-700"
          @click="$emit('copy', newUserKey)"
        >
          复制
        </Button>
      </div>
    </div>

    <PageLoadingState
      v-if="userKeysLoading"
      compact
      title="正在加载用户密钥"
      description="读取普通用户密钥列表。"
    />
    <StateBlock v-else-if="userKeys.length === 0" compact dashed>
      暂无普通用户密钥。创建后可以分发给只需要画图入口的用户。
    </StateBlock>
    <div v-else class="space-y-2">
      <div
        v-for="item in userKeys"
        :key="item.id"
        class="flex flex-col gap-3 rounded-xl border border-border bg-card px-4 py-3 md:flex-row md:items-center md:justify-between"
      >
        <div class="flex items-center gap-2">
          <input type="checkbox" :checked="selectedIds.includes(item.id)" @change="toggleOne(item.id, ($event.target as HTMLInputElement).checked)" />
          <div class="min-w-0">
          <div class="flex flex-wrap items-center gap-2">
            <p class="truncate text-sm font-medium text-foreground">{{ item.name || '普通用户' }}</p>
            <StateBadge
              :tone="item.enabled ? 'success' : 'muted'"
              size="xs"
              shape="rounded"
            >
              {{ item.enabled ? '已启用' : '已禁用' }}
            </StateBadge>
          </div>
          <p class="mt-1 text-xs text-muted-foreground">
            创建 {{ formatDateTime(item.created_at) }} · 最近使用 {{ formatDateTime(item.last_used_at) }}
          </p>
          <p class="mt-1 text-xs text-muted-foreground">
            今日 {{ item.image_daily_used }}/{{ item.image_daily_limit || '不限' }} · 本月 {{ item.image_monthly_used }}/{{ item.image_monthly_limit || '不限' }}
            <span v-if="item.expires_at"> · 到期 {{ item.expires_at }}</span>
          </p>
          </div>
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <Button
            size="xs"
            variant="outline"
            :disabled="userKeyBusy === item.id"
            @click="$emit('edit', item)"
          >
            编辑
          </Button>
          <Button
            size="xs"
            variant="outline"
            :disabled="userKeyBusy === item.id"
            @click="$emit('toggle', item)"
          >
            {{ item.enabled ? '禁用' : '启用' }}
          </Button>
          <Button
            size="xs"
            variant="outline"
            root-class="text-rose-600"
            :disabled="userKeyBusy === item.id"
            @click="$emit('delete', item)"
          >
            删除
          </Button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { Button, Input } from 'nanocat-ui'
import type { UserKey } from '@/api/userKeys'
import { userKeysApi } from '@/api/userKeys'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useToast } from '@/composables/useToast'
import { errorMessage } from '@/lib/errorMessage'
import PageLoadingState from '@/components/ai/PageLoadingState.vue'
import StateBadge from '@/components/ai/StateBadge.vue'
import StateBlock from '@/components/ai/StateBlock.vue'
import { formatDateTime } from '@/views/settings/settingsView'

const props = defineProps<{
  userKeys: UserKey[]
  userKeysLoading: boolean
  userKeyBusy: string
  newUserKey: string
}>()

const emit = defineEmits<{
  load: []
  create: []
  copy: [value: string]
  edit: [item: UserKey]
  toggle: [item: UserKey]
  delete: [item: UserKey]
}>()

const selectedIds = ref<string[]>([])
const batchDailyLimit = ref(0)
const batchMonthlyLimit = ref(0)
const batchBusy = ref(false)
const toast = useToast()
const confirmDialog = useConfirmDialog()

const allSelected = computed(() => (
  props.userKeys.length > 0 && selectedIds.value.length === props.userKeys.length
))

function toggleAll(checked: boolean) {
  selectedIds.value = checked ? props.userKeys.map(item => item.id) : []
}

function toggleOne(id: string, checked: boolean) {
  const next = new Set(selectedIds.value)
  if (checked) next.add(id)
  else next.delete(id)
  selectedIds.value = [...next]
}

async function runBatch(payload: {
  enabled?: boolean
  image_daily_limit?: number
  image_monthly_limit?: number
  delete?: boolean
}) {
  if (!selectedIds.value.length) return
  batchBusy.value = true
  try {
    await userKeysApi.batch({ ids: selectedIds.value, ...payload })
    selectedIds.value = []
    toast.success('批量操作已完成')
    emit('load')
  } catch (error) {
    toast.error(errorMessage(error, '批量操作失败'))
  } finally {
    batchBusy.value = false
  }
}

function applyLimits() {
  return runBatch({
    image_daily_limit: Math.max(0, Number(batchDailyLimit.value || 0)),
    image_monthly_limit: Math.max(0, Number(batchMonthlyLimit.value || 0)),
  })
}

function setEnabled(enabled: boolean) {
  return runBatch({ enabled })
}

async function deleteSelected() {
  if (!selectedIds.value.length) return
  const confirmed = await confirmDialog.ask({
    title: '批量删除用户密钥',
    message: `确定删除选中的 ${selectedIds.value.length} 条用户密钥吗？删除后无法恢复。`,
    confirmText: '删除',
    cancelText: '取消',
  })
  if (confirmed) await runBatch({ delete: true })
}
</script>
