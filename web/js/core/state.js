import { DEFAULT_MAX_FILE_MB } from "../constants.js?v=2c2bf4b0";
import { TaskController } from "./task-controller.js?v=2c2bf4b0";

/**
 * 带 LRU 上限的 Map。超过 maxEntries 时淘汰「最近最少使用」的条目。
 * 用于存放结果快照：长会话持续生成时，快照不能无界囤积，
 * 只保留最近 N 个任务的副本即可覆盖事件委托（复制/重生成）的回查窗口，
 * 更早任务的渲染结果已落在 DOM 里，无需保留快照。
 */
export class LruMap {
  constructor(maxEntries = 64) {
    this.maxEntries = maxEntries;
    this._m = new Map();
  }
  get(key) {
    if (!this._m.has(key)) return undefined;
    const v = this._m.get(key);
    this._m.delete(key);      // 命中刷新到最新（LRU）
    this._m.set(key, v);
    return v;
  }
  set(key, value) {
    if (this._m.has(key)) this._m.delete(key);
    this._m.set(key, value);
    if (this._m.size > this.maxEntries) {
      const oldest = this._m.keys().next().value; // 表头即最久未用
      this._m.delete(oldest);
    }
    return this;
  }
  has(key) { return this._m.has(key); }
  delete(key) { return this._m.delete(key); }
  clear() { this._m.clear(); }
  get size() { return this._m.size; }
  keys() { return this._m.keys(); }
  values() { return this._m.values(); }
}

/**
 * 全局可变状态。
 *
 * 注意：所有"任务级"的运行时句柄都必须按 taskId 存进 streams，
 * 不能再放单例字段——否则并发第二个任务会抢占先断第一个任务的 SSE / 轮询。
 * results 同理：结果快照按 taskId 存放，供事件委托（复制/重生成）回查对应文案，
 * 这样同一条会话里多张结果卡片不会互相串数据。
 *
 * results 用 LruMap 设上限：结果快照体积随版本数增长，长会话若只进不出会整页
 * 慢下来（内存 + 每次 clear 前的 GC 压力）。上限 32 个任务，够覆盖可滚动
 * 回看的最近历史，同时把最老的快照淘汰（其 DOM 已渲染，不影响展示）。
 *
 * streams 用 TaskController 统一持有每个任务的句柄（es/timer/cleanup/finish），
 * 取代”裸 Map + 模块级 cancelHandlers“的双份注册，取消时从 entry.finish 取回调。
 */
export const state = {
  attached: null,      // { file, dataUrl }
  product: null,       // 当前识别（或被用户修正）的产品
  productId: null,
  historyId: null,     // 当前聊天中加载的历史任务 id
  price: "",
  promotion: "",
  maxFileMb: DEFAULT_MAX_FILE_MB,
  analyzing: false,    // 产品识别中（防重复提交）
  streams: new TaskController(), // taskId -> { es, timer, cleanup, finish }
  results: new LruMap(32), // taskId -> 结果快照（copies / images），LRU 上限 32
};
