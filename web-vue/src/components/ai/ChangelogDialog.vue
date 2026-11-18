<template>
  <ModalShell
    :open="open"
    :z-index="110"
    panel-class="flex min-h-0 max-h-[82dvh] flex-col overflow-hidden p-6"
    close-on-backdrop
    @close="close"
  >
    <ModalHeader
      title="更新日志"
      :subtitle="subtitle"
      title-class="ui-subsection-title"
      :bordered="false"
      flush
      @close="close"
    />

    <section class="mt-4 rounded-xl border border-border bg-muted/25 px-4 py-3">
      <div class="flex flex-wrap items-center gap-2">
        <span class="text-sm font-semibold text-foreground">更新中心</span>
        <span class="text-xs text-muted-foreground">当前 v{{ view?.current_version || '—' }}</span>
        <span v-if="check?.latest_version" class="text-xs text-muted-foreground">
          · 最新 v{{ check.latest_version }}
        </span>
        <div class="ml-auto flex flex-wrap items-center gap-2">
          <Button size="xs" variant="outline" :disabled="checking || updating" @click="checkUpdates(true)">
            {{ checking ? '检测中…' : '检测更新' }}
          </Button>
          <Button
            v-if="canApply"
            size="xs"
            variant="primary"
            :disabled="updating"
            @click="applyUpdate"
          >
            {{ updating ? '更新中…' : `一键更新到 v${check?.latest_version}` }}
          </Button>
        </div>
      </div>
      <p v-if="updateError" class="mt-2 text-xs text-rose-600 dark:text-rose-400">{{ updateError }}</p>
      <p v-else-if="updateMessage" class="mt-2 text-xs text-muted-foreground">{{ updateMessage }}</p>
      <p v-else-if="check && check.error" class="mt-2 text-xs text-amber-600 dark:text-amber-400">
        {{ check.error }}
      </p>
      <p v-else-if="check" class="mt-2 text-xs text-muted-foreground">
        {{ check.update_available ? `发现新版本 v${check.latest_version}` : '已是最新版本' }}
        <span> · 检测于 {{ check.checked_at }}</span>
        <span v-if="!check.updater.updater_available"> · 宿主机更新代理未就绪（需安装 /root/qm_update_agent.sh 并启用 cron），一键更新暂不可用</span>
      </p>
      <p v-else class="mt-2 text-xs text-muted-foreground">
        点击「检测更新」从 GitHub 仓库读取最新版本；检测通过后可直接一键更新并自动重启容器。
      </p>
      <a
        v-if="check?.update_available && check.release_url"
        class="mt-1 inline-block text-xs text-primary underline"
        :href="check.release_url"
        target="_blank"
        rel="noreferrer"
      >
        打开 GitHub 发布页
      </a>
    </section>

    <div v-if="loading" class="mt-6 flex items-center justify-center gap-2 text-sm text-muted-foreground">
      <span class="h-2 w-2 animate-pulse rounded-full bg-primary"></span>
      正在读取本地更新日志…
    </div>

    <div v-else-if="error" class="mt-6 rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-700 dark:text-rose-300">
      {{ error }}
      <button type="button" class="ml-2 underline" @click="load">重试</button>
    </div>

    <div
      v-else-if="!view?.available"
      class="mt-6 rounded-xl border border-dashed border-border bg-muted/30 px-4 py-6 text-center text-sm text-muted-foreground"
    >
      未找到本地更新日志文件（CHANGELOG.md）。
    </div>

    <div v-else class="scrollbar-slim mt-5 min-h-0 flex-1 space-y-5 overflow-y-auto pr-2">
      <article
        v-for="release in visibleReleases"
        :key="release.version"
        class="border-l border-border pl-4"
      >
        <header class="flex flex-wrap items-center gap-2">
          <span class="text-sm font-semibold text-foreground">v{{ release.version }}</span>
          <span v-if="release.date" class="text-xs text-muted-foreground">{{ release.date }}{{ release.time ? ` ${release.time}` : '' }}</span>
          <MetaChip v-if="release.version === view.current_version" size="xs" tone="success" strong>
            当前版本
          </MetaChip>
          <MetaChip v-else-if="release.archived" size="xs" tone="muted">历史</MetaChip>
        </header>

        <div v-for="section in release.sections" :key="section.title" class="mt-2.5">
          <p class="text-xs font-medium text-muted-foreground">{{ section.title }}</p>
          <ul class="mt-1.5 space-y-1.5">
            <li
              v-for="(item, index) in section.items"
              :key="`${section.title}-${index}`"
              class="flex items-start gap-2 text-sm leading-6 text-muted-foreground"
            >
              <MetaChip
                v-if="item.type && item.type !== '说明'"
                size="xs"
                :tone="typeTone(item.type)"
                strong
                chip-class="mt-0.5 shrink-0"
              >
                {{ item.type }}
              </MetaChip>
              <span class="min-w-0 flex-1 text-foreground/85">{{ item.text }}</span>
            </li>
          </ul>
        </div>
      </article>
    </div>

    <ModalFooter class="mt-6" :bordered="false" flush>
      <Button
        v-if="view?.archive?.length"
        size="xs"
        variant="outline"
        @click="showArchive = !showArchive"
      >
        {{ showArchive ? '只看当前版本线' : `查看历史版本（${view.totals.archive}）` }}
      </Button>
      <Button size="xs" variant="primary" root-class="min-w-14 justify-center" @click="close">
        知道了
      </Button>
    </ModalFooter>
  </ModalShell>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Button } from 'nanocat-ui'

