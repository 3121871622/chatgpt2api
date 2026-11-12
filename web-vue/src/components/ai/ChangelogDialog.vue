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

    <div v-if="loading" class="mt-6 flex items-center justify-center gap-2 text-sm text-muted-foreground">
      <span class="h-2 w-2 animate-pulse rounded-full bg-primary"></span>
      正在读取本地更新日志…
    </div>

    <div v-else-if="error" class="mt-6 rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-700 dark:text-rose-300">
      {{ error }}
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
import { computed, ref, watch } from 'vue'
import { Button } from 'nanocat-ui'

import { systemOpsApi, type ChangelogView } from '@/api/systemOps'
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

const subtitle = computed(() => {
  const version = view.value?.current_version
  return version ? `当前版本 v${version} · 仅读取本地 CHANGELOG.md，不联网校验` : '仅读取本地 CHANGELOG.md，不联网校验'
})

const visibleReleases = computed(() => {
  if (!view.value) return []
  return showArchive.value
    ? [...view.value.releases, ...view.value.archive]
    : view.value.releases
})

function close() {
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

watch(
  () => props.open,
  (open) => {
    if (open && !view.value) void load()
  },
  { immediate: true },
)
</script>
