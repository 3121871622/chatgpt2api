<template>
  <article
    v-memo="[signature, selected, syncing, refreshingAccessToken, busy, statusDetail, statusDetailCardClass]"
    class="ui-card flex h-full flex-col gap-4 transition-colors"
    :class="accountSurfaceClass(item, selected, 'card')"
  >
    <div class="flex items-start justify-between gap-3">
      <div class="flex min-w-0 items-start gap-3">
        <Checkbox
          :model-value="selected"
          :aria-label="`选择账号 ${accountPrimaryText(item)}`"
          @update:model-value="emit('toggle-select', item.id, $event)"
        />
        <div class="min-w-0">
          <h3 class="truncate text-sm font-medium text-foreground">{{ accountPrimaryText(item) }}</h3>
          <p class="mt-1 truncate font-mono text-xs text-muted-foreground">{{ accountSecondaryText(item) }}</p>
        </div>
      </div>
      <StatusDetailPill
        :label="statusText(item)"
        :tone="statusTone(item)"
        title="状态详情"
        detail-label="状态说明"
        raw-error-label="原始报错"
        :card-class="statusDetailCardClass"
        :detail="statusDetail"
        :raw-error="statusRawError(item)"
      />
    </div>

    <div class="flex flex-wrap items-center gap-2">
      <MetaChip tone="info">
        {{ accountSourceText(item) }}
      </MetaChip>
      <AccountCredentialStatus
        :item="item"
        @copy-credential="emit('copy-credential', item, $event)"
      />
    </div>

    <KeyValueList
      :items="accountDetailItems(item)"
      :columns="2"
    />

    <AccountActionButtons
      class="mt-auto"
      :item="item"
      :syncing="syncing"
      :refreshing-access-token="refreshingAccessToken"
      :busy="busy"
      @edit="emit('edit', item)"
      @test="emit('test', item)"
      @toggle-enabled="emit('toggle-enabled', item)"
      @sync-account="emit('sync-account', item)"
      @refresh-access-token="emit('refresh-access-token', item)"
      @remove="emit('remove', item)"
    />
  </article>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Checkbox, KeyValueList, MetaChip, StatusDetailPill } from 'nanocat-ui'

import AccountActionButtons from '@/components/ai/AccountActionButtons.vue'
import type { Account } from '@/api/accounts'
import AccountCredentialStatus from './AccountCredentialStatus.vue'
import type { AccountSelectionHandle } from './accountSelectionRuntime'
import {
  accountDetailItems,
  accountPrimaryText,
  accountRowSignature,
  accountSecondaryText,
  accountSourceText,
  accountSurfaceClass,
  statusRawError,
  statusText,
  statusTone,
} from './viewUtils'

const props = withDefaults(defineProps<{
  item: Account
  selection: AccountSelectionHandle
  statusDetail: string
  syncing?: boolean
  refreshingAccessToken?: boolean
  busy?: boolean
  statusDetailCardClass?: string
}>(), {
  syncing: false,
  refreshingAccessToken: false,
  busy: false,
  statusDetailCardClass: '',
})

const emit = defineEmits<{
  (e: 'toggle-select', id: string, checked: unknown): void
  (e: 'copy-credential', item: Account, kind: 'access' | 'refresh'): void
  (e: 'edit', item: Account): void
  (e: 'test', item: Account): void
  (e: 'toggle-enabled', item: Account): void
  (e: 'sync-account', item: Account): void
  (e: 'refresh-access-token', item: Account): void
  (e: 'remove', item: Account): void
}>()

// 选中状态由卡片自己按 id 从选择集读取，父组件渲染期不再逐行求值。
const selected = computed(() => props.selection.has(props.item.id))
const signature = computed(() => accountRowSignature(props.item))
</script>
