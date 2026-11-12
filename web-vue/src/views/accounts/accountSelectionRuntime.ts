import { computed, reactive, ref, shallowRef, type ComputedRef, type Ref } from 'vue'

import type {
  Account,
  AccountCredentialFilter,
  AccountSelectionPreview,
  AccountSelectionScope,
  AccountStatusCategory,
} from '@/api/accounts'

type AccountSelectionRuntimeOptions = {
  accounts: Ref<Account[]>
  pagedAccounts: ComputedRef<Account[]>
  total: Ref<number>
  allTotal: Ref<number>
  keyword: Ref<string>
  status: Ref<'all' | AccountStatusCategory>
  groupId: Ref<string>
  credential: Ref<AccountCredentialFilter>
}

type FilterSnapshot = Pick<AccountSelectionScope, 'keyword' | 'status' | 'group_id' | 'credential'>

/** 行组件只需要按 id 判定选中，不参与父组件渲染期的逐行求值。 */
export type AccountSelectionHandle = {
  has: (accountId: string) => boolean
}

export function useAccountSelectionRuntime(options: AccountSelectionRuntimeOptions) {
  const selectionMode = ref<'explicit' | 'filter' | 'all'>('explicit')
  // 按 id 以 key 粒度追踪：Set 原地增删，行组件读取 `has(id)` 只会让对应行失效。
  const explicitSelected = reactive(new Set<string>())
  const scopedExcluded = reactive(new Set<string>())
  // Set 的 size / 遍历会追踪 ITERATE 依赖，因此计数单独维护，渲染期只读计数器。
  const explicitCount = ref(0)
  const excludedCount = ref(0)
  const filterSnapshot = ref<FilterSnapshot>({
    keyword: '',
    status: 'all',
    group_id: 'all',
    credential: 'all',
  })
  const selectionRevision = ref(0)
  const authoritativeSelectedCount = ref<number | null>(null)
  // 当前页 id 与其中已选数量：表头“全选本页”用 O(1) 计数，不再整页扫描。
  const currentPageIds = shallowRef<ReadonlySet<string>>(new Set<string>())
  const currentPageSelectedCount = ref(0)

  const scopedSelectionActive = computed(() => selectionMode.value !== 'explicit')
  const selectedCount = computed(() => {
    if (!scopedSelectionActive.value) return explicitCount.value
    if (authoritativeSelectedCount.value !== null) return authoritativeSelectedCount.value
    const scopeTotal = selectionMode.value === 'all' ? options.allTotal.value : options.total.value
    return Math.max(0, scopeTotal - excludedCount.value)
  })
  const selectedIds = computed<string[]>(() => Array.from(explicitSelected))
  const selectionScope = computed<AccountSelectionScope>(() => {
    if (!scopedSelectionActive.value) {
      return { mode: 'explicit', account_ids: selectedIds.value }
    }
    if (selectionMode.value === 'all') {
      return {
        mode: 'all',
        excluded_account_ids: Array.from(scopedExcluded),
      }
    }
    return {
      mode: 'filter',
      ...filterSnapshot.value,
      excluded_account_ids: Array.from(scopedExcluded),
    }
  })
  const allVisibleSelected = computed(() => (
    currentPageIds.value.size > 0
    && currentPageSelectedCount.value === currentPageIds.value.size
  ))
  const someVisibleSelected = computed(() => (
    currentPageSelectedCount.value > 0
    && currentPageSelectedCount.value < currentPageIds.value.size
  ))
  const allMatchingSelected = computed(() => {
    if (!options.total.value || excludedCount.value) return false
    if (selectionMode.value === 'all') return true
    if (selectionMode.value !== 'filter') return false
    return filterSnapshot.value.keyword === options.keyword.value.trim()
      && filterSnapshot.value.status === options.status.value
      && filterSnapshot.value.group_id === options.groupId.value
      && (filterSnapshot.value.credential || 'all') === options.credential.value
  })

  function isSelected(accountId: string) {
    return scopedSelectionActive.value
      ? !scopedExcluded.has(accountId)
      : explicitSelected.has(accountId)
  }

  function applyExplicitSelection(accountId: string, selected: boolean) {
    if (selected === explicitSelected.has(accountId)) return false
    if (selected) {
      explicitSelected.add(accountId)
      explicitCount.value += 1
    } else {
      explicitSelected.delete(accountId)
      explicitCount.value -= 1
    }
    return true
  }

  function applyScopedSelection(accountId: string, selected: boolean) {
    if (selected === !scopedExcluded.has(accountId)) return false
    if (selected) {
      scopedExcluded.delete(accountId)
      excludedCount.value -= 1
    } else {
      scopedExcluded.add(accountId)
      excludedCount.value += 1
    }
    return true
  }

  function applySelection(accountId: string, selected: boolean) {
    const changed = scopedSelectionActive.value
      ? applyScopedSelection(accountId, selected)
      : applyExplicitSelection(accountId, selected)
    if (!changed) return false
    if (currentPageIds.value.has(accountId)) {
      currentPageSelectedCount.value += selected ? 1 : -1
    }
    return true
  }

  function recountCurrentPageSelection() {
    const ids = new Set<string>()
    let selected = 0
    for (const item of options.pagedAccounts.value) {
      ids.add(item.id)
      if (isSelected(item.id)) selected += 1
    }
    currentPageIds.value = ids
    currentPageSelectedCount.value = selected
  }

  function pruneToCurrentAccounts() {
    if (!scopedSelectionActive.value && explicitCount.value) {
      const existingIds = new Set(options.accounts.value.map((item) => item.id))
      const staleIds: string[] = []
      for (const accountId of explicitSelected) {
        if (!existingIds.has(accountId)) staleIds.push(accountId)
      }
      for (const accountId of staleIds) {
        explicitSelected.delete(accountId)
        explicitCount.value -= 1
      }
      if (staleIds.length) selectionRevision.value += 1
    }
    recountCurrentPageSelection()
  }

  function toggleSelect(accountId: string, checked?: boolean) {
    const shouldSelect = typeof checked === 'boolean' ? checked : !isSelected(accountId)
    if (applySelection(accountId, shouldSelect) && scopedSelectionActive.value) {
      authoritativeSelectedCount.value = null
    }
    selectionRevision.value += 1
  }

  // 空集合上的 clear() 也会触发全部按 key 追踪的行失效，先判空避免无谓重渲染。
  function clearSelectionBuckets() {
    if (explicitSelected.size) explicitSelected.clear()
    if (scopedExcluded.size) scopedExcluded.clear()
  }

  function clearSelection() {
    selectionMode.value = 'explicit'
    clearSelectionBuckets()
    explicitCount.value = 0
    excludedCount.value = 0
    authoritativeSelectedCount.value = null
    currentPageSelectedCount.value = 0
    selectionRevision.value += 1
  }

  function clearExplicitSelection() {
    if (!scopedSelectionActive.value) clearSelection()
  }

  function resetScopedBuckets(total: number) {
    clearSelectionBuckets()
    explicitCount.value = 0
    excludedCount.value = 0
    authoritativeSelectedCount.value = total
    recountCurrentPageSelection()
    selectionRevision.value += 1
  }

  function selectAllMatching() {
    if (!options.total.value) return
    selectionMode.value = 'filter'
    filterSnapshot.value = {
      keyword: options.keyword.value.trim(),
      status: options.status.value,
      group_id: options.groupId.value,
      credential: options.credential.value,
    }
    resetScopedBuckets(options.total.value)
  }

  function selectAllAccounts() {
    if (!options.allTotal.value) return
    selectionMode.value = 'all'
    resetScopedBuckets(options.allTotal.value)
  }

  function toggleSelectAllVisible(checked?: boolean) {
    const shouldSelect = typeof checked === 'boolean' ? checked : !allVisibleSelected.value
    for (const item of options.pagedAccounts.value) {
      applySelection(item.id, shouldSelect)
    }
    if (scopedSelectionActive.value) authoritativeSelectedCount.value = null
    selectionRevision.value += 1
  }

  function syncScopedExclusions(nextExcluded: ReadonlySet<string>) {
    const staleIds: string[] = []
    for (const accountId of scopedExcluded) {
      if (!nextExcluded.has(accountId)) staleIds.push(accountId)
    }
    for (const accountId of staleIds) scopedExcluded.delete(accountId)
    for (const accountId of nextExcluded) {
      if (!scopedExcluded.has(accountId)) scopedExcluded.add(accountId)
    }
    excludedCount.value = nextExcluded.size
  }

  function reconcileScopedSelection(
    preview: AccountSelectionPreview,
    expectedRevision: number,
  ) {
    if (!scopedSelectionActive.value || selectionRevision.value !== expectedRevision) return false
    const nextExcluded = new Set(
      (preview.excluded_account_ids || []).map((id) => String(id || '').trim()).filter(Boolean),
    )
    syncScopedExclusions(nextExcluded)
    const matchingCount = Math.max(0, Number(preview.matching_count || 0))
    authoritativeSelectedCount.value = Math.min(
      matchingCount,
      Math.max(0, Number(preview.selected_count || 0)),
    )
    recountCurrentPageSelection()
    return true
  }

  function removeSelectedIds(ids: readonly string[]) {
    if (!ids.length) return
    if (scopedSelectionActive.value) {
      clearSelection()
      return
    }
    for (const accountId of ids) {
      if (!explicitSelected.has(accountId)) continue
      explicitSelected.delete(accountId)
      explicitCount.value -= 1
      if (currentPageIds.value.has(accountId)) currentPageSelectedCount.value -= 1
    }
    selectionRevision.value += 1
  }

  const selection: AccountSelectionHandle = { has: isSelected }

  return {
    selectedIds,
    selection,
    selectedCount,
    selectionRevision,
    selectionScope,
    scopedSelectionActive,
    allVisibleSelected,
    someVisibleSelected,
    allMatchingSelected,
    pruneToCurrentAccounts,
    isSelected,
    toggleSelect,
    clearSelection,
    clearExplicitSelection,
    selectAllMatching,
    selectAllAccounts,
    toggleSelectAllVisible,
    reconcileScopedSelection,
    removeSelectedIds,
  }
}
