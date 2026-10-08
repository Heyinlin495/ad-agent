/* ============================================================
   任务分发 —— 每个"进行中任务"的运行时句柄的唯一持有者

   背景：此前每个任务的 EventSource / 定时器 / cleanup 存在 state.streams（裸 Map），
   而"取消要调用的 finish 回调"却另外存在 chat-stream.js 模块级的 cancelHandlers Map 里。
   同一个 taskId 出现在两份注册表，多一处 remove/set 就多一分不一致风险
   （任务结束漏删 cancelHandlers，会残留一个总被调用的陈旧 finish）。

   收敛：把 finish 也放进每个 task 自己的 entry，由本控制器统一持有，
   取消时从 entry.finish 取，消除平行注册表。

   API 对齐原生 Map 的最小子集（get / has / keys / forEach / delete / size），
   这样既有调用点与 jsdom 测试无需改动即可继续工作。
   ============================================================ */
export class TaskController {
  constructor() {
    this._m = new Map(); // taskId -> { es, timer, cleanup, finish }
  }

  /** 登记一个任务句柄（覆盖式：同 taskId 重复订阅时先释放旧连接由调用方负责） */
  register(taskId, entry) {
    this._m.set(taskId, entry);
    return entry;
  }

  get(taskId) {
    return this._m.get(taskId);
  }

  has(taskId) {
    return this._m.has(taskId);
  }

  keys() {
    return this._m.keys();
  }

  forEach(fn) {
    this._m.forEach(fn);
  }

  delete(taskId) {
    return this._m.delete(taskId);
  }

  clear() {
    this._m.clear();
  }

  get size() {
    return this._m.size;
  }
}