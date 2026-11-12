import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch, type ComputedRef, type Ref } from 'vue'

import type { Account } from '@/api/accounts'
import { useWindowedList } from '@/composables/useWindowedList'

// 必须与 Accounts.vue 中 `tbody tr.account-row` 的固定行高保持一致（px）。
export const ACCOUNT_ROW_HEIGHT = 64
// 少量行不做窗口化：占位行与滚动容器的额外复杂度没有收益。
const WINDOW_MIN_ROWS = 40
const TABLE_SCROLL_SELECTOR = '.table-shell__scroll'

type AccountRowWindowOptions = {
  rows: ComputedRef<readonly Account[]>
  containerRef: Ref<HTMLElement | null>
  // 视图模式 / 布局模式变化后需要重新定位滚动容器（TableShell 会重挂载或改变滚动模式）。
  refreshKey: ComputedRef<string>
  // 当前是否处于表格视图：卡片视图下没有滚动容器，不能据此判定窗口化失效。
  active: ComputedRef<boolean>
}

/**
 * 账号表格窗口化渲染。
 *
 * TableShell 没有暴露滚动元素，但滚动容器的类名固定为 `.table-shell__scroll`
 * （nanocat-ui 0.1.10），页面侧从容器根节点查询即可，不改公共组件。
 * 行高按实际渲染结果测量一次，避免 CSS 行高与常量不一致时占位高度漂移；
 * 若升级 Nanocat 后类名变化，则退化为整页渲染而不是只渲染一屏。
 */
export function useAccountRowWindow(options: AccountRowWindowOptions) {
  const measuredRowHeight = ref(ACCOUNT_ROW_HEIGHT)
  const scrollElement = shallowRef<HTMLElement | null>(null)
  const scrollContainerMissing = ref(false)
  const windowDisabled = computed(() => (
    options.rows.value.length <= WINDOW_MIN_ROWS || scrollContainerMissing.value
  ))
  const windowed = useWindowedList<Account>({
    items: options.rows,
    get itemHeight() {
      return measuredRowHeight.value
    },
    overscan: 8,
    disabled: windowDisabled,
  })

  function measureRowHeight() {
    const row = options.containerRef.value?.querySelector<HTMLElement>('tbody tr.account-row') ?? null
    if (!row) return
    const height = Math.round(row.getBoundingClientRect().height)
    if (height > 0 && Math.abs(height - measuredRowHeight.value) > 0.5) {
      measuredRowHeight.value = height
    }
  }

  function bindScrollElement() {
    const element = options.containerRef.value?.querySelector<HTMLElement>(TABLE_SCROLL_SELECTOR) ?? null
    if (scrollElement.value !== element) {
      scrollElement.value?.removeEventListener('scroll', windowed.handleScroll)
      scrollElement.value = element
      element?.addEventListener('scroll', windowed.handleScroll, { passive: true })
    }
    windowed.setScrollElement(element)
    scrollContainerMissing.value = options.active.value && !element
    measureRowHeight()
  }

  onMounted(bindScrollElement)

  watch(
    [options.refreshKey, () => options.rows.value],
    () => {
      bindScrollElement()
    },
    { flush: 'post' },
  )

  onBeforeUnmount(() => {
    scrollElement.value?.removeEventListener('scroll', windowed.handleScroll)
    scrollElement.value = null
  })

  return {
    visibleRows: windowed.visibleItems,
    topSpacerHeight: windowed.topSpacerHeight,
    bottomSpacerHeight: windowed.bottomSpacerHeight,
  }
}
