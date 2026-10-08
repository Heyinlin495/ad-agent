/**
 * 跨模块回调插槽。
 *
 * chat.js 需要在任务结束后刷新侧边栏历史，history.js 又依赖 chat.js 的渲染函数，
 * 直接互相 import 会形成循环依赖；用这个极薄的"接线板"在 main.js 里统一装配。
 */
export const hooks = {
  /** 任务产出发生变化（完成 / 取消 / 删除）后刷新历史列表 */
  refreshHistory: () => {},
};
