/** components/ui 内部共享类型（避免在 .vue 中导出类型）。 */

/** ApBadge 语义色档。 */
export type BadgeTone = 'default' | 'success' | 'warn' | 'danger' | 'info';

/** ApTable 列定义。key 对应行对象字段；渲染可由 `cell:<key>` 具名插槽覆盖。 */
export interface ApTableColumn {
  key: string;
  label: string;
  align?: 'left' | 'center' | 'right';
  width?: string;
}

/** ApTabs / ApStepper 的条目。 */
export interface ApTabItem {
  key: string;
  label: string;
  disabled?: boolean;
}

/** ApStepper 步骤（title 可选副标题）。 */
export interface ApStepItem {
  key: string;
  label: string;
  hint?: string;
}

/** ApToast 提示条目。 */
export interface ApToastItem {
  id: number;
  type: 'success' | 'error' | 'info';
  message: string;
}