import { systemOpsApi, type ChangelogView, type UpdateCheckView } from '@/api/systemOps'
import MetaChip from '@/components/ai/MetaChip.vue'
import ModalFooter from '@/components/ai/ModalFooter.vue'
import ModalHeader from '@/components/ai/ModalHeader.vue'
import ModalShell from '@/components/ai/ModalShell.vue'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: [] }>()

const loading = ref(false)
const error = ref('')
const view = ref<ChangelogView | null>(null)
const showArchive = ref(false)

const check = ref<UpdateCheckView | null>(null)
const checking = ref(false)
const updating = ref(false)
const updateError = ref('')
const updateMessage = ref('')
let pollTimer: ReturnType<typeof setTimeout> | null = null
let pollDeadline = 0

const subtitle = computed(() => {
  const version = view.value?.current_version
  return version
    ? `当前版本 v${version} · 本地 CHANGELOG.md + GitHub 版本检测`
    : '本地 CHANGELOG.md + GitHub 版本检测'
})

const canApply = computed(() => {
  const data = check.value
  return Boolean(
    data
    && data.update_available
    && data.updater?.updater_available
    && data.latest_version
    && !updating.value,
  )
})

const visibleReleases = computed(() => {
  if (!view.value) return []
  return showArchive.value
    ? [...view.value.releases, ...view.value.archive]
    : view.value.releases
})

function close() {
  stopPolling()
  emit('close')
}

function typeTone(type: string): 'default' | 'muted' | 'success' | 'warning' | 'danger' | 'info' {
  if (['新增', '添加'].includes(type)) return 'success'
  if (['优化', '重构', '改进'].includes(type)) return 'info'
  if (['修复'].includes(type)) return 'warning'
  if (['移除', '删除', '废弃'].includes(type)) return 'danger'
  return 'muted'
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    view.value = await systemOpsApi.changelog(20)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '读取更新日志失败'
  } finally {
    loading.value = false
  }
}

async function checkUpdates(force = false) {
  if (checking.value || updating.value) return
  checking.value = true
  updateError.value = ''
  updateMessage.value = ''
  try {
    check.value = await systemOpsApi.checkUpdate(force)
    if (check.value.error) {
      updateError.value = ''
      return
    }
    updateMessage.value = check.value.update_available
      ? `发现新版本 v${check.value.latest_version}，可一键更新`
      : '已是最新版本'
  } catch (cause) {
    updateError.value = cause instanceof Error ? cause.message : '版本检测失败'
  } finally {
    checking.value = false
  }
}

function stopPolling() {
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
}

async function pollStatus() {
  pollTimer = null
  try {
    const status = await systemOpsApi.updateStatus()
    const agent = status.updater
    if (agent.phase === 'pulling' || agent.phase === 'applying') {
      updateMessage.value = agent.message || '宿主机正在拉取镜像并重启容器…'
      if (Date.now() < pollDeadline) pollTimer = setTimeout(pollStatus, 3000)
      return
    }
    if (agent.phase === 'done') {
      updating.value = false
      updateMessage.value = `已更新到 v${agent.current_version || agent.target_version}，页面将在 5 秒后自动刷新`
      setTimeout(() => window.location.reload(), 5000)
      return
    }
    if (agent.phase === 'failed') {
      updating.value = false
      updateError.value = agent.message || '宿主机更新失败，请查看 /data/update/agent.log'
      return
    }
    if (Date.now() < pollDeadline) pollTimer = setTimeout(pollStatus, 3000)
  } catch (cause) {
    // 容器重启期间接口会短暂不可用，继续重试直到超时。
    if (Date.now() < pollDeadline) pollTimer = setTimeout(pollStatus, 3000)
    else {
      updating.value = false
      updateError.value = cause instanceof Error ? cause.message : '更新状态查询失败'
    }
  }
}

async function applyUpdate() {
  const target = check.value?.latest_version
  if (!target || updating.value) return
  updating.value = true
  updateError.value = ''
  updateMessage.value = `正在提交 v${target} 更新请求…`
  try {
    const result = await systemOpsApi.applyUpdate(target)
    updateMessage.value = result.message || '已提交更新请求'
    pollDeadline = Date.now() + 5 * 60 * 1000
    pollTimer = setTimeout(pollStatus, 2000)
  } catch (cause) {
    updating.value = false
    updateError.value = cause instanceof Error ? cause.message : '提交更新失败'
  }
}

watch(
  () => props.open,
  (open) => {
    if (!open) {
      stopPolling()
      return
    }
    if (!view.value) void load()
    void checkUpdates(false)
  },
  { immediate: true },
)

onBeforeUnmount(stopPolling)
</script>
